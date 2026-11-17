"""Observed source geometry survives rendering, validation, and checkpoint reuse."""
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace as NS

import pytest

from backend.ass_utils import generate_ass_file, sync_and_clamp_subtitles
from backend.pipeline_v2.cover_qc import build_expected_cover_timeline, inspect_covers, parse_ass_covers
from backend import v1_ocr_geometry as geometry
from backend.ocr_subtitle_locator import select_chinese_subtitle_band


def observed(start=.4, end=1., **fields):
    return NS(start=start, end=end, x_pct=.1, max_x_pct=.9, y_pct=.72,
              max_y_pct=.80, geometry_verified=True, **fields)


def segment(tracks, start=0., end=3., **fields):
    return NS(index=1, start=timedelta(seconds=start), end=timedelta(seconds=end),
              content='Phụ đề tiếng Việt: chữ dịch dài hơn chữ nguồn',
              best_block=tracks[0] if tracks else None, tracking_blocks=tracks,
              geometry_verified=True, **fields)


def test_source_lifetime_is_independent_of_speech_and_tts(tmp_path):
    item = segment([observed(.5, 2.5)], start=1., end=2.)
    path = tmp_path / 'source.ass'
    generate_ass_file([item], [], path, video_duration=3)
    ass = path.read_text(encoding='utf-8-sig')
    covers, _, _ = parse_ass_covers(ass)
    assert [(a, b) for a, b, *_ in covers] == [(.5, 1.), (1., 2.), (2., 2.5)]
    texts = [line.split(',', 9) for line in ass.splitlines() if line.startswith('Dialogue: 1,')]
    assert [(row[1], row[2]) for row in texts] == [('0:00:01.00', '0:00:02.00')]
    item.end = timedelta(seconds=4)  # Longer TTS changes only translation lifetime.
    generate_ass_file([item], [], path, video_duration=4)
    covers, _, _ = parse_ass_covers(path.read_text(encoding='utf-8-sig'))
    assert min(row[0] for row in covers) == .5
    assert max(row[1] for row in covers) == 2.5


def test_cover_qc_preserves_verified_absence_and_does_not_mutate(tmp_path):
    item = segment([observed(.4, 1.), observed(1.8, 2.2)])
    before = copy.deepcopy(vars(item))
    path = tmp_path / 'source.ass'
    generate_ass_file([item], [], path, video_duration=3)
    ass = path.read_text(encoding='utf-8-sig')
    timeline = build_expected_cover_timeline([item], video_duration=3)
    assert [(row.expected_start, row.expected_end) for row in timeline] == [(.4, 1.), (1.8, 2.2)]
    assert all(row.hold_seconds == 0 and not row.bridged_to_next for row in timeline)
    assert not inspect_covers([item], ass, video_duration=3)['failures']
    assert vars(item) == before
    item.tracking_blocks = []  # Verified empty is authoritative, despite old best_block.
    assert build_expected_cover_timeline([item], video_duration=3) == []


def test_quality_gate_does_not_count_translation_background_as_source_mask(tmp_path, monkeypatch):
    from backend.v1_quality_gate import run_quality_gate
    import v1_media_streams
    monkeypatch.setattr(v1_media_streams, 'probe_main_video',
                        lambda *_: (_ for _ in ()).throw(ValueError('isolated media fixture')))
    item = segment([observed()], ocr_mask_status='located')
    path = tmp_path / 'source.ass'
    generate_ass_file([item], [], path, video_duration=3)
    ass = path.read_text(encoding='utf-8-sig')
    # Deliberately remove only source-mask events; leave style declarations and
    # actual translation drawings, which the old substring check accepted.
    ass = '\n'.join(line for line in ass.splitlines()
                    if not (line.startswith('Dialogue:') and ',SourceMaskStyle,' in line))
    path.write_text(ass, encoding='utf-8-sig')
    with pytest.raises(RuntimeError):  # Fixture intentionally has no media/audio.
        run_quality_gate('text_background_only', tmp_path / 'missing.mp4',
                         translated_subtitles=[item], ass_subtitle_path=path,
                         workspace_path=tmp_path, enable_visual_pixel_qc=False)
    report = json.loads((tmp_path / 'bot_system/qc_reports/text_background_only.qc.json').read_text(encoding='utf-8'))
    check = next(row for row in report['checks'] if row['name'] == 'visual_mask_coverage')
    assert check['status'] == 'UNKNOWN'


