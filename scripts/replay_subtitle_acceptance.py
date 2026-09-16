"""Re-render retained smoke artifacts, without rerunning paid/network AI stages.

This is a render/QC regression check, NOT a cold end-to-end benchmark.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from environment import load_environment
from ass_utils import generate_ass_file
from pipeline_v2.segments import segments_from_dicts
from pipeline_v2.qc import run_report_only_qc, QCSettings
from video_utils import process_video


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    load_environment(ROOT / 'backend')
    if args.output.exists():
        raise FileExistsError('Use a new output directory to preserve previous evidence')
    artifacts = args.run / 'job/pipeline_v2/artifacts'
    if not artifacts.is_dir():
        artifacts = args.run / 'pipeline_v2/artifacts'
    segments_path = artifacts / 'qc/segments.json'
    raw = json.loads(segments_path.read_text(encoding='utf-8'))['segments']
    manifest = json.loads((artifacts.parent / 'job_manifest.json').read_text(encoding='utf-8'))
    ocr = json.loads((artifacts / 'ocr/result.json').read_text(encoding='utf-8'))
    args.output.mkdir(parents=True)
    ass = args.output / 'final.ass'
    video = args.output / 'final.mp4'
    generate_ass_file(segments_from_dicts(raw), [], ass,
        ocr['width'], ocr['height'], video_duration=manifest['metadata']['source_duration_seconds'])
    if not process_video(str(args.source), str(ass), str(artifacts / 'audio/mixed_v2.wav'),
                         str(video), timeout_seconds=240):
        raise RuntimeError('Render failed')
    report = run_report_only_qc(video, args.output / 'qc_report.json', video,
        segments_path, ass, args.output / 'diagnostics', QCSettings(gate_policy='block'))
    print([(c.name, c.status) for c in report.checks])
    return int(any(c.status == 'error' for c in report.checks))


if __name__ == '__main__':
    sys.exit(main())
