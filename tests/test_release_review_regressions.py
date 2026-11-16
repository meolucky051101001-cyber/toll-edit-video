"""Release policy negative cases; all inventories and file contents are fixtures."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import package_v1_release as pkg
from scripts import generate_release_manifest as manifest


def source(tmp_path, value):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/config.py").write_text(value, encoding="utf-8")
    return [Path("backend/config.py")]


def test_zip_and_manifest_reject_same_literal_without_dotenv(tmp_path, capsys):
    literal = "z" * 30
    files = source(tmp_path, f'API_KEY = "{literal}"\n')
    assert pkg.check_secrets_hygiene is manifest.check_secrets_hygiene
    assert manifest.check_secrets_hygiene(tmp_path, files)
    with pytest.raises(RuntimeError, match="Credential validation"):
        pkg.create_release_zip(tmp_path, tmp_path / "out.zip", dry_run=True, required_files=())
    with pytest.raises(ValueError, match="sensitive data"):
        manifest.generate_manifest(tmp_path, dry_run=True)
    assert literal not in capsys.readouterr().out
    assert not (tmp_path / "out.zip").exists()


def test_configured_secret_is_rejected_even_without_literal_assignment(tmp_path):
    value = "configured-" + "z" * 30
    files = source(tmp_path, f'print("{value}")\n')
    (tmp_path / "backend/.env").write_text(f'API_KEY={value}\n', encoding="utf-8")
    assert pkg.check_secrets_hygiene(tmp_path, files)


def test_test_only_dummy_credentials_are_allowed(tmp_path):
    (tmp_path / "tests").mkdir()
    path = Path("tests/test_fixture.py")
    (tmp_path / path).write_text('API_KEY="dummy_' + "z" * 30 + '"\n', encoding="utf-8")
    assert pkg.check_secrets_hygiene(tmp_path, [path]) == []


def test_unreadable_release_file_fails_validation(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot inspect release file"):
        pkg.check_secrets_hygiene(tmp_path, [Path("backend/missing.py")])


@pytest.mark.parametrize("failure", [SimpleNamespace(returncode=128, stdout=""), OSError("git unavailable")])
def test_git_failure_never_packages_untracked_fallback(tmp_path, failure):
    source(tmp_path, "# untracked local data\n")
    (tmp_path / ".git").mkdir()
    kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    with patch.object(pkg.subprocess, "run", **kwargs):
        with pytest.raises(RuntimeError, match="refusing filesystem fallback"):
            pkg.iter_release_files(tmp_path)


def test_empty_git_inventory_stays_empty(tmp_path):
    source(tmp_path, "# untracked\n")
    (tmp_path / ".git").mkdir()
    with patch.object(pkg.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
        assert pkg.iter_release_files(tmp_path) == []


def test_source_export_without_git_remains_supported(tmp_path):
    files = source(tmp_path, "# exported source\n")
    assert pkg.iter_release_files(tmp_path) == files


@pytest.mark.parametrize('missing', ['backend/ai/v1_caption_evidence.py', 'backend/visual_caption_timing.py'])
def test_missing_required_caption_module_blocks_zip_and_manifest(tmp_path, missing):
    for relative in pkg.REQUIRED_RELEASE_FILES:
        if relative == missing:
            continue
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=Path(missing).stem):
        pkg.create_release_zip(tmp_path, tmp_path / "release.zip", dry_run=True)
    with pytest.raises(RuntimeError, match=Path(missing).stem):
        manifest.generate_manifest(tmp_path, dry_run=True)
    assert not (tmp_path / "release.zip").exists()
    assert not (tmp_path / "RELEASE_MANIFEST.sha256").exists()
