from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("use_popen", [False, True])
def test_wsl_argv_uses_exec_and_passes_arguments_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    use_popen: bool,
):
    raw_args = ["folder with spaces/file's $name.mp4", "$HOME", "don't-expand"]
    calls = []

    if use_popen:
        class FakePopen:
            def __init__(self, argv, **_kwargs):
                calls.append(list(argv))
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.stdin = None
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        monkeypatch.setattr(colab_bridge.subprocess, "Popen", FakePopen)
        result = colab_bridge._run_wsl_argv(
            "python3",
            raw_args,
            distro="Ubuntu",
            user="test-user",
            cancel_check=lambda: False,
        )
    else:
        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(colab_bridge.subprocess, "run", fake_run)
        result = colab_bridge._run_wsl_argv(
            "python3",
            raw_args,
            distro="Ubuntu",
            user="test-user",
        )

    assert result == (0, "", "")
    assert calls == [[
        "wsl",
        "-d",
        "Ubuntu",
        "-u",
        "test-user",
        "--exec",
        "python3",
        *raw_args,
    ]]


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


def test_external_env_path_matching_local_path_is_skipped_without_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    local_env = tmp_path / "local.env"
    local_env.write_text("AUTODUB_TEST_SAME_ENV=loaded-locally\n", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    external_env = local_env.parent / "subdir" / ".." / local_env.name
    monkeypatch.setattr(cloud_config, "_LOCAL_ENV_PATH", local_env)
    monkeypatch.setattr(cloud_config, "_EXTERNAL_ENV_PATH", external_env)
    monkeypatch.delenv("AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK", raising=False)

    with caplog.at_level(logging.WARNING, logger="backend.cloud.cloud_config"):
        candidates = cloud_config._env_candidates()

    assert candidates == [(local_env, False)]
    assert not any("Bỏ qua file .env bên ngoài" in record.message for record in caplog.records)


def test_cloud_cache_backend_is_loaded_from_dotenv_before_config_reload(
    tmp_path: Path,
):
    module_name = "cloud_config_reload_test"
    module_dir = tmp_path / "backend" / "cloud"
    module_dir.mkdir(parents=True)
    module_path = module_dir / f"{module_name}.py"
    module_path.write_text(Path(cloud_config.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "backend" / ".env").write_text(
        "AUTODUB_CLOUD_CACHE_BACKEND=gdrive\n",
        encoding="utf-8",
    )
    script = (
        "import importlib, os; "
        f"config = importlib.import_module({module_name!r}); "
        "os.environ.pop('AUTODUB_CLOUD_CACHE_BACKEND', None); "
        "config = importlib.reload(config); "
        "assert config.DEFAULT_CONFIG['cloud_cache_backend'] == 'gdrive'"
    )
    env = os.environ.copy()
    env.pop("AUTODUB_CLOUD_CACHE_BACKEND", None)
    env.pop("AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK", None)
    env["AUTODUB_WORKSPACE"] = str(tmp_path / "workspace")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(module_dir), env.get("PYTHONPATH", "")) if part
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


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

    def fake_run(colab_args, **kwargs):
        script_text = str(kwargs.get("stdin_text") or "")
        if "AUTODUB_LAUNCH_JSON" in script_text:
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
