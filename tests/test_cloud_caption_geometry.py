import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend.cloud.gpu_scripts import caption_geometry as geometry
from backend.cloud.gpu_scripts.subtitle_timing import SubtitleTimingError, generate_cloud_ass


def evidence(count=1):
    return dict(version=1, passed=True, cue_count=count,
                verified_cues=[str(i+1) for i in range(count)], unresolved_cues=[],
                sampled_frames=3, caption_samples=0, empty_samples=3, source_mask_count=0)


def test_low_recognition_confidence_does_not_discard_measured_geometry(monkeypatch):
    # Reproduce the real 18.5s source box whose CJK confidence was 3e-10.
    boxes = iter([[[27/720,743/1038,697/720,805/1038]],
                  [[30/720,744/1038,696/720,806/1038]]])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(boxes))
    measured = geometry.observe_caption_boxes(None, np.zeros((1038,720,3)), band_top=.7158,band_bottom=.78)
    assert measured == [[27/720,743/1038,697/720,806/1038]]


def test_detector_disagreement_is_unknown_not_caption_absence(monkeypatch):
    answers = iter([[[.2,.72,.8,.78]], []])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(answers))
    with pytest.raises(geometry.CaptionGeometryError, match="disagree"):
        geometry.observe_caption_boxes(None, np.zeros((10,10,3)),band_top=.72,band_bottom=.78)


def test_compact_caption_probe_prefers_spoken_rows_over_corner_badge():
    from backend.cloud.gpu_scripts import full_pipeline

    def boxes(y1, y2, text, x1=.32, x2=.68):
        x1, x2 = (x1 * 1440, x2 * 1440)
        y1, y2 = ((y1 * 1280 - 1049) * 2, (y2 * 1280 - 1049) * 2)
        return ([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], text, .9)

    badge = boxes(.958, .976, "Blind Box Unboxing", x1=.75, x2=.96)
    observations = iter([
        [boxes(.88, .90, "open the package"), boxes(.92, .94, "打开盒子"), badge],
        [boxes(.89, .91, "change her paws"), boxes(.95, .97, "换她的小爪子"), badge],
    ])
    reader = SimpleNamespace(readtext=lambda *args, **kwargs: next(observations))
    segments = [{"id": 1, "start": 0., "end": 2.}, {"id": 2, "start": 2., "end": 4.}]
    band, y_bounds, x_bounds = full_pipeline._probe_compact_caption_band(
        reader, lambda t: (True, np.zeros((1280, 720, 3), dtype=np.uint8)),
        [(segments[0], [1.]), (segments[1], [3.])],
        {1: "Now we open the package", 2: "I have to change her paws"},
        width=720, height=1280, duration=4.,
    )
    assert set(band.selected_by_segment) == {1, 2}
    assert y_bounds == pytest.approx((.88, .97))
    assert x_bounds == pytest.approx((.265, .735))


def test_compact_caption_geometry_projects_boxes_into_the_source_frame(monkeypatch):
    probes = []

    def detect(reader, crop, *, top, bottom, scale):
        probes.append((crop.shape[1], scale))
        return [[.2, .9, .8, .92]]

    monkeypatch.setattr(geometry, "_detect", detect)
    measured = geometry.observe_caption_boxes(
        object(), np.zeros((100, 100, 3)), band_top=.87, band_bottom=.98, x_bounds=(.25, .75),
    )
    assert probes == [(50, 1.5), (50, 2.)]
    assert measured[0] == pytest.approx([.35, .9, .65, .92])


@pytest.mark.parametrize("corroborated", [True, False])
def test_larger_probe_requires_an_independent_caption_measurement(monkeypatch, corroborated):
    boxes = {
        1.: [[.18, .735, .60, .775]],
        1.25: [[.71, .731, .93, .771]],
        1.5: [[.22, .78, .64, .809]],
        2.: [[.17, .734, .61, .777]] if corroborated else [[.68, .78, .98, .81]],
    }
    attempted = []

    def detect(*args, scale, **kwargs):
        attempted.append(scale)
        return boxes[scale]

    monkeypatch.setattr(geometry, "_detect", detect)
    frame = np.zeros((10, 10, 3))
    if corroborated:
        assert geometry.observe_caption_boxes(object(), frame, band_top=.72, band_bottom=.82) == boxes[2.]
    else:
        with pytest.raises(geometry.CaptionGeometryError, match="scales disagree"):
            geometry.observe_caption_boxes(object(), frame, band_top=.72, band_bottom=.82)
    assert attempted == [1., 1.5, 1.25, 2.]


def test_text_without_dialogue_anchor_is_not_masked_as_caption(monkeypatch):
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: [[.2,.72,.8,.78]])
    with pytest.raises(geometry.CaptionGeometryError, match="no verified"):
        geometry.observe_caption_boxes(None, np.zeros((10,10,3)))


