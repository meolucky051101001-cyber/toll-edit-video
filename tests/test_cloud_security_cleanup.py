from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.cloud import cloud_config, colab_bridge
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_orchestrator import (
    _build_gemini_env_vars,
    _resolve_gemini_key_and_secrets,
)
from backend.cloud.cloud_security import redact_secrets


def test_external_env_fallback_requires_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    local_env = tmp_path / "local.env"
    external_env = tmp_path / "external.env"
    external_env.write_text("AUTODUB_TEST_EXTERNAL_ENV=loaded-from-external\n", encoding="utf-8")
    monkeypatch.setattr(cloud_config, "_LOCAL_ENV_PATH", local_env)
    monkeypatch.setattr(cloud_config, "_EXTERNAL_ENV_PATH", external_env)
    monkeypatch.delenv("AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK", raising=False)
    monkeypatch.delenv("AUTODUB_TEST_EXTERNAL_ENV", raising=False)

    with caplog.at_level(logging.WARNING, logger="backend.cloud.cloud_config"):
        cloud_config._load_environment()

    assert "AUTODUB_TEST_EXTERNAL_ENV" not in os.environ
    warnings = [record.message for record in caplog.records if "Bỏ qua file .env bên ngoài" in record.message]
    assert len(warnings) == 1

    monkeypatch.setenv("AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK", "1")
    cloud_config._load_environment()

    assert os.environ["AUTODUB_TEST_EXTERNAL_ENV"] == "loaded-from-external"
    assert len([record for record in caplog.records if "Bỏ qua file .env bên ngoài" in record.message]) == 1


def test_remote_gemini_keys_are_capped_and_redaction_keeps_full_pool(
    monkeypatch: pytest.MonkeyPatch,
):
    primary = "synthetic-primary-key-123456"
    secondary = "synthetic-secondary-key-234567"
    third = "synthetic-third-key-345678"
    monkeypatch.setenv("GEMINI_API_KEY", primary)
    monkeypatch.setenv("GEMINI_API_KEYS", f"{primary},{secondary},{third}")
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_gemini_keys_max", 2)

    resolved_primary, extra_secrets = _resolve_gemini_key_and_secrets()
    env_vars = _build_gemini_env_vars(resolved_primary, extra_secrets)

    assert resolved_primary == primary
    assert extra_secrets == (primary, secondary, third)
    assert env_vars == {
        "GEMINI_API_KEY": primary,
        "GEMINI_API_KEYS": f"{primary},{secondary}",
    }
    assert third not in redact_secrets(f"failure: {third}", extra_secrets=extra_secrets)

    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_gemini_keys_max", 1)
    capped_env = _build_gemini_env_vars(resolved_primary, extra_secrets)
    assert capped_env == {"GEMINI_API_KEY": primary}


@pytest.mark.parametrize(
    ("return_code", "output", "expected"),
    [(0, "SyntaxError is part of the remote output", True), (1, "remote execution failed", False)],
)
def test_exec_script_result_uses_return_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    output: str,
    expected: bool,
):
    script = tmp_path / "remote.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(
        colab_bridge,
        "_run_wsl_colab_argv",
        lambda *_args, **_kwargs: (return_code, output, ""),
    )

    assert colab_bridge.exec_script("cjobsecuritytest", script) == (expected, output)


@pytest.mark.parametrize(
    ("return_code", "output", "expected"),
    [(0, "SyntaxError is part of the remote output", True), (1, "remote execution failed", False)],
)
def test_exec_argv_result_uses_return_code(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    output: str,
    expected: bool,
):
    monkeypatch.setattr(colab_bridge, "_write_wsl_temp_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(colab_bridge, "_remove_wsl_temp_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        colab_bridge,
        "_run_wsl_colab_argv",
        lambda *_args, **_kwargs: (return_code, output, ""),
    )

    assert colab_bridge.exec_argv("cjobsecuritytest", ["python3", "-c", "pass"]) == (expected, output)


@pytest.mark.parametrize(
    ("return_code", "output", "expected"),
    [(0, "SyntaxError is part of the remote output", True), (1, "remote execution failed", False)],
)
def test_monitored_result_uses_return_code(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    output: str,
    expected: bool,
):
    monkeypatch.setattr(colab_bridge, "_write_wsl_temp_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(colab_bridge, "_remove_wsl_temp_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(colab_bridge, "_wait_interruptible", lambda *_args, **_kwargs: False)

    def fake_run(colab_args, **_kwargs):
        args = list(colab_args)
        script_path = args[args.index("-f") + 1]
        if "colab_launch_" in script_path:
            return 0, 'AUTODUB_LAUNCH_JSON:{"launched": true, "sup_pid": 4321}', ""
        payload = {"done": True, "returncode": return_code, "log_tail": output}
        return 0, "AUTODUB_POLL_JSON:" + json.dumps(payload), ""

    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", fake_run)

    ok, final_output = colab_bridge.run_argv_monitored(
        "cjobsecuritytest",
        ["python3", "pipeline.py"],
        timeout=30,
        poll_interval_seconds=0.5,
    )

    assert (ok, final_output) == (expected, output)
