from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.cloud import cloud_orchestrator, colab_bridge
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.gpu_scripts import setup_env


def _use_temp_model_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(setup_env, "_CLOUD_CACHE_MYDRIVE_ROOT", tmp_path)
    for name in (
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "EASYOCR_MODULE_PATH",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def _prepare_setup_workdir(tmp_path: Path) -> tuple[Path, Path]:
    workdir = tmp_path / "remote"
    workdir.mkdir(parents=True)
    manifest_path = workdir / "cloud_env_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "requirements_file": "requirements_cloud.txt",
                "min_free_disk_mb": 1,
                "gpu_family_patterns": {},
            }
        ),
        encoding="utf-8",
    )
    (workdir / "requirements_cloud.txt").write_text("", encoding="utf-8")
    return workdir, manifest_path


def _cache_key(workdir: Path, manifest_path: Path) -> str:
    manifest_sha = setup_env._sha256_file(manifest_path)
    requirements_sha = setup_env._sha256_file(workdir / "requirements_cloud.txt")
    return hashlib.sha256(
        f"{manifest_sha}:{requirements_sha}:{sys.version.split()[0]}".encode("utf-8")
    ).hexdigest()


def test_warm_reset_preserves_drive_and_nested_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    content_root = tmp_path / "content"
    drive = content_root / "drive"
    nested_mount = content_root / "temporary" / "mounted"
    drive.mkdir(parents=True)
    nested_mount.mkdir(parents=True)
    (drive / "user-file.txt").write_text("keep", encoding="utf-8")
    (nested_mount / "mounted-file.txt").write_text("keep", encoding="utf-8")
    (content_root / "temporary" / "stale.txt").write_text("remove", encoding="utf-8")
    mount_paths = {drive.resolve(), nested_mount.resolve()}
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path,
        "ismount",
        lambda path: Path(path).resolve() in mount_paths or real_ismount(path),
    )

    exec(cloud_orchestrator._build_warm_reset_script(str(content_root)), {})

    assert (drive / "user-file.txt").read_text(encoding="utf-8") == "keep"
    assert (nested_mount / "mounted-file.txt").read_text(encoding="utf-8") == "keep"
    assert not (content_root / "temporary" / "stale.txt").exists()


def test_sanitize_warm_workdir_uses_validated_encoded_reset_script(tmp_path: Path):
    class _ResetAdapter:
        argv = None

        def exec_argv(self, _session_name, argv, **_kwargs):
            self.argv = list(argv)
            return True, "reset"

    adapter = _ResetAdapter()
    orchestrator = CloudOrchestrator(
        store=CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False),
        adapter=adapter,
        allowed_roots=[tmp_path],
    )

    assert orchestrator._sanitize_warm_workdir("reset-session") is True
    assert adapter.argv is not None
    assert colab_bridge._validate_argv(adapter.argv) == adapter.argv

    command = adapter.argv[2]
    prefix = "import base64;exec(base64.b64decode("
    suffix = ").decode())"
    assert command.startswith(prefix)
    assert command.endswith(suffix)
    payload = ast.literal_eval(command[len(prefix) : -len(suffix)])
    assert base64.b64decode(payload).decode("utf-8") == cloud_orchestrator._WARM_RESET_SCRIPT


def _write_cache_archive(
    cache_root: Path,
    cache_key: str,
    anchor_paths: dict[str, Path],
    members: list[tuple[str, bytes]],
) -> Path:
    key_dir = cache_root / cache_key
    key_dir.mkdir(parents=True, exist_ok=True)
    archive_path = key_dir / "runtime.tar"
    index = {
        "schema_version": 1,
        "cache_key": cache_key,
        "anchors": {name: str(path.absolute()) for name, path in anchor_paths.items()},
    }
    index_bytes = json.dumps(index, sort_keys=True).encode("utf-8")
    with tarfile.open(archive_path, mode="w:") as archive:
        index_info = tarfile.TarInfo(setup_env._CACHE_INDEX_MEMBER)
        index_info.size = len(index_bytes)
        archive.addfile(index_info, io.BytesIO(index_bytes))
        for name, content in members:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    (key_dir / "runtime.tar.sha256").write_text(
        setup_env._sha256_file(archive_path) + "\n",
        encoding="ascii",
    )
    return archive_path