def test_real_no_caption_evidence_can_pass_without_synthetic_mask(tmp_path, monkeypatch):
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: [])
    assert geometry.observe_caption_boxes(None, np.zeros((10,10,3))) == []
    geometry.validate_caption_evidence(evidence(),cue_count=1)
    cue=dict(id=1,start=0.,end=1.,text="原话",translated_text="Lời dịch",tts_duration=.5,
             ocr_mask_status="verified_no_caption",caption_geometry_verified=True)
    audit=generate_cloud_ass([cue],tmp_path/'ok.ass',video_w=720,video_h=1038,video_duration=1.)
    assert audit['passed'] and audit['source_masks']==[]
    cue['caption_geometry_verified']=False
    with pytest.raises(SubtitleTimingError,match="absence is unverified"):
        generate_cloud_ass([cue],tmp_path/'bad.ass',video_duration=1.)


def test_old_missing_ocr_artifact_cannot_be_rendered_as_verified(tmp_path):
    cue=dict(id=1,start=0.,end=1.,text="原话",translated_text="Lời dịch",
             ocr_mask_status="no_caption_evidence")
    with pytest.raises(SubtitleTimingError,match="unverified"):
        generate_cloud_ass([cue],tmp_path/'bad.ass',video_duration=1.)


def test_reacquisition_keeps_empty_gaps_and_source_masks_outside_speech(tmp_path,monkeypatch):
    import cv2
    position=[0]
    capture=SimpleNamespace(isOpened=lambda:True,release=lambda:None,
        set=lambda key,value:(position.__setitem__(0,int(value)) or True),
        get=lambda key:{cv2.CAP_PROP_FRAME_WIDTH:720,cv2.CAP_PROP_FRAME_HEIGHT:1038,
                        cv2.CAP_PROP_FPS:10.,cv2.CAP_PROP_FRAME_COUNT:10,
                        cv2.CAP_PROP_POS_MSEC:position[0]*100.,
                        cv2.CAP_PROP_POS_FRAMES:position[0]+1.}[key],
        read=lambda:(True,np.full((1,1,3),position[0],dtype=np.uint8)))
    monkeypatch.setattr(cv2,'VideoCapture',lambda path:capture)
    monkeypatch.setattr(geometry,'observe_caption_boxes',lambda reader,frame,**kw:
                        [[.1,.72,.9,.78]] if int(frame[0,0,0]) in {0,1,2,7,8,9} else [])
    cues=[dict(id=1,start=.1,end=.8,text="字幕")]
    report=geometry.acquire_caption_geometry('source',cues,None,duration=1.,audit_path=tmp_path/'audit.json')
    geometry.validate_caption_evidence(report,cue_count=1)
    rows=cues[0]['tracking_blocks']
    assert min(row['start'] for row in rows)==0.
    assert max(row['end'] for row in rows)==1.
    assert all(row['end']<=.3 or row['start']>=.7 for row in rows)
    assert report['empty_samples']>0 and report['caption_samples']>0


@pytest.mark.parametrize('variable_hint', [True, False])
def test_caption_clock_uses_source_pts_when_opencv_timestamps_repeat(tmp_path, monkeypatch, variable_hint):
    import cv2

    pts = [0., .031, .071, .1, .137, .2, .23, .27]
    seeks = []

    class Capture:
        position = 0

        def isOpened(self):
            return True

        def release(self):
            pass

        def grab(self):
            if self.position == len(pts):
                return False
            self.position += 1
            return True

        def read(self):
            index = self.position
            return self.grab(), np.full((1, 1, 3), index, dtype=np.uint8)

        def set(self, key, position):
            seeks.append(position)
            self.position = position
            return True

        def get(self, key):
            return {
                cv2.CAP_PROP_FRAME_WIDTH: 720, cv2.CAP_PROP_FRAME_HEIGHT: 1280,
                cv2.CAP_PROP_FPS: 10., cv2.CAP_PROP_FRAME_COUNT: len(pts),
                cv2.CAP_PROP_POS_FRAMES: self.position, cv2.CAP_PROP_POS_MSEC: 0.,
            }[key]

    def probe(_path, *, include_frames=False):
        if include_frames:
            return {'format': {'start_time': '5.0'},
                    'frames': [{'best_effort_timestamp_time': str(5. + time)} for time in pts]}
        return {'streams': [{'r_frame_rate': '30/1', 'avg_frame_rate': '29/1' if variable_hint else '30/1'}]}

    monkeypatch.setattr(cv2, 'VideoCapture', lambda _: Capture())
    monkeypatch.setattr(geometry, '_probe_frame_clock', probe)
    monkeypatch.setattr(geometry, 'observe_caption_boxes', lambda reader, frame, **kw:
                        [[.1, .72, .9, .78]] if 2 <= frame[0, 0, 0] < 5 else [])
    cues = [dict(id=1, start=0., end=.3, text='字幕')]
    report = geometry.acquire_caption_geometry('source', cues, None, duration=.3, audit_path=tmp_path / 'pts.json')
    assert report['frame_clock'] == 'source_presentation_timestamps'
    assert report['passed'] and report['refinement_frames'] > 0
    assert [(row['start'], row['end']) for row in cues[0]['tracking_blocks']] == [(pts[2], pts[5])]
    assert bool(seeks) == (not variable_hint)


