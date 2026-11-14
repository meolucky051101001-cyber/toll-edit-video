"""Exercise text repair after OCR band selection, without inference or GPU use."""
import ast
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def correct_asr():
    # The repair helper is nested in the geometry pipeline. Extract the actual
    # functions so selected caption text can be tested independently of layout.
    source = Path(__file__).resolve().parents[1] / "backend" / "ocr_utils.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_cjk_only", "_maybe_correct_asr_from_ocr"}
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(functions) == 2
    scope = {"_re": re, "_SequenceMatcher": SequenceMatcher, "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
    return scope["_maybe_correct_asr_from_ocr"]


@pytest.mark.parametrize("asr,ocr,expected", [
    ("今天我们介绍树木", "欢迎关注我的频道", "今天我们介绍树木"),
    ("今天我们学习做反", "今天我们学习做饭", "今天我们学习做饭"),
    ("今天我们学习做饭", "学习做饭", "今天我们学习做饭"),
    ("买得起", "买不起", "买得起"),
    ("今天我们学习做饭", "今天我们学习做饭", "今天我们学习做饭"),
])
def test_selected_caption_cannot_replace_unrelated_or_partial_speech(correct_asr, asr, ocr, expected):
    segment = SimpleNamespace(content=asr)
    row = {"text": ocr, "prob": .99, "sample_time": .5}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == expected


def test_low_confidence_close_caption_keeps_asr(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    row = {"text": "今天我们学习做饭", "prob": .81, "sample_time": .5}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == "今天我们学习做反"


def test_long_multi_caption_fallback_still_requires_matching_speech(correct_asr):
    segment = SimpleNamespace(content="今天我们介绍树木")
    rows = [{"text": text, "prob": .99, "sample_time": t}
            for text, t in [("欢迎关注我的频道", .5), ("点击这里查看更多", .8)]]
    correct_asr(segment, rows, rows[0], 0, 1)
    assert segment.content == "今天我们介绍树木"