def _patch_successful_setup(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(setup_env, "_detect_gpu", lambda: {"device": "test"})
    monkeypatch.setattr(setup_env, "_gpu_matches_expected", lambda *_args, **_kwargs: (True, "ok"))

    def verify_packages(*_args):
        calls.append("packages")
        return True, 0, "", "ok", {}

    def verify_models(*_args):
        calls.append("models")
        return True, "models ok"

    monkeypatch.setattr(setup_env, "_verify_packages_and_binaries", verify_packages)
    monkeypatch.setattr(setup_env, "_verify_models_readiness", verify_models)
    monkeypatch.setattr(setup_env, "_stage_bundled_subtitle_fonts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(setup_env, "_ensure_linux_subtitle_fonts", lambda **_kwargs: (True, "ok"))
    for name in (
        "AUTODUB_SETUP_PHASES",
        setup_env._CACHE_RESTORE_DONE_ENV,
        setup_env._CACHE_STATUS_ENV,
        setup_env._CACHE_KEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _run_setup_with_cache(
    workdir: Path,
    manifest_path: Path,
    cache_root: Path,
    *,
    no_cache_save: bool,
    expected_sha256: str | None = None,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict]:
    result_path = workdir / "env_result.json"
    state_path = workdir / "env_state.json"
    args = [
        "--workdir",
        str(workdir),
        "--manifest",
        str(manifest_path),
        "--manifest-out",
        str(result_path),
        "--state-file",
        str(state_path),
        "--expected-gpu",
        "CPU",
        "--allow-local-test",
        "--cache-dir",
        str(cache_root),
    ]
    if expected_sha256 is not None:
        args.extend(["--cache-expected-sha256", expected_sha256])
    if no_cache_save:
        args.append("--no-cache-save")
    rc = setup_env.main(args)
    output = capsys.readouterr().out
    result_line = next(
        line for line in output.splitlines() if line.startswith(setup_env.RESULT_PREFIX)
    )
    return rc, json.loads(result_line[len(setup_env.RESULT_PREFIX) :])


def _run_cache_save_only(
    workdir: Path,
    manifest_path: Path,
    cache_root: Path,
    *,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict]:
    result_path = workdir / "cache_save_result.json"
    rc = setup_env.main(
        [
            "--workdir",
            str(workdir),
            "--manifest",
            str(manifest_path),
            "--manifest-out",
            str(result_path),
            "--allow-local-test",
            "--cache-dir",
            str(cache_root),
            "--cache-save-only",
        ]
    )
    output = capsys.readouterr().out
    result_line = next(
        line for line in output.splitlines() if line.startswith(setup_env.RESULT_PREFIX)
    )
    return rc, json.loads(result_line[len(setup_env.RESULT_PREFIX) :])


def test_setup_cache_restores_matching_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    roformer = workdir / "model_cache"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python").write_text("runtime", encoding="utf-8")
    roformer.mkdir()
    (roformer / "roformer.ckpt").write_bytes(b"model")
    cache_root = tmp_path / "drive"
    cache_key = _cache_key(workdir, manifest_path)

    assert setup_env._save_cloud_cache(cache_root, cache_key, workdir) == "saved"
    archive_path = cache_root / cache_key / "runtime.tar"
    assert setup_env._sha256_file(archive_path) == (
        cache_root / cache_key / "runtime.tar.sha256"
    ).read_text(encoding="ascii").strip()
    trusted_sha256 = setup_env._sha256_file(archive_path)
    (cache_root / cache_key / "runtime.tar.sha256").write_text("0" * 64, encoding="ascii")
    shutil.rmtree(runtime)
    shutil.rmtree(roformer)

    assert setup_env._restore_cloud_cache(
        cache_root,
        cache_key,
        workdir,
        expected_sha256=trusted_sha256,
    ) == "restored"
    assert (runtime / "bin" / "python").read_text(encoding="utf-8") == "runtime"
    assert (roformer / "roformer.ckpt").read_bytes() == b"model"


def test_cache_restore_keeps_real_symlink_venv_runnable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--symlinks", str(runtime)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    runtime_python = runtime / "bin" / "python"
    assert runtime_python.is_symlink()
    base_interpreter = runtime_python.resolve()

    python_install = workdir / ".autodub_python"
    python_install.mkdir()
    (python_install / "interpreter-marker").write_text("python", encoding="utf-8")
    cache_root = tmp_path / "drive"
    cache_key = "e" * 64

    assert setup_env._save_cloud_cache(cache_root, cache_key, workdir) == "saved"
    archive_path = cache_root / cache_key / "runtime.tar"
    shutil.rmtree(runtime)

    assert setup_env._restore_cloud_cache(
        cache_root,
        cache_key,
        workdir,
        expected_sha256=setup_env._sha256_file(archive_path),
    ) == "restored"
    assert runtime_python.is_symlink()
    assert runtime_python.resolve() == base_interpreter
    assert (python_install / "interpreter-marker").read_text(encoding="utf-8") == "python"
    result = subprocess.run(
        [str(runtime_python), "-c", "import sys; print(sys.prefix)"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == str(runtime)


def test_cache_sha_mismatch_is_invalid_and_setup_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    runtime.mkdir()
    (runtime / "existing.txt").write_text("local", encoding="utf-8")
    cache_root = tmp_path / "drive"
    cache_key = _cache_key(workdir, manifest_path)
    assert setup_env._save_cloud_cache(cache_root, cache_key, workdir) == "saved"
    archive_path = cache_root / cache_key / "runtime.tar"
    shutil.rmtree(runtime)
    checksum_path = cache_root / cache_key / "runtime.tar.sha256"
    checksum_path.write_text("0" * 64, encoding="ascii")

    calls: list[str] = []
    _patch_successful_setup(monkeypatch, calls)
    rc, result = _run_setup_with_cache(
        workdir,
        manifest_path,
        cache_root,
        no_cache_save=True,
        expected_sha256="0" * 64,
        capsys=capsys,
    )

    assert rc == setup_env.EXIT_OK
    assert calls == ["packages", "models"]
    assert result["cache_status"] == "invalid"
    assert result["cache_key"] == cache_key
    assert (cache_root / cache_key / "runtime.tar").is_file()
    assert checksum_path.read_text(encoding="ascii") == "0" * 64
    assert not runtime.exists()
    assert archive_path.is_file()


def test_cache_restore_without_local_hash_skips_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    runtime.mkdir()
    (runtime / "local.txt").write_text("local", encoding="utf-8")
    cache_root = tmp_path / "drive"
    cache_key = _cache_key(workdir, manifest_path)
    assert setup_env._save_cloud_cache(cache_root, cache_key, workdir) == "saved"
    shutil.rmtree(runtime)

    calls: list[str] = []
    _patch_successful_setup(monkeypatch, calls)
    rc, result = _run_setup_with_cache(
        workdir,
        manifest_path,
        cache_root,
        no_cache_save=True,
        capsys=capsys,
    )

    assert rc == setup_env.EXIT_OK
    assert calls == ["packages", "models"]
    assert result["cache_status"] == "miss"
    assert not runtime.exists()


def test_cache_restore_rejects_traversal_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)
    anchors = {"runtime": (workdir / ".autodub_venv").absolute()}
    cache_key = "c" * 64
    cache_root = tmp_path / "drive"
    traversal = anchors["runtime"].as_posix().lstrip("/") + "/../escape.txt"
    _write_cache_archive(cache_root, cache_key, anchors, [(traversal, b"unsafe")])
    archive_path = cache_root / cache_key / "runtime.tar"

    assert setup_env._restore_cloud_cache(
        cache_root,
        cache_key,
        workdir,
        expected_sha256=setup_env._sha256_file(archive_path),
    ) == "invalid"
    assert archive_path.is_file()
    assert not (workdir / "escape.txt").exists()


def test_cache_save_is_atomic_and_prunes_old_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    runtime.mkdir()
    (runtime / "runtime.txt").write_text("cached", encoding="utf-8")
    cache_root = tmp_path / "drive"
    old_key = "a" * 64
    old_dir = cache_root / old_key
    old_dir.mkdir(parents=True)
    (old_dir / "runtime.tar").write_bytes(b"old archive")
    unrelated_dir = cache_root / ("c" * 64)
    unrelated_dir.mkdir()
    unrelated_file = unrelated_dir / "unrelated.txt"
    unrelated_file.write_text("preserve", encoding="utf-8")
    non_cache_dir = cache_root / "keep-me"
    non_cache_dir.mkdir()
    (non_cache_dir / "keep.txt").write_text("keep", encoding="utf-8")
    current_key = "b" * 64

    assert setup_env._save_cloud_cache(cache_root, current_key, workdir) == "saved"
    key_dir = cache_root / current_key
    archive_path = key_dir / "runtime.tar"
    checksum_path = key_dir / "runtime.tar.sha256"
    assert archive_path.is_file() and checksum_path.is_file()
    assert setup_env._sha256_file(archive_path) == checksum_path.read_text(encoding="ascii").strip()
    assert not old_dir.exists()
    assert unrelated_file.read_text(encoding="utf-8") == "preserve"
    assert (non_cache_dir / "keep.txt").is_file()

    failing_root = tmp_path / "failed-drive"

    class FailingTar:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self):
            self.path.write_bytes(b"partial archive")
            return self

        def __exit__(self, *_args):
            return False

        def addfile(self, *_args):
            return None

        def add(self, *_args, **_kwargs):
            raise OSError("simulated archive write failure")

    monkeypatch.setattr(
        setup_env.tarfile,
        "open",
        lambda path, *_args, **_kwargs: FailingTar(Path(path)),
    )
    failed_key = "d" * 64
    assert setup_env._save_cloud_cache(failing_root, failed_key, workdir) == "save_failed"
    failed_dir = failing_root / failed_key
    assert not (failed_dir / "runtime.tar").exists()
    assert not (failed_dir / "runtime.tar.sha256").exists()
    assert not (failed_dir / "runtime.tar.partial").exists()


def test_cache_prune_does_not_descend_into_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)
    (workdir / ".autodub_venv").mkdir()
    cache_root = tmp_path / "drive"
    old_dir = cache_root / ("a" * 64)
    mounted_dir = old_dir / "mounted"
    mounted_dir.mkdir(parents=True)
    mounted_file = mounted_dir / "user-file.txt"
    mounted_file.write_text("keep", encoding="utf-8")
    mount_path = mounted_dir.resolve()
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path,
        "ismount",
        lambda path: Path(path).resolve() == mount_path or real_ismount(path),
    )

    assert setup_env._save_cloud_cache(cache_root, "b" * 64, workdir) == "saved"
    assert mounted_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "cache_root",
    ["/content/drive", "/content/drive/MyDrive", "/tmp/outside-cloud-cache"],
)
def test_cache_save_refuses_unsafe_roots(tmp_path: Path, cache_root: str):
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)

    assert setup_env._validated_cloud_cache_root(cache_root) is None
    assert setup_env._save_cloud_cache(cache_root, "f" * 64, workdir) == "save_failed"


