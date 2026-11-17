"""Explicit runtime paths must survive API imports and shared default loading."""
import os
from pathlib import Path
import subprocess
import sys

from backend.environment import load_environment


def test_dotenv_defaults_do_not_replace_explicit_process_paths(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        'AUTODUB_WORKSPACE="live-workspace"\nAUTODUB_OUTPUT_DIR=live-output\n'
        'REVIEW_DEFAULT_VALUE=from-file\n', encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_WORKSPACE", "isolated-workspace")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", "isolated-output")
    monkeypatch.delenv("REVIEW_DEFAULT_VALUE", raising=False)
    load_environment(tmp_path)
    assert os.environ["AUTODUB_WORKSPACE"] == "isolated-workspace"
    assert os.environ["AUTODUB_OUTPUT_DIR"] == "isolated-output"
    assert os.environ["REVIEW_DEFAULT_VALUE"] == "from-file"
    monkeypatch.delenv("REVIEW_DEFAULT_VALUE")


def test_api_import_keeps_all_mutable_stores_in_explicit_workspace(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    env = dict(os.environ, AUTODUB_WORKSPACE=str(workspace),
               TOOL_V1_CONTROL_DIR=str(workspace / "control"),
               AUTODUB_CONTROL_DIR=str(workspace / "control"),
               TOOL_V2_RENDER_HISTORY=str(workspace / "bot_system/history.json"),
               AUTODUB_INPUT_DIR=str(tmp_path / "input"),
               AUTODUB_OUTPUT_DIR=str(tmp_path / "output"),
               AUTODUB_LOCAL_TOKEN="synthetic-local-review-token")
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'backend'))
expected = Path(os.environ['AUTODUB_WORKSPACE']).resolve()
import main, audio_settings, render_history, notification_service
assert Path(os.environ['AUTODUB_WORKSPACE']).resolve() == expected
assert Path(main.WORKSPACE).resolve() == expected
assert main.PATHS.workspace == expected
for path in (audio_settings.SETTINGS_FILE, render_history.HISTORY_FILE,
             notification_service.NOTIF_FILE):
    assert Path(path).resolve().is_relative_to(expected), str(path)
print('explicit workspace preserved')
"""
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script],
                            cwd=root, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=45)
    # Keep provider/environment values out of assertion output.
    assert result.returncode == 0, result.stderr[-3000:]
    assert "explicit workspace preserved" in result.stdout
