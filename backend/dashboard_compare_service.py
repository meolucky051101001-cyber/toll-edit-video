"""Resolve compare sources from verified delivery receipts or unique legacy names."""
import json
from pathlib import Path
from urllib.parse import quote

from batch_plan_service import bare_name, fingerprint, VIDEO_EXTENSIONS


def resolve_compare(input_root, output_root, filename):
    filename = bare_name(filename)
    input_root, output_root = Path(input_root).resolve(), Path(output_root).resolve()
    output = output_root / filename
    if output.resolve().parent != output_root or not output.is_file() or output.suffix.lower() not in VIDEO_EXTENSIONS:
        raise FileNotFoundError(filename)
    # Support the established batch receipt naming without scanning workspaces.
    from batch_processor import _receipt_path
    receipt_path = _receipt_path(output)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    candidates = []
    if receipt.get("source_path"):
        candidate = Path(receipt["source_path"]).resolve()
        if candidate.parent in {input_root, input_root / "processed"} and candidate.is_file():
            candidates.append(candidate)
    source_name = receipt.get("source_name")
    if source_name:
        source_name = bare_name(source_name)
        candidates.extend(root / source_name for root in (input_root, input_root / "processed") if (root / source_name).is_file())
    if not candidates:
        stem = output.stem[7:] if output.stem.startswith("Dubbed_") else output.stem
        for root in (input_root, input_root / "processed"):
            if root.is_dir():
                candidates.extend(p for p in root.iterdir() if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_EXTENSIONS)
    candidates = list(dict.fromkeys(p.resolve() for p in candidates if p.resolve().parent in {input_root, input_root / "processed"}))
    expected = receipt.get("input_sha256")
    if expected:
        candidates = [p for p in candidates if fingerprint(p)["sha256"] == expected]
    original = candidates[0] if len(candidates) == 1 else None
    reason = "" if original else ("Có nhiều nguồn phù hợp; cần xác minh receipt." if candidates else "Không còn nguồn phù hợp với thành phẩm.")
    return output, original, reason


def compare_response(input_root, output_root, filename):
    output, original, reason = resolve_compare(input_root, output_root, filename)
    return {"dubbed": {"name": output.name, "url": "/api/media/compare-dubbed?filename=" + quote(output.name, safe="")},
            "original": {"name": original.name, "url": "/api/media/compare-original?filename=" + quote(output.name, safe="")} if original else None,
            "reason": reason}