def test_setup_cache_miss_does_not_save_during_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, manifest_path = _prepare_setup_workdir(tmp_path)
    cache_root = tmp_path / "drive"
    calls: list[str] = []
    _patch_successful_setup(monkeypatch, calls)

    rc, result = _run_setup_with_cache(
        workdir,
        manifest_path,
        cache_root,
        no_cache_save=False,
        capsys=capsys,
    )

    assert rc == setup_env.EXIT_OK
    assert result["cache_status"] == "miss"
    assert "cache_restore" in result["phase_seconds"]
    assert "cache_save" in result["phase_seconds"]
    assert not cache_root.exists() or not list(cache_root.iterdir())


def test_setup_cache_save_only_writes_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _use_temp_model_homes(monkeypatch, tmp_path)
    workdir, manifest_path = _prepare_setup_workdir(tmp_path)
    runtime = workdir / ".autodub_venv"
    runtime.mkdir()
    (runtime / "runtime.txt").write_text("runtime", encoding="utf-8")
    model_cache = workdir / "model_cache"
    model_cache.mkdir()
    (model_cache / "weights.bin").write_bytes(b"weights")
    cache_root = tmp_path / "drive"
    calls: list[str] = []
    _patch_successful_setup(monkeypatch, calls)

    rc, result = _run_setup_with_cache(
        workdir,
        manifest_path,
        cache_root,
        no_cache_save=False,
        capsys=capsys,
    )

    assert rc == setup_env.EXIT_OK
    assert calls == ["packages", "models"]
    assert result["cache_status"] == "miss"
    assert not cache_root.exists()

    save_rc, save_result = _run_cache_save_only(
        workdir,
        manifest_path,
        cache_root,
        capsys=capsys,
    )

    key_dir = cache_root / result["cache_key"]
    assert save_rc == setup_env.EXIT_OK
    assert save_result["cache_status"] == "saved"
    assert save_result["cache_key"] == result["cache_key"]
    assert save_result["cache_sha256"] == setup_env._sha256_file(key_dir / "runtime.tar")
    assert "cache_save" in save_result["phase_seconds"]
    assert calls == ["packages", "models"]
    assert (key_dir / "runtime.tar").is_file()
    assert (key_dir / "runtime.tar.sha256").is_file()