@pytest.mark.parametrize('as_dict', [False, True])
def test_tts_sync_does_not_stretch_merge_or_clip_verified_tracks(as_dict):
    item = segment([observed(.4, 1.), observed(1.2, 1.8)])
    before = copy.deepcopy(item.tracking_blocks)
    if as_dict:
        item = {**vars(item), 'start': 0., 'end': 3.}
    sync_and_clamp_subtitles([item], [{'index': 1, 'actual_audio_duration': 5}])
    assert (item['end'] if as_dict else item.end.total_seconds()) == 5
    assert (item['tracking_blocks'] if as_dict else item.tracking_blocks) == before


def test_rescued_rows_cannot_create_a_chain_between_original_anchors():
    speech = {1: '今天我们学习做饭', 2: '先准备所有材料', 3: '然后把菜放进锅里', 4: '晚餐已经做好了'}
    texts = {1: speech[1], 2: '肉弼唣愈屙豳凸纸', 3: '芈弼倥泶魉呔伧炅', 4: speech[4]}
    rows = [dict(text=texts[sid], start=sid-1, end=sid, sample_segment_id=sid,
                 sample_time=sid-.5, x_pct=.2, max_x_pct=.8, y_pct=.75,
                 max_y_pct=.79, prob=.95 if sid in (1, 4) else .001)
            for sid in speech]
    result = select_chinese_subtitle_band(rows, speech, 1080, 1920)
    assert set(result.selected_by_segment) == {1, 4}


@pytest.mark.parametrize('strict', [True, False])
@pytest.mark.parametrize('as_dict', [True, False])
def test_validation_never_promotes_or_erases_unresolved(strict, as_dict):
    good = segment([observed()], ocr_mask_status='located')
    unresolved = segment([], ocr_mask_status='unresolved')
    unresolved.index = 3
    items = [good, copy.deepcopy(good), unresolved]
    if as_dict:
        items = [vars(item) for item in items]
    before = copy.deepcopy(items)
    if strict:
        with pytest.raises(geometry.OCRGeometryError):
            geometry.validate_mask_geometry(items, strict=True)
    else:
        assert geometry.validate_mask_geometry(items, strict=False) is False
    assert items == before


@pytest.mark.parametrize('version', [None, geometry.OCR_GEOMETRY_VERSION - 1, geometry.OCR_GEOMETRY_VERSION])
def test_resume_invalidates_only_ocr_and_downstream_stages(tmp_path, version):
    from backend.v1_orchestrator import V1Orchestrator
    from backend.v1_checkpoint import STAGES_ORDER
    from backend.v1_stage_runtime import save_payload
    save_payload(tmp_path / 'ocr.json', geometry_version=version, segments=[])
    original = tmp_path / 'keep-asr.json'
    original.write_text('retained ASR artifact', encoding='utf-8')
    orchestrator = object.__new__(V1Orchestrator)
    orchestrator.manifest_manager = NS(verify_manifest_resumability=lambda *a, **k:
                                      (True, 'completed', {'verified_stages': list(STAGES_ORDER)}))
    ctx = NS(manifest=NS(routing_snapshot={'metadata': {'duration_s': 3}}),
             effective_cfg={'speech_timing_version': 3}, visual_sub_to_speech=False,
             job_id='fixture',
             v_path=tmp_path / 'source.mp4', out_dir=tmp_path)
    orchestrator._restore_execution_checkpoint(ctx, True)
    if version == geometry.OCR_GEOMETRY_VERSION:
        assert ctx.verified_stages == list(STAGES_ORDER)
    else:
        assert ctx.verified_stages == STAGES_ORDER[:STAGES_ORDER.index('visual_ocr')]
        assert 'video_rendering' not in ctx.verified_stages
    assert original.read_text(encoding='utf-8') == 'retained ASR artifact'


def test_synthetic_source_masks_at_rendered_frame_boundaries(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg is needed for the pixel acceptance fixture')
    from tools.render_ocr_remediation_evidence import create_evidence
    output = Path(os.environ.get('OCR_REMEDIATION_EVIDENCE', str(tmp_path)))
    report = create_evidence(output)
    assert report['geometry_version'] == geometry.OCR_GEOMETRY_VERSION
    assert report['checks'] and all(row['passed'] for row in report['checks'])
    assert report['source_intervals'] == [[.4, 1.], [1.8, 2.2]]
    assert (output / 'audit.json').is_file()
