"""Isolated PP-OCRv6 worker used only by Tool V1."""

from __future__ import annotations

import argparse
import json
import os
import traceback
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


def _json_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False), encoding="utf-8"
    )
    os.replace(str(temporary), str(path))


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


@lru_cache(maxsize=1)
def _model(detection_model, recognition_model, engine):
    from paddleocr import PaddleOCR
    return PaddleOCR(
        text_detection_model_name=detection_model,
        text_recognition_model_name=recognition_model,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine=engine,
    )

def _run(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    detection_model = str(payload.get("detection_model", "PP-OCRv6_tiny_det"))
    recognition_model = str(payload.get("recognition_model", "PP-OCRv6_tiny_rec"))
    engine = str(payload.get("engine", "onnxruntime"))
    ocr = _model(detection_model, recognition_model, engine)
    images = []
    for image_path in payload.get("images", []):
        resolved_path = str(Path(image_path).resolve())
        predictions = list(ocr.predict(resolved_path))
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
        "detection_model": detection_model,
        "recognition_model": recognition_model,
        "engine": engine,
    }


def _serve():
    # Model logs may use stdout; responses use atomic files instead.
    for line in sys.stdin:
        request = json.loads(line)
        response_path = Path(request["response"])
        try:
            payload = json.loads(Path(request["request"]).read_text(encoding="utf-8"))
            _write_json(response_path, {"success": True, "result": _run(payload)})
        except Exception as exc:
            _write_json(
                response_path,
                {
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                },
            )
    return 0


def main() -> int:
    if "--serve" in sys.argv:
        return _serve()
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    response_path = Path(args.response)
    try:
        payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
        _write_json(response_path, {"success": True, "result": _run(payload)})
        return 0
    except Exception as exc:
        _write_json(
            response_path,
            {
                "success": False,
                "error": "{}: {}\n{}".format(
                    type(exc).__name__, exc, traceback.format_exc()
                ),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