class _CacheJobAdapter:
    def __init__(
        self,
        mount_result: tuple[bool, str],
        warm_session: str | None = None,
        *,
        setup_cache_status: str = "miss",
        pipeline_result: tuple[bool, str] = (True, "PIPELINE_SUCCESS"),
        cache_save_result: tuple[bool, str] = (True, ""),
        unmount_result: tuple[bool, str] = (True, "unmounted"),
    ):
        self.mount_result = mount_result
        self.mount_calls = []
        self.setup_argv = None
        self.save_only_argv = None
        self.warm_reset_argv = None
        self.command_order = []
        self.setup_cache_status = setup_cache_status
        self.pipeline_result = pipeline_result
        self.cache_save_result = cache_save_result
        self.cache_key = "f" * 64
        self.cache_sha256 = "a" * 64
        self.unmount_result = unmount_result
        self.unmount_calls = []
        self.active_sessions = {warm_session} if warm_session else set()
        self.video_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42rendered"
        self.metadata = {
            "status": "completed",
            "segments_count": 1,
            "source_caption_evidence": {"version": 1, "passed": True, "cue_count": 1},
            "subtitle_timing": {"passed": True, "cue_count": 1},
            "translated_count": 1,
            "tts_verified_count": 1,
            "output_video_probe": {
                "video_codec": "h264",
                "audio_codec": "aac",
                "size_bytes": len(self.video_bytes),
            },
            "artifacts": {
                "output_video.mp4": {
                    "size_bytes": len(self.video_bytes),
                    "sha256": hashlib.sha256(self.video_bytes).hexdigest(),
                }
            },
        }

    def get_usage(self):
        return {"ok": True, "balance": 100.0, "usage_rate": 0.0, "active_assignments": 0}

    def get_sessions_status(self):
        return {"ok": True, "sessions": [{"name": name} for name in self.active_sessions]}

    def create_session(self, session_name, gpu="T4", cancel_check=None):
        self.active_sessions.add(session_name)
        return True, "created"

    def stop_session(self, session_name):
        self.active_sessions.discard(session_name)
        return True, "stopped"

    def verify_session_stopped(self, session_name):
        self.active_sessions.discard(session_name)
        return {
            "verified_stopped": True,
            "cleanup_status": "verified",
            "attempts": 1,
            "error": None,
        }

    def mount_drive(self, session_name, mount_path="/content/drive", **kwargs):
        self.mount_calls.append((session_name, mount_path, kwargs))
        return self.mount_result

    def upload_file(self, session_name, local_path, remote_filename=None, cancel_check=None):
        return True, "uploaded"

    def exec_argv(
        self,
        session_name,
        argv,
        *,
        cwd="/content",
        env_vars=None,
        timeout=None,
        cancel_check=None,
    ):
        if len(argv) >= 3 and "flush_and_unmount" in argv[-1]:
            self.command_order.append("drive_unmount")
            self.unmount_calls.append((session_name, list(argv), timeout))
            return self.unmount_result
        if len(argv) >= 3 and "base64.b64decode(" in argv[-1]:
            self.command_order.append("warm_reset")
            self.warm_reset_argv = list(argv)
            return True, "reset"
        if "--cache-save-only" in argv:
            self.command_order.append("cache_save")
            self.save_only_argv = list(argv)
            if self.cache_save_result[1]:
                return self.cache_save_result
            result = {
                "ok": True,
                "cache_status": "saved",
                "cache_key": self.cache_key,
                "cache_sha256": self.cache_sha256,
                "phase_seconds": {"cache_save": 0.2},
            }
            return True, f"{setup_env.RESULT_PREFIX}{json.dumps(result)}"
        if "setup_env.py" in argv:
            self.command_order.append("setup")
            self.setup_argv = list(argv)
            cache_status = self.setup_cache_status if "--cache-dir" in argv else "disabled"
            result = {
                "ok": True,
                "cache_status": cache_status,
                "cache_key": self.cache_key,
                "phase_seconds": {"cache_restore": 0.1, "cache_save": 0.0},
            }
            return True, f"{setup_env.RESULT_PREFIX}{json.dumps(result)}"
        if "full_pipeline.py" in argv:
            self.command_order.append("pipeline")
            return self.pipeline_result
        return True, "ok"

    def download_file(self, session_name, remote_filename, local_dir, cancel_check=None):
        destination = Path(local_dir) / Path(remote_filename).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if remote_filename == "output_video.mp4":
            destination.write_bytes(self.video_bytes)
        elif remote_filename == "result_metadata.json":
            destination.write_text(json.dumps(self.metadata), encoding="utf-8")
        else:
            destination.write_text("artifact", encoding="utf-8")
        return True, str(destination)


