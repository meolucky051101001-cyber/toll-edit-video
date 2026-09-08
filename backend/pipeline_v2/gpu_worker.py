"""Short-lived worker process for one heavyweight model stage."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import io
if isinstance(sys.stdout, io.TextIOWrapper):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
if isinstance(sys.stderr, io.TextIOWrapper):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

from .atomic_io import atomic_write_json
from .batching import chunked
from .segments import segments_from_dicts, segments_to_dicts
from .stage_validation import validate_demucs_outputs, validate_transformed_audio


def _prepare_legacy_imports() -> None:
    backend_directory = Path(__file__).resolve().parents[1]
    if str(backend_directory) not in sys.path:
        sys.path.insert(0, str(backend_directory))


def _run_health(_payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {"pid": os.getpid(), "worker": "pipeline_v2"}


def _run_demucs(payload: Mapping[str, Any]) -> Dict[str, Any]:
    _prepare_legacy_imports()
    from video_utils import separate_vocals_demucs

    vocals, background = separate_vocals_demucs(
        str(payload["input_audio"]),
        str(payload["output_directory"]),
        segment_seconds=float(payload.get("segment_seconds", 6.0)),
        timeout_seconds=float(payload.get("timeout_seconds", 3600.0)),
    )
    validate_demucs_outputs(payload["input_audio"], vocals, background)
    return {"vocals_path": vocals, "background_path": background}


def _run_whisper(payload: Mapping[str, Any]) -> Dict[str, Any]:
    _prepare_legacy_imports()
    from ai.transcription import extract_subtitles_whisper

    segments = extract_subtitles_whisper(
        str(payload["input_audio"]),
        str(payload["output_srt"]),
        num_workers=int(payload.get("num_workers", 1)),
    )
    return {"segments": segments_to_dicts(segments), "srt_path": str(payload["output_srt"])}


def _run_ocr(payload: Mapping[str, Any]) -> Dict[str, Any]:
    _prepare_legacy_imports()
    from ocr_utils import perform_video_ocr, release_ocr_reader

    segments = segments_from_dicts(payload.get("segments", []))
    batches = chunked(
        segments, max(1, int(payload.get("batch_segments", len(segments) or 1)))
    )
    try:
        block_count = 0
        width, height = 1080, 1920
        main_positions = []
        for batch in batches:
            blocks, width, height, main_y_pct = perform_video_ocr(
                str(payload["video_path"]),
                target_lang=str(payload.get("target_lang", "vi")),
                sample_rate=float(payload.get("sample_rate", 1.0)),
                srt_segments=batch,
            )
            block_count += len(blocks)
            main_positions.append(float(main_y_pct))
        main_y_pct = (
            sorted(main_positions)[len(main_positions) // 2]
            if main_positions
            else 0.85
        )
        return {
            "segments": segments_to_dicts(segments),
            "width": width,
            "height": height,
            "main_y_pct": main_y_pct,
            "block_count": block_count,
        }
    finally:
        release_ocr_reader()


async def _rvc_batch(payload: Mapping[str, Any]) -> Dict[str, Any]:
    _prepare_legacy_imports()
    from ai.voice_cloning import apply_rvc_clone

    model_path = str(payload["model_path"])
    completed = []
    for item in payload.get("items", []):
        input_path = str(item["input_path"])
        output_path = str(item["output_path"])
        await apply_rvc_clone(
            input_path,
            output_path,
            model_path,
            strict=True,
        )
        validate_transformed_audio(input_path, output_path, "RVC")
        completed.append(
            {"index": int(item["index"]), "output_path": output_path}
        )
    return {"items": completed}


def _run_rvc(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return asyncio.run(_rvc_batch(payload))


_HANDLERS = {
    "health": _run_health,
    "demucs": _run_demucs,
    "whisper": _run_whisper,
    "ocr": _run_ocr,
    "rvc": _run_rvc,
}


def run_request(request: Mapping[str, Any]) -> Dict[str, Any]:
    if int(request.get("schema_version", 0)) != 1:
        raise ValueError("Unsupported GPU worker request schema")
    stage = str(request.get("stage", ""))
    try:
        handler = _HANDLERS[stage]
    except KeyError as exc:
        raise ValueError("Unsupported GPU worker stage: {!r}".format(stage)) from exc
    return handler(dict(request.get("payload", {})))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one pipeline v2 GPU stage")
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    return parser


def main(argv: Sequence[str] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = run_request(request)
        atomic_write_json(
            args.response,
            {"schema_version": 1, "success": True, "result": result},
        )
        return 0
    except BaseException as exc:
        atomic_write_json(
            args.response,
            {
                "schema_version": 1,
                "success": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "traceback": traceback.format_exc(),
            },
        )
        return 1
    finally:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
