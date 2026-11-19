"""Regression from the real Hatsune Miku acceptance clip; preserve strict count checking."""
import pytest
from ai.v1_translation_meaning import translation_meaning_issues

SOURCE="有不见材料和一只瓷吸光头初音"

@pytest.mark.parametrize("target",[
    "Có bộ nguyên liệu và một bé Miku đầu hói gắn nam châm",
    "Có set nguyên liệu và một bé Miku đầu hói hít nam châm",
    "Có nguyên liệu và 1 bé Miku",
    "Có set nguyên liệu và một em Miku đầu trọc hít nam châm",
    "Cùng với một em Miku đầu hói có nam châm",
])
def test_vietnamese_be_counter_preserves_one_toy(target):
    assert translation_meaning_issues(SOURCE,target)==[]

@pytest.mark.parametrize("target",[
    "Có nguyên liệu và hai bé Miku",
    "Có nguyên liệu và 3 bé Miku",
    "Có nguyên liệu và hai em Miku",
    "Có nguyên liệu và bé Miku",
])
def test_missing_or_changed_toy_count_is_still_rejected(target):
    assert "sai số lượng, số đo hoặc đơn vị" in translation_meaning_issues(SOURCE,target)

def test_measurement_guard_is_not_weakened_by_new_counter():
    assert translation_meaning_issues("两个公仔高三厘米","Hai bé búp bê cao ba xăng ti mét")==[]
    assert translation_meaning_issues("两个公仔高三厘米","Hai bé búp bê cao bốn xăng ti mét")
