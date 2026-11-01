# -*- coding: utf-8 -*-
"""F7 — Học từ sửa tay: ghi nhận cặp "gốc → bản sửa" khi người dùng sửa câu trong
Segment Editor, nhóm lại và gợi ý thêm vào Custom Glossary khi lặp lại >= MIN_COUNT lần.

Một cặp chỉ được ghi khi diff word-level có đúng MỘT vùng thay đổi ngắn (<= MAX_TOKENS từ)
— sửa nhiều chỗ cùng lúc hoặc viết lại cả câu được coi là chỉnh văn bản, không phải thuật ngữ.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("glossary_learning_service")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
CONTROL_DIR = WORKSPACE / "control"
SUGGEST_FILE = CONTROL_DIR / "glossary_suggestions.json"

MIN_COUNT = 3        # số lần lặp tối thiểu để xuất hiện trong danh sách gợi ý
MAX_TOKENS = 6       # vùng đổi dài hơn -> viết lại cả câu, bỏ qua
MAX_SUGGESTIONS = 200


def _tokenize(text: str) -> List[str]:
    return re.findall(r"\S+", str(text or "").strip())


def _key(before: str, after: str) -> str:
    return f"{str(before).strip().lower()}|{str(after).strip().lower()}"


def extract_edit_pair(before: str, after: str) -> Optional[Dict[str, str]]:
    """Trích cặp thay đổi mức từ nếu diff có đúng 1 vùng đổi ngắn, ngược lại trả None."""
    a, b = _tokenize(before), _tokenize(after)
    if not a or not b or a == b:
        return None
    ops = [op for op in SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if len(ops) != 1:
        return None
    tag, i1, i2, j1, j2 = ops[0]
    if tag == "insert":
        old_span, new_span = "", " ".join(b[j1:j2])
    elif tag == "delete":
        old_span, new_span = " ".join(a[i1:i2]), ""
    else:
        old_span, new_span = " ".join(a[i1:i2]), " ".join(b[j1:j2])
    if not old_span and not new_span:
        return None
    if max(len(a[i1:i2]), len(b[j1:j2])) > MAX_TOKENS:
        return None
    if len(old_span.strip()) < 2 and len(new_span.strip()) < 2:
        return None
    return {"before": old_span.strip(), "after": new_span.strip()}


def _load() -> Dict[str, Any]:
    try:
        if SUGGEST_FILE.is_file():
            data = json.loads(SUGGEST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("suggestions", {})
                data.setdefault("dismissed", {})
                return data
    except Exception as exc:
        logger.warning("glossary_suggestions.json hỏng, tạo lại: %s", exc)
    return {"suggestions": {}, "dismissed": {}}


def _save(data: Dict[str, Any]) -> None:
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SUGGEST_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SUGGEST_FILE)
    except Exception as exc:
        logger.error("Không thể ghi glossary_suggestions.json: %s", exc)


def record_edit(before_text: str, after_text: str, video_name: str = "") -> Optional[Dict[str, Any]]:
    pair = extract_edit_pair(before_text, after_text)
    if not pair:
        return None
    key = _key(pair["before"], pair["after"])
    data = _load()
    sugg: Dict[str, Any] = data["suggestions"]
    item = sugg.get(key) or {
        "before": pair["before"],
        "after": pair["after"],
        "count": 0,
        "samples": [],
        "first_seen": time.time(),
    }
    item["count"] = int(item.get("count", 0)) + 1
    item["last_seen"] = time.time()
    if video_name and video_name not in item.get("samples", []):
        item["samples"] = (item.get("samples", []) + [str(video_name)[:120]])[:5]
    sugg[key] = item
    if len(sugg) > MAX_SUGGESTIONS:
        for stale in sorted(sugg, key=lambda k: sugg[k].get("count", 0))[: len(sugg) - MAX_SUGGESTIONS]:
            sugg.pop(stale, None)
    _save(data)
    return item


def get_suggestions(min_count: int = MIN_COUNT) -> List[Dict[str, Any]]:
    data = _load()
    dismissed = data.get("dismissed", {})
    items = [
        dict(v, id=k)
        for k, v in data["suggestions"].items()
        if int(v.get("count", 0)) >= max(1, min_count) and k not in dismissed
    ]
    items.sort(key=lambda x: (-int(x.get("count", 0)), -float(x.get("last_seen") or 0)))
    return items[:50]


def accept_suggestion(before: str, after: str) -> Dict[str, Any]:
    """Thêm cặp vào Custom Glossary (qua ai.translation.save_custom_glossary) và gỡ khỏi gợi ý."""
    from ai.translation import save_custom_glossary

    before_c = str(before or "").strip()
    after_c = str(after or "").strip()
    if not before_c or not after_c:
        raise ValueError("Cặp thuật ngữ không hợp lệ")
    save_custom_glossary(entries={before_c: after_c}, merge=True, workspace=WORKSPACE)
    data = _load()
    key = _key(before_c, after_c)
    data["suggestions"].pop(key, None)
    data.setdefault("dismissed", {})[key] = time.time()
    _save(data)
    return {"added": True, "before": before_c, "after": after_c}


def dismiss_suggestion(before: str, after: str) -> Dict[str, Any]:
    data = _load()
    key = _key(str(before or ""), str(after or ""))
    data["suggestions"].pop(key, None)
    data.setdefault("dismissed", {})[key] = time.time()
    _save(data)
    return {"dismissed": True}