def test_duplicate_source_presentation_timestamps_still_fail(monkeypatch):
    monkeypatch.setattr(geometry, '_probe_frame_clock', lambda *a, **kw: {
        'frames': [{'best_effort_timestamp_time': '0'}, {'best_effort_timestamp_time': '0'}],
    })
    with pytest.raises(geometry.CaptionGeometryError, match='Source presentation timestamps must increase'):
        geometry._verified_frame_times('source', 1.)


@pytest.mark.parametrize('change',[
    {'passed':False},{'verified_cues':[]},{'unresolved_cues':['1']},
    {'sampled_frames':0},{'caption_samples':1,'empty_samples':1},
    {'caption_samples':3,'empty_samples':0,'source_mask_count':0},
])
def test_incomplete_evidence_cannot_pass(change):
    value=evidence();value.update(change)
    with pytest.raises(geometry.CaptionGeometryError):
        geometry.validate_caption_evidence(value,cue_count=1)


def test_download_qc_rejects_old_success_without_source_cover_evidence(tmp_path,monkeypatch):
    from backend.cloud.cloud_orchestrator import CloudOrchestrator,_OrchestratorStageError
    from backend.cloud.cloud_job_store import CloudJobStore
    from backend.cloud import cloud_orchestrator
    output=tmp_path/'output_video.mp4';output.write_bytes(b'\x00'*64)
    meta=dict(status='completed',segments_count=1,translated_count=1,tts_verified_count=1,
              input_duration_seconds=1.,subtitle_timing={'passed':True,'cue_count':1})
    (tmp_path/'result_metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    monkeypatch.setattr(cloud_orchestrator,'probe_video_metadata',lambda _:dict(
        probe_method='ffprobe',video_codec='h264',audio_codec='aac',duration_seconds=1.))
    orch=CloudOrchestrator(store=CloudJobStore(tmp_path/'jobs.db'),adapter=SimpleNamespace())
    with pytest.raises(_OrchestratorStageError,match='has not been verified'):
        orch.validate_downloaded_output(output,tmp_path)
    assert json.loads((tmp_path/'qc_report.json').read_text())['passed'] is False
    meta['source_caption_evidence']=evidence()
    (tmp_path/'result_metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    assert orch.validate_downloaded_output(output,tmp_path)['qc_report']['passed']
    del meta['segments_count']
    (tmp_path/'result_metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    with pytest.raises(_OrchestratorStageError,match='missing its caption'):
        orch.validate_downloaded_output(output,tmp_path)


def test_cloud_bundle_declares_and_imports_caption_dependencies():
    from backend.cloud.cloud_orchestrator import _V1_CORE_MODULE_NAMES
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'backend/cloud/gpu_scripts/cloud_env_manifest.json').read_text())
    assert 'visual_caption_timing.py' in _V1_CORE_MODULE_NAMES
    assert {'caption_geometry.py','visual_caption_timing.py'} <= set(manifest['required_v1_modules'])
    assert callable(importlib.import_module('visual_caption_timing').refine_observations)
    assert callable(geometry.acquire_caption_geometry)


def test_cloud_bundle_runs_standalone_ass_with_new_caption_contract(tmp_path):
    import shutil
    import subprocess
    import sys
    from backend.cloud.cloud_orchestrator import _V1_CORE_MODULE_NAMES
    root=Path(__file__).resolve().parents[1]
    bundle=tmp_path/'bundle';bundle.mkdir()
    for name in _V1_CORE_MODULE_NAMES:
        shutil.copy2(root/'backend'/name,bundle/name)
    for path in (root/'backend/cloud/gpu_scripts').glob('*.py'):
        shutil.copy2(path,bundle/path.name)
    script="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from caption_geometry import validate_caption_evidence