def _run_cache_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mount_result: tuple[bool, str],
    *,
    warm_session: str | None = None,
    setup_cache_status: str = "miss",
    cache_save_enabled: bool = True,
    pipeline_result: tuple[bool, str] = (True, "PIPELINE_SUCCESS"),
    cache_save_result: tuple[bool, str] = (True, ""),
    cache_backend: str = "gdrive",
    warm_reuse_enabled: bool = False,
    warm_keepalive_seconds: int = 0,
    unmount_result: tuple[bool, str] = (True, "unmounted"),
    expected_job_success: bool = True,
    trusted_cache_sha256: str | None = None,
):
    output_dir = tmp_path / "deliverables"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("AUTODUB_CLOUD_OUTPUT_DIR", "")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_cache_backend", cache_backend)
    monkeypatch.setitem(
        DEFAULT_CONFIG,
        "cloud_cache_drive_root",
        "/content/drive/MyDrive/AutoDubCloudCache",
    )
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_cache_save_enabled", cache_save_enabled)
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_drive_mount_timeout_seconds", 10)
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_keepalive_seconds", warm_keepalive_seconds)
    monkeypatch.setattr(cloud_orchestrator, "_resolve_gemini_key_and_secrets", lambda: ("", ()))
    monkeypatch.setattr(cloud_orchestrator, "_resolve_local_font_files_for_bundle", lambda _font: ())

    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42source")
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    if trusted_cache_sha256 is not None:
        store.set_setting(f"drive_cache_sha256:{'f' * 64}", trusted_cache_sha256)
        store.set_setting("drive_cache_latest_key", "f" * 64)
    adapter = _CacheJobAdapter(
        mount_result,
        warm_session=warm_session,
        setup_cache_status=setup_cache_status,
        pipeline_result=pipeline_result,
        cache_save_result=cache_save_result,
        unmount_result=unmount_result,
    )
    adapter.store = store
    if warm_session:
        store.park_warm_session(
            warm_session,
            "T4",
            ttl_seconds=3600,
            source_job_id="prior-job",
            now=time.time() - 30,
        )
    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.4)
    claimed = store.claim_job(job.job_id, owner_id="cache-test")
    assert claimed is not None
    orchestrator = CloudOrchestrator(
        store=store,
        adapter=adapter,
        allowed_roots=[tmp_path],
        enable_warm_session_reuse=bool(warm_session) or warm_reuse_enabled,
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_downloaded_output",
        lambda *_args, **_kwargs: {
            "sha256": hashlib.sha256(adapter.video_bytes).hexdigest(),
            "size_bytes": len(adapter.video_bytes),
            "remote_metadata": adapter.metadata,
            "local_probe": {
                "duration_seconds": 1.0,
                "video_codec": "h264",
                "audio_codec": "aac",
            },
            "qc_report": {"passed": True},
        },
    )
    assert orchestrator.process_job(claimed, owner_id="cache-test") is expected_job_success
    for active_session in list(adapter.active_sessions):
        orchestrator._cancel_warm_timer(active_session)
    adapter.parked_warm_sessions = store.list_warm_sessions()
    return adapter, store.get_job(job.job_id)


