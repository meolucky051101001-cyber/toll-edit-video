"""Isolated runtime for optional best-quality local models.

This file intentionally imports no AutoDub modules so it can run inside the
separate ``backend/model_venv`` without disturbing the bot's proven venv.
Requests and responses are JSON files to avoid shell quoting issues on Windows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


def _json_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False), encoding="utf-8"
    )
    os.replace(str(temporary), str(target))


def _classify_separator_outputs(
    output_files: Iterable[str], output_directory: str
) -> Dict[str, str]:
    resolved = []
    for value in output_files:
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = Path(output_directory) / candidate
        resolved.append(candidate.resolve())
    vocals = next(
        (item for item in resolved if "vocal" in item.name.lower()), None
    )
    background = next(
        (
            item
            for item in resolved
            if any(token in item.name.lower() for token in ("instrumental", "no_vocal"))
        ),
        None,
    )
    if vocals is None or background is None:
        raise RuntimeError(
            "Could not identify Vocals/Instrumental outputs: {}".format(
                [str(item) for item in resolved]
            )
        )
    if not vocals.is_file() or not background.is_file():
        raise RuntimeError("Audio separator reported output files that do not exist")
    return {"vocals_path": str(vocals), "background_path": str(background)}


def _run_separator(payload: Mapping[str, Any]) -> Dict[str, Any]:
    from audio_separator.separator import Separator

    output_directory = str(Path(payload["output_directory"]).resolve())
    model_directory = str(Path(payload["model_directory"]).resolve())
    Path(output_directory).mkdir(parents=True, exist_ok=True)
    Path(model_directory).mkdir(parents=True, exist_ok=True)
    separator = Separator(
        log_level=logging.INFO,
        output_dir=output_directory,
        model_file_dir=model_directory,
        output_format="WAV",
        use_soundfile=True,
        use_native_fp16=bool(payload.get("use_native_fp16", True)),
    )
    separator.load_model(model_filename=str(payload["model_filename"]))
    outputs = separator.separate(str(Path(payload["input_audio"]).resolve()))
    result = _classify_separator_outputs(outputs, output_directory)
    result.update(
        {
            "model": str(payload["model_filename"]),
            "effective_precision": str(
                getattr(separator, "effective_precision", "unknown")
            ),
        }
    )
    return result


def _paddle_payload(result: Any) -> Mapping[str, Any]:
    value = getattr(result, "json", result)
    if callable(value):
        value = value()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping) and hasattr(result, "to_dict"):
        value = result.to_dict()
    if not isinstance(value, Mapping):
        raise TypeError("Unsupported PaddleOCR result type: {}".format(type(result)))
    nested = value.get("res", value)
    return nested if isinstance(nested, Mapping) else value


_ocr_models = {}


def _run_paddle_ocr(payload: Mapping[str, Any]) -> Dict[str, Any]:
    # Models are already cached locally. Import-time host probing can block
    # every new job on remote DNS/TLS even though no download is required.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR

    key = (str(payload.get("ocr_version", "PP-OCRv6")), str(payload.get("engine", "onnxruntime")))
    if key not in _ocr_models:
        _ocr_models.clear()
        _ocr_models[key] = PaddleOCR(
            ocr_version=str(payload.get("ocr_version", "PP-OCRv6")),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            engine=str(payload.get("engine", "onnxruntime")),
            device="gpu:0",
        )
    ocr = _ocr_models[key]
    images = []
    for image_path in payload.get("images", []):
        predictions = list(ocr.predict(str(Path(image_path).resolve())))
        rows = []
        for prediction in predictions:
            data = _paddle_payload(prediction)
            boxes = data.get("rec_polys")
            if boxes is None or len(boxes) == 0:
                boxes = data.get("dt_polys")
            if boxes is None:
                boxes = []
            texts = data.get("rec_texts")
            scores = data.get("rec_scores")
            if texts is None:
                texts = []
            if scores is None:
                scores = []
            for index, text in enumerate(texts):
                if index >= len(boxes):
                    break
                rows.append(
                    {
                        "bbox": _json_value(boxes[index]),
                        "text": str(text),
                        "score": float(scores[index]) if index < len(scores) else 0.0,
                    }
                )
        images.append({"path": str(image_path), "rows": rows})
    return {
        "images": images,
        "model": str(payload.get("ocr_version", "PP-OCRv6")),
        "engine": str(payload.get("engine", "onnxruntime")),
    }


_HANDLERS = {
    "separator": _run_separator,
    "paddle_ocr": _run_paddle_ocr,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--session-dir")
    args = parser.parse_args()
    if args.session_dir:
        import time
        root = Path(args.session_dir)
        idle = time.monotonic()
        while root.is_dir() and time.monotonic()-idle < 300:
            if (root / 'stop').exists():
                break
            requests = sorted(root.glob('request-*.json'))
            if not requests:
                time.sleep(.05)
                continue
            for path in requests:
                try:
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    data = {'success': True, 'result': _run_paddle_ocr(payload)}
                except Exception as exc:
                    data = {'success': False, 'error': str(exc)}
                response = root/path.name.replace('request-', 'response-')
                temporary = response.with_suffix('.tmp')
                temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                path.unlink()
                temporary.replace(response)
                idle = time.monotonic()
        return 0
    if not args.request or not args.response:
        parser.error('--request and --response are required without --session-dir')
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        stage = str(request.get("stage", ""))
        handler = _HANDLERS[stage]
        result = handler(dict(request.get("payload", {})))
        _write_json(args.response, {"success": True, "result": result})
        return 0
    except BaseException as exc:
        _write_json(
            args.response,
            {
                "success": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "traceback": traceback.format_exc(),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
