"""Architectural regression tests for the 2026-11-18 V1 modularization & hygiene refactor."""
from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_zero_utf8_bom_and_valid_ast_across_tracked_python_files():
    tracked = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        text=True,
        encoding="utf-8",
    ).splitlines()
    assert len(tracked) > 100
    bom_offenders = []
    syntax_offenders = []
    for rel in tracked:
        fp = REPO_ROOT / rel
        if not fp.is_file():
            continue
        raw = fp.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            bom_offenders.append(rel)
        try:
            ast.parse(raw.decode("utf-8"), filename=rel)
        except SyntaxError as exc:
            syntax_offenders.append((rel, exc.lineno, exc.msg))
    assert bom_offenders == []
    assert syntax_offenders == []


def test_singleton_stateful_modules_never_split_brain_across_import_styles():
    backend_dir = str(REPO_ROOT / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    for name in ("shared_state", "batch_control", "v1_gpu_gatekeeper", "job_tracker", "ocr_track_coalescer"):
        short_mod = importlib.import_module(name)
        pkg_mod = importlib.import_module(f"backend.{name}")
        assert short_mod is pkg_mod, f"Split-brain module instances detected for {name}"


def test_v1_orchestrator_contains_zero_runtime_importlib_reloads():
    src = (REPO_ROOT / "backend" / "v1_orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="backend/v1_orchestrator.py")
    reloads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reload"
    ]
    assert reloads == []


def test_environment_merges_root_dotenv_fallback_when_loading_from_backend(tmp_path):
    from backend.environment import read_environment

    proj = tmp_path / "proj"
    bdir = proj / "backend"
    bdir.mkdir(parents=True)
    (proj / ".env").write_text("ROOT_ONLY_KEY=root_val\nSHARED_KEY=from_root\n", encoding="utf-8")
    (bdir / ".env").write_text("BACKEND_ONLY_KEY=backend_val\nSHARED_KEY=from_backend\n", encoding="utf-8")

    merged = read_environment(bdir, {"PROCESS_KEY": "proc_val", "SHARED_KEY": "from_proc"})
    assert merged["PROCESS_KEY"] == "proc_val"
    assert merged["SHARED_KEY"] == "from_proc"
    assert merged["BACKEND_ONLY_KEY"] == "backend_val"
    assert merged["ROOT_ONLY_KEY"] == "root_val"

    merged_no_proc = read_environment(bdir, {})
    assert merged_no_proc["SHARED_KEY"] == "from_backend"
    assert merged_no_proc["ROOT_ONLY_KEY"] == "root_val"


def test_ocr_track_coalescer_reexports_match_ocr_utils():
    from backend import ocr_track_coalescer as kernel
    from backend import ocr_utils as ocr

    for symbol in (
        "_merge_colinear_frame_blocks",
        "_coalesce_stationary_source_tracks",
        "_bridge_source_caption_gaps",
        "_attach_source_caption_tracks",
        "normalize_visual_speech_timing",
        "stabilize_samples",
    ):
        assert getattr(ocr, symbol) is getattr(kernel, symbol)