from visual_caption_timing import refine_observations
from subtitle_timing import generate_cloud_ass
cue=dict(id=1,start=0.,end=1.,text='source',translated_text='Test subtitle',ocr_mask_status='verified_no_caption',caption_geometry_verified=True)
report=generate_cloud_ass([cue],Path(sys.argv[1])/'smoke.ass',video_w=720,video_h=1038,video_duration=1.)
assert report['passed'] and report['source_masks']==[]
"""
    result=subprocess.run([sys.executable,'-I','-B','-c',script,str(bundle)],cwd=bundle,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('seek_ok,decoded_next',[(False,1.),(True,7.)])
def test_bad_decoder_seek_never_creates_verified_geometry(tmp_path,monkeypatch,seek_ok,decoded_next):
    import cv2
    capture=SimpleNamespace(isOpened=lambda:True,release=lambda:None,set=lambda *a:seek_ok,
        get=lambda key:{cv2.CAP_PROP_FRAME_WIDTH:720,cv2.CAP_PROP_FRAME_HEIGHT:1038,
                        cv2.CAP_PROP_FPS:10.,cv2.CAP_PROP_FRAME_COUNT:10,
                        cv2.CAP_PROP_POS_MSEC:0.,cv2.CAP_PROP_POS_FRAMES:decoded_next}[key],
        read=lambda:(True,np.zeros((1,1,3),dtype=np.uint8)))
    monkeypatch.setattr(cv2,'VideoCapture',lambda path:capture)
    monkeypatch.setattr(geometry,'observe_caption_boxes',lambda *a,**kw:pytest.fail('unverified frame reached detector'))
    audit=tmp_path/'bad.json'
    with pytest.raises(geometry.CaptionGeometryError):
        geometry.acquire_caption_geometry('source',[],None,duration=1.,audit_path=audit)
    assert json.loads(audit.read_text())['passed'] is False


def test_same_line_word_split_and_two_line_cluster_agree_across_scales(monkeypatch):
    # 1. Horizontal space split at 1.0x vs merged single line at 1.5x
    horiz = iter([
        [[0.2111, 0.7656, 0.5083, 0.7937], [0.5278, 0.7656, 0.7861, 0.7937]],
        [[0.2130, 0.7656, 0.7852, 0.7927]],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(horiz))
    res_h = geometry.observe_caption_boxes(None, np.zeros((1280, 720, 3)), band_top=0.7516, band_bottom=0.8078)
    assert len(res_h) == 1
    assert res_h[0][0] == pytest.approx(0.2111) and res_h[0][2] == pytest.approx(0.7861)

    # 2. Merged 2-line box at 1.0x vs two stacked lines at 1.5x
    two_line = iter([
        [[0.2944, 0.7523, 0.6972, 0.8086]],
        [[0.3389, 0.7565, 0.6611, 0.7826], [0.3019, 0.7753, 0.6889, 0.8034]],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(two_line))
    res_2 = geometry.observe_caption_boxes(None, np.zeros((1280, 720, 3)), band_top=0.7516, band_bottom=0.8078)
    assert len(res_2) == 1
    assert res_2[0][1] == pytest.approx(0.7523) and res_2[0][3] == pytest.approx(0.8086)

    # 3. Micro product speck on band fringe at 1.5x does not reject agreed dialogue caption
    speck = iter([
        [[0.2736, 0.7656, 0.7292, 0.7937]],
        [[0.2759, 0.7659, 0.7259, 0.7940], [0.4926, 0.7501, 0.5343, 0.7655]],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(speck))
    res_s = geometry.observe_caption_boxes(None, np.zeros((1280, 720, 3)), band_top=0.7516, band_bottom=0.8078)
    assert len(res_s) == 1
    assert res_s[0][0] == pytest.approx(0.2736) and res_s[0][2] == pytest.approx(0.7292)

    # 4. Tie-breaker 1.25x scale resolves 1-frame transition flicker when reader is present
    tie = iter([
        [[0.25, 0.76, 0.75, 0.79]],
        [],
        [[0.252, 0.761, 0.748, 0.791]],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(tie))
    res_t = geometry.observe_caption_boxes(object(), np.zeros((1280, 720, 3)), band_top=0.75, band_bottom=0.81)
    assert len(res_t) == 1

    # 5. Peripheral micro poster print in outer margin (<42% band height) does not reject agreed 2-line caption
    poster_edge = iter([
        [
            [0.2125, 0.7555, 0.7903, 0.7836],
            [0.3042, 0.7758, 0.6792, 0.8039],
            [0.0708, 0.7810, 0.1361, 0.7932],
        ],
        [
            [0.2130, 0.7563, 0.7852, 0.7823],
            [0.3056, 0.7760, 0.6778, 0.8031],
            [0.0710, 0.7752, 0.1817, 0.7962],
        ],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(poster_edge))
    res_p = geometry.observe_caption_boxes(None, np.zeros((1280, 720, 3)), band_top=0.7523, band_bottom=0.8086)
    assert len(res_p) == 2
    assert res_p[0][0] == pytest.approx(0.2125) and res_p[0][2] == pytest.approx(0.7903)