@pytest.mark.parametrize("setup_cache_status", ["miss", "invalid"])
def test_orchestrator_mounts_drive_and_passes_cache_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup_cache_status: str,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        setup_cache_status=setup_cache_status,
    )

    assert len(adapter.mount_calls) == 1
    assert adapter.setup_argv is not None
    assert "--cache-dir" in adapter.setup_argv
    assert "/content/drive/MyDrive/AutoDubCloudCache" in adapter.setup_argv
    assert "--no-cache-save" not in adapter.setup_argv
    assert adapter.save_only_argv is not None
    assert colab_bridge._validate_argv(adapter.setup_argv) == adapter.setup_argv
    assert colab_bridge._validate_argv(adapter.save_only_argv) == adapter.save_only_argv
    assert adapter.command_order == ["setup", "pipeline", "cache_save"]
    assert job.output_manifest["cache_backend"] == "gdrive"
    assert job.output_manifest["cache_mounted"] is True
    assert job.output_manifest["cache_status"] == "saved"
    assert adapter.store.get_setting("drive_cache_latest_key") == adapter.cache_key
    assert adapter.store.get_setting(
        f"drive_cache_sha256:{adapter.cache_key}"
    ) == adapter.cache_sha256
    assert job.output_manifest["setup_phase_seconds"]["cache_restore"] == 0.1
    assert job.output_manifest["setup_phase_seconds"]["cache_save"] == 0.2
    assert "cache_save" in job.output_manifest["stage_timings_seconds"]


