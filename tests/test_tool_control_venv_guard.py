"""tool_control._venv_matches_interpreter unit tests."""
import tool_control


def _write_cfg(venv, text):
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(text, encoding="utf-8")


def test_matching_version_returns_true(tmp_path):
    venv = tmp_path / "venv"
    _write_cfg(venv, "home = C:\\py\nversion = 3.10.11\n")
    assert tool_control._venv_matches_interpreter(venv, (3, 10, 11)) is True


def test_minor_mismatch_returns_false(tmp_path):
    venv = tmp_path / "venv"
    _write_cfg(venv, "version = 3.12.14\n")
    assert tool_control._venv_matches_interpreter(venv, (3, 10, 11)) is False


def test_version_info_key_is_accepted(tmp_path):
    venv = tmp_path / "venv"
    _write_cfg(venv, "version_info = 3.12.14.final.0\n")
    assert tool_control._venv_matches_interpreter(venv, (3, 12, 14)) is True


def test_missing_cfg_returns_false(tmp_path):
    assert tool_control._venv_matches_interpreter(tmp_path / "nope", (3, 10, 11)) is False


def test_garbage_cfg_returns_false(tmp_path):
    venv = tmp_path / "venv"
    _write_cfg(venv, "not a cfg file\nversion = abc\n")
    assert tool_control._venv_matches_interpreter(venv, (3, 12, 0)) is False


def test_live_venv_matches_host_interpreter():
    import sys
    from pathlib import Path
    venv = Path(tool_control.__file__).resolve().parent / "venv"
    if venv.is_dir():
        assert tool_control._venv_matches_interpreter(venv) == (
            sys.version_info[:2] == (3, 12)
        )
