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


def test_rejects_digit_or_latin_corruption_even_when_cjk_similarity_is_high(correct_asr):
    for asr, ocr in [
        ("前几天我网上冲浪刷到了这种90年代的绿色桌面", "前几天我网上冲浪刷到了这种9日年代的绿色桌面"),
        ("无论纸片什么形状尺寸都能美美展示", "无论纸片什么形状R寸都能美美展示"),
    ]:
        segment = SimpleNamespace(content=asr)
        correct_asr(segment, [{"text": ocr, "prob": .99, "sample_time": .5}], None, 0, 1)
        assert segment.content == asr


def test_rejects_multiple_unconfirmed_homophone_changes(correct_asr):
    segment = SimpleNamespace(content="这样子不占空间不会损伤纸片")
    row = {"text": "这释子不占空间，不会损份纸片", "prob": .99, "sample_time": .5}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == "这样子不占空间不会损伤纸片"


def test_accepts_one_cjk_homophone_with_one_clear_caption_frame(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    row = {"text": "今天我们学习做饭", "prob": .99, "sample_time": .5}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == "今天我们学习做饭"


def test_does_not_use_best_row_outside_cue(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    row = {"text": "今天我们学习做饭", "prob": .99, "sample_time": 3.0}
    correct_asr(segment, [], row, 0, 1)
    assert segment.content == "今天我们学习做反"


@pytest.mark.parametrize("times", [[.5, .5], [.5, .5001], [.5, .6]])
def test_duplicate_or_nearby_frames_cannot_confirm_two_glyphs(correct_asr, times):
    segment = SimpleNamespace(content="无论纸片什么形状尺寸都能眉眉展示")
    rows = [{"text": "无论纸片什么形状尺寸都能美美展示", "prob": .97, "sample_time": t}
            for t in times]
    correct_asr(segment, rows, rows[0], 0, 1)
    assert segment.content == "无论纸片什么形状尺寸都能眉眉展示"
    assert segment.ocr_text_review["reason"] == "multiple_glyphs_without_consensus"


def test_independent_caption_consensus_repairs_only_cjk_and_retains_source_format(correct_asr):
    source = "今天用2张DIY纸片，什么形状尺寸都能眉眉展示！"
    segment = SimpleNamespace(content=source)
    rows = [{"text": "今天用2张DIY纸片什么形状尺寸都能美美展示", "prob": .97, "sample_time": t}
            for t in [.2, .8]]
    correct_asr(segment, rows, rows[0], 0, 1)
    assert segment.content == "今天用2张DIY纸片，什么形状尺寸都能美美展示！"
    assert segment.ocr_text_review["asr_text"] == source
    assert segment.ocr_text_review["sample_times"] == [.2, .8]


def test_partial_homophone_caption_cannot_remove_prefix_or_suffix(correct_asr):
    source = "今天我们学习做反然后准备材料"
    segment = SimpleNamespace(content=source)
    row = {"text": "我们学习做饭然后准备材料", "prob": .99, "sample_time": .5}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == source


def test_out_of_cue_selected_rows_do_not_fall_back_to_best_row(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    outside = {"text": "今天我们学习做饭", "prob": .99, "sample_time": 1.01}
    best = dict(outside, sample_time=.5)
    correct_asr(segment, [outside], best, 0, 1)
    assert segment.content == "今天我们学习做反"


def test_missing_timestamp_does_not_default_to_cue_start(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    row = {"text": "今天我们学习做饭", "prob": .99}
    correct_asr(segment, [row], row, 0, 1)
    assert segment.content == "今天我们学习做反"


def test_zero_timestamp_is_not_replaced_with_nonzero_cue_start(correct_asr):
    segment = SimpleNamespace(content="今天我们学习做反")
    row = {"text": "今天我们学习做饭", "prob": .99, "sample_time": 0.0}
    correct_asr(segment, [row], row, 1, 2)
    assert segment.content == "今天我们学习做反"


def test_repeated_noisy_latin_caption_still_cannot_replace_cjk(correct_asr):
    source = "无论纸片什么形状尺寸都能眉眉展示"
    segment = SimpleNamespace(content=source)
    rows = [{"text": "无论纸片什么形状R寸都能美美展示", "prob": .97, "sample_time": t}
            for t in [.2, .8]]
    correct_asr(segment, rows, rows[0], 0, 1)
    assert segment.content == source
    assert segment.ocr_text_review["reason"] == "caption_changes_non_cjk"