def test_orchestrator_passes_locally_trusted_cache_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    trusted_sha256 = "b" * 64
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        setup_cache_status="restored",
        trusted_cache_sha256=trusted_sha256,
    )

    assert adapter.setup_argv is not None
    expected_index = adapter.setup_argv.index("--cache-expected-sha256") + 1
    assert adapter.setup_argv[expected_index] == trusted_sha256
    assert job.state == "completed"


@pytest.mark.parametrize(
    "mount_result",
    [
        (False, "mount failed"),
        (False, "Google Drive cần được ủy quyền một lần."),
    ],
)
def test_orchestrator_mount_failure_continues_without_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mount_result: tuple[bool, str],
):
    adapter, job = _run_cache_job(tmp_path, monkeypatch, mount_result)

    assert len(adapter.mount_calls) == 1
    assert adapter.setup_argv is not None
    assert "--cache-dir" not in adapter.setup_argv
    assert job.state == "completed"
    assert job.output_manifest["cache_mounted"] is False
    assert job.output_manifest["cache_status"] == "disabled"
    assert adapter.save_only_argv is None


def test_orchestrator_does_not_mount_for_warm_reused_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        warm_session="cjobcachewarm",
    )

    assert adapter.mount_calls == []
    assert adapter.setup_argv is not None
    assert "--cache-dir" not in adapter.setup_argv
    assert job.output_manifest["cache_mounted"] is False
    assert job.output_manifest["cache_status"] == "disabled"
    assert adapter.save_only_argv is None


def test_orchestrator_unmounts_drive_before_parking_warm_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        warm_reuse_enabled=True,
        warm_keepalive_seconds=300,
    )

    assert job.state == "completed"
    assert len(adapter.unmount_calls) == 1
    assert adapter.unmount_calls[0][2] == 30
    assert colab_bridge._validate_argv(adapter.unmount_calls[0][1]) == adapter.unmount_calls[0][1]
    assert adapter.command_order.index("drive_unmount") < adapter.command_order.index("warm_reset")
    assert adapter.active_sessions


def test_warm_session_parks_when_drive_cache_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        cache_backend="none",
        warm_reuse_enabled=True,
        warm_keepalive_seconds=300,
    )

    assert job.state == "completed"
    assert adapter.mount_calls == []
    assert adapter.unmount_calls == []
    assert adapter.command_order == ["setup", "pipeline", "warm_reset"]
    assert adapter.warm_reset_argv is not None
    assert len(adapter.parked_warm_sessions) == 1
    assert adapter.parked_warm_sessions[0]["session_name"] in adapter.active_sessions


def test_orchestrator_does_not_unmount_drive_that_was_not_mounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (False, "mount failed"),
        warm_reuse_enabled=True,
        warm_keepalive_seconds=300,
    )

    assert job.state == "completed"
    assert adapter.unmount_calls == []
    assert "warm_reset" in adapter.command_order


