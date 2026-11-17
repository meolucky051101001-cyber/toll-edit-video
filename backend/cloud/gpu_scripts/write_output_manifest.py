"""Emit identity and integrity metadata only after the pipeline has finished."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess


def duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'json', str(path)], check=True, capture_output=True, text=True, timeout=30)
    seconds = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('invalid_media_duration')
    return seconds


def main():
    parser = argparse.ArgumentParser()
    for name in ('job-id', 'attempt-id', 'source-sha256', 'input'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--source-kind', choices=('file', 'url'), required=True)
    args = parser.parse_args()
    output = Path('output_video.mp4')
    actual, expected = duration(output), duration(args.input)
    if abs(actual - expected) > max(.5, expected * .01):
        raise ValueError('output_duration_mismatch')
    if args.source_kind == 'file':
        input_digest = hashlib.sha256()
        with open(args.input, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                input_digest.update(chunk)
        if input_digest.hexdigest() != args.source_sha256:
            raise ValueError('uploaded_source_hash_mismatch')
    digest = hashlib.sha256()
    with output.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    manifest = dict(job_id=args.job_id, attempt_id=args.attempt_id, source_sha256=args.source_sha256,
                    sha256=digest.hexdigest(), size_bytes=output.stat().st_size, duration_seconds=actual)
    Path('output_manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')


if __name__ == '__main__':
    main()
