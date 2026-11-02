"""Regression tests for Gói C3: dead OCR branches, obsolete translation state, and hold module contracts."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_ocr_utils_has_no_unreachable_easyocr_or_best_cluster_samples():
    source_path = BACKEND_DIR / "ocr_utils.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    func_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "_best_cluster_samples" not in func_names

    run_paddle = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_recognize_batch"
    )
    assert isinstance(run_paddle.body[-1], ast.Raise)
    # Ensure no statements follow a Raise inside any block of _recognize_batch
    for node in ast.walk(run_paddle):
        for attr in ("body", "orelse", "finalbody"):
            stmts = getattr(node, attr, None)
            if not isinstance(stmts, list):
                continue
            for idx, stmt in enumerate(stmts[:-1]):
                assert not isinstance(stmt, (ast.Raise, ast.Return)), (
                    f"Unreachable statement after {type(stmt).__name__} in _recognize_batch at line {stmt.lineno}"
                )


def test_recognize_batch_fails_closed_when_gpu_ocr_unavailable():
    import ocr_utils

    with (
        patch.object(ocr_utils, "runtime_module_available", return_value=False),
        patch.object(ocr_utils, "_paddle_failed", False),
    ):
        with pytest.raises(RuntimeError, match="V1 GPU OCR unavailable; CPU EasyOCR fallback disabled"):
            ocr_utils._recognize_batch([b"fake_frame"])


def test_translation_removes_obsolete_transient_failure_and_unread_locals():
    source_path = BACKEND_DIR / "ai" / "translation.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    func_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "_gemini_transient_failure" not in func_names

    translate_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "translate_with_gemini"
    )
    assigned_names = set()
    for node in ast.walk(translate_fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned_names.add(target.id)
    assert "models_to_try" not in assigned_names
    assert "response" not in assigned_names


def test_translate_with_gemini_cache_hit_does_not_mutate_legacy_last_good():
    from backend.ai import translation

    translation._gemini_last_good.clear()
    fake_cached = {"model": "gemini-2.5-flash", "texts": ["Xin chào"]}

    with patch("backend.ai.v1_translation_cache.read_cache", return_value=fake_cached):
        result = translation.translate_with_gemini(["你好"], target_lang="vi", api_key="dummy-key")

    assert result == ["Xin chào"]
    assert translation._gemini_last_good == {}
