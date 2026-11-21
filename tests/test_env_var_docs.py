import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from config.env import env_flag, env_float, env_int, env_str  # noqa: E402


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_env_docs", ROOT / "scripts" / "gen_env_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_var_docs_are_current():
    assert _generator().main(["--check"]) == 0, (
        "docs/ENV_VARS.md is stale; run: python scripts/gen_env_docs.py"
    )


def test_generator_finds_known_variables():
    found = _generator().collect()
    assert "V1_GPU_LOCALIZED_BLUR" in found
    assert "AUTODUB_WORKSPACE" in found
    assert found["V1_GPU_LOCALIZED_BLUR"]["defaults"] == {"True"}


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, True, True),
        (None, False, False),
        ("0", True, False),
        (" OFF ", True, False),
        ("anything", True, True),
        ("1", False, True),
        (" Yes ", False, True),
        ("anything", False, False),
    ],
)
def test_env_flag(monkeypatch, value, default, expected):
    if value is None:
        monkeypatch.delenv("X_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_TEST_FLAG", value)
    assert env_flag("X_TEST_FLAG", default) is expected


def test_env_numbers_and_strings(monkeypatch):
    monkeypatch.setenv("X_TEST_NUMBER", " 7 ")
    assert env_int("X_TEST_NUMBER", 1) == 7
    assert env_float("X_TEST_NUMBER", 1.0) == 7.0
    monkeypatch.setenv("X_TEST_NUMBER", "abc")
    assert env_int("X_TEST_NUMBER", 1) == 1
    assert env_float("X_TEST_NUMBER", 1.5) == 1.5
    monkeypatch.setenv("X_TEST_TEXT", "  hi ")
    assert env_str("X_TEST_TEXT") == "hi"
    monkeypatch.delenv("X_TEST_TEXT")
    assert env_str("X_TEST_TEXT", "d") == "d"