def test_drive_unmount_failure_stops_session_instead_of_parking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    adapter, job = _run_cache_job(
        tmp_path,
        monkeypatch,
        (True, "mounted"),
        warm_reuse_enabled=True,
        warm_keepalive_seconds=300,
        unmount_result=(False, "unmount failed"),
    )

    assert job.state == "completed"
    assert len(adapter.unmount_calls) == 1
    assert "warm_reset" not in adapter.command_order
    assert adapter.active_sessions == set()


def test_orchestrator_does_not_save_on_cache_hit_or_when_saving_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hit_adapter, hit_job = _run_cache_job(
        tmp_path / "hit",
        monkeypatch,
        (True, "mounted"),
        setup_cache_status="restored",
    )
    assert hit_adapter.save_only_argv is None
    assert hit_job.output_manifest["cache_status"] == "restored"

    disabled_adapter, disabled_job = _run_cache_job(
        tmp_path / "disabled",
        monkeypatch,
        (True, "mounted"),
        cache_save_enabled=False,
    )
    assert disabled_adapter.save_only_argv is None
    assert disabled_job.output_manifest["cache_status"] == "miss"


def test_orchestrator_saves_only_after_pipeline_success_and_continues_on_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    failed_pipeline_adapter, _ = _run_cache_job(
        tmp_path / "pipeline-failed",
        monkeypatch,
        (True, "mounted"),
        pipeline_result=(False, "pipeline failed"),
        expected_job_success=False,
    )
    assert failed_pipeline_adapter.save_only_argv is None

    save_failure = {
        "ok": False,
        "cache_status": "save_failed",
        "phase_seconds": {"cache_save": 0.2},
    }
    save_failure_adapter, save_failure_job = _run_cache_job(
        tmp_path / "save-failed",
        monkeypatch,
        (True, "mounted"),
        cache_save_result=(False, f"{setup_env.RESULT_PREFIX}{json.dumps(save_failure)}"),
    )
    assert save_failure_adapter.save_only_argv is not None
    assert save_failure_job.output_manifest["cache_status"] == "save_failed"
    assert save_failure_job.state == "completed"


def test_mount_drive_hides_authorization_url_and_closes_stdin(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return (
            1,
            "[colab] REQUIRED: Google Drive Authorization needed. https://accounts.google.com/o/oauth2/auth?token=secret",
            "",
        )

    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", fake_run)
    mounted, message = colab_bridge.mount_drive("drivetest", timeout=17)

    assert mounted is False
    assert "colab drivemount" in message
    assert "https://" not in message and "token=secret" not in message
    assert calls[0][0] == ["drivemount", "-s", "drivetest", "/content/drive"]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["timeout"] == 17
    assert calls[0][1]["log_failure_output"] is False


def test_mount_drive_rejects_paths_outside_content(monkeypatch: pytest.MonkeyPatch):
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", fake_run)

    assert colab_bridge.mount_drive("drivetest", "/content/../etc")[0] is False
    assert colab_bridge.mount_drive("drivetest", "/tmp/drive")[0] is False
    assert colab_bridge.mount_drive("drivetest", "/content/drive\n")[0] is False
    assert called is False


def test_cache_anchors_exclude_pip_and_hf_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _use_temp_model_homes(monkeypatch, tmp_path)
    monkeypatch.setenv("HF_HOME", str(home / ".cache" / "huggingface"))
    workdir, _manifest_path = _prepare_setup_workdir(tmp_path)
    anchors = setup_env._cloud_cache_anchor_paths(workdir)

    assert anchors["runtime"] == (workdir / ".autodub_venv").absolute()
    assert anchors["python_install"] == (workdir / ".autodub_python").absolute()
    assert anchors["roformer"] == (workdir / "model_cache").absolute()
    assert anchors["whisper"] == (home / ".cache" / "whisper").absolute()
    assert anchors["demucs"] == (home / ".cache" / "torch" / "hub" / "checkpoints").absolute()
    assert anchors["easyocr"] == (home / ".EasyOCR" / "model").absolute()
    assert anchors["huggingface"] == (home / ".cache" / "huggingface" / "hub").absolute()
    assert all(".autodub_uv_cache" not in str(path) for path in anchors.values())
    assert all(path.name != "pip" for path in anchors.values())
