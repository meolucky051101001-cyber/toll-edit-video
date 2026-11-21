"""Single-job V1 ASR worker. No OCR, TTS, Telegram or CPU fallback."""
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.pyav_compat import install_pyav_compat

install_pyav_compat()

from ai.transcription import extract_subtitles_whisper


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _serve() -> int:
    os.environ["V1_ASR_REUSE"] = "1"
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        response_path = None
        try:
            envelope = json.loads(raw)
            response_path = Path(envelope["response"])
            request_path = Path(envelope["request"])
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            options = payload.get("options") or {}
            if not isinstance(options, dict):
                raise TypeError("ASR request options must be an object")
            audio = str(payload["audio"])
            output = str(payload["output"])
            original_audio = payload.get("original_audio")
            segments = extract_subtitles_whisper(
                audio,
                output,
                num_workers=1,
                original_audio_path=str(original_audio) if original_audio else None,
                **options,
            )
            from v1_stage_runtime import save_payload
            save_payload(Path(output).with_suffix(".json"), segments=segments)
            _write_json_atomic(response_path, {"success": True})
        except Exception as exc:
            logging.getLogger(__name__).exception("ASR serve request failed")
            if response_path is not None:
                try:
                    _write_json_atomic(
                        response_path,
                        {"success": False, "error": str(exc)},
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Could not write ASR error response")
    return 0


def main() -> int:
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError) as exc:
            logging.getLogger(__name__).debug("Could not configure worker stream encoding: %s", exc)
    logging.basicConfig(level=logging.INFO)
    if "--serve" in sys.argv:
        return _serve()
    orig_audio = sys.argv[3] if len(sys.argv) > 3 else None
    options = json.loads(os.environ.get("V1_ASR_JOB_OPTIONS", "{}"))
    segments = extract_subtitles_whisper(sys.argv[1], sys.argv[2], num_workers=1, original_audio_path=orig_audio, **options)
    from v1_stage_runtime import save_payload
    save_payload(Path(sys.argv[2]).with_suffix(".json"), segments=segments)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
