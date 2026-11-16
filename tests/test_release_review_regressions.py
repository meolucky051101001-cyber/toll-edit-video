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


def test_cloud_release_requires_the_pinned_wheel_and_standalone_runner():
    config = "backend/cloud/cloud_config.py"
    assert all(pkg.should_include(name) for name in pkg.CLOUD_RELEASE_FILES)
    assert not pkg.should_include("backend/cloud/gpu_scripts/unpinned.whl")
    assert not pkg.should_include("backend/cloud/results/job/output_manifest.json")
    with pytest.raises(RuntimeError, match="capcut_tts_api"):
        pkg.verify_required_files([config], required_files=())
    pkg.verify_required_files([config, *pkg.CLOUD_RELEASE_FILES], required_files=())
