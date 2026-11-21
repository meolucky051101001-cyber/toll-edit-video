from pathlib import Path

from backend import control_token


TOKEN_NAME = ".dashboard_control_token"


def test_environment_token_takes_precedence(tmp_path, monkeypatch):
    token_file = tmp_path / "bot_system" / TOKEN_NAME
    token_file.parent.mkdir()
    token_file.write_text("file-control-token-123", encoding="utf-8")
    monkeypatch.setenv("AUTODUB_LOCAL_TOKEN", "environment-token")

    assert control_token.read_control_token(tmp_path) == "environment-token"


def test_candidate_paths_keep_shared_search_order(tmp_path):
    paths = control_token.candidate_token_paths(tmp_path)

    assert paths[:2] == [
        tmp_path / "bot_system" / TOKEN_NAME,
        tmp_path / TOKEN_NAME,
    ]
    assert paths[2:] == [
        Path(control_token.__file__).resolve().parent.parent
        / "workspace_backup_safety" / TOKEN_NAME,
        Path(control_token.__file__).resolve().parent / TOKEN_NAME,
    ]


def test_short_token_is_skipped_for_later_valid_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTODUB_LOCAL_TOKEN", raising=False)
    first, second, *_ = control_token.candidate_token_paths(tmp_path)
    first.parent.mkdir(parents=True)
    first.write_text("short", encoding="utf-8")
    second.write_text("valid-control-token", encoding="utf-8")

    assert control_token.read_control_token(tmp_path) == "valid-control-token"


def test_unreadable_candidate_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTODUB_LOCAL_TOKEN", raising=False)
    unreadable, valid, *_ = control_token.candidate_token_paths(tmp_path)
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("unreadable-token-file", encoding="utf-8")
    valid.write_text("valid-control-token", encoding="utf-8")
    read_text = Path.read_text

    def read_with_unreadable_candidate(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("fixture unreadable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_with_unreadable_candidate)

    assert control_token.read_control_token(tmp_path) == "valid-control-token"


def test_workflow_api_resolves_token_from_bot_system(tmp_path, monkeypatch):
    from backend import workflow_api

    token_path = tmp_path / "bot_system" / TOKEN_NAME
    token_path.parent.mkdir()
    token_path.write_text("workflow-control-token", encoding="utf-8")
    monkeypatch.delenv("AUTODUB_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr(workflow_api, "WORKSPACE", tmp_path)

    assert workflow_api.get_current_control_token() == "workflow-control-token"
