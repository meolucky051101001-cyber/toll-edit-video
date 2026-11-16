"""Phase 4 offline tests: Pinned environment manifest, idempotent setup_env, local preflight, and bundle verification."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_contract import (
    CloudErrorCode,
    build_input_fingerprint,
    probe_video_metadata,
)
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.gpu_scripts import setup_env


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path_obj: Path) -> str:
    return _sha256_bytes(path_obj.read_bytes())


def _make_sample_video(directory: Path, name: str = "sample.mp4", payload: bytes = b"") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    body = payload or (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"test-video-data" * 4)
    p.write_bytes(body)
    return p


def _prepare_slim_workdir(tmp_path: Path, *, manifest_overrides: dict | None = None) -> Path:
    workdir = tmp_path / "remote_content"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "requirements_cloud.txt").write_text("requests>=2.0\n", encoding="utf-8")

    # Use a real binary present on PATH (python executable directory) and lightweight stdlib/installed packages
    manifest = {
        "schema_version": 1,
        "python_min_version": "3.10",
        "requirements_file": "requirements_cloud.txt",
        "min_free_disk_mb": 1,
        "pinned_packages": [
            {"package": "pytest", "version": "", "import_name": "pytest"},
        ],
        "wheels": [],
        "required_binaries": [],
        "required_v1_modules": [],
        "models": {},
        "gpu_family_patterns": {
            "T4": ["t4"],
            "L4": ["l4"],
            "A100": ["a100"],
        },
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (workdir / "cloud_env_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return workdir


def test_env_manifest_pinned_requirements_wheel_sha256_and_config_defaults():
    scripts_dir = BACKEND_ROOT / "cloud" / "gpu_scripts"
    manifest_path = scripts_dir / "cloud_env_manifest.json"
    req_path = scripts_dir / "requirements_cloud.txt"

    assert manifest_path.is_file()
    assert req_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    req_lines = [
        line.strip()
        for line in req_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(req_lines) >= 10
    assert all("==" in line for line in req_lines), "Every package in requirements_cloud.txt must be strictly pinned."

    for wheel_spec in manifest["wheels"]:
        wheel_file = scripts_dir / wheel_spec["filename"]
        assert wheel_file.is_file()
        assert _sha256_file(wheel_file) == wheel_spec["sha256"]

    for mod_name in manifest["required_v1_modules"]:
        in_scripts = scripts_dir / mod_name
        in_backend = BACKEND_ROOT / mod_name
        assert (in_scripts.is_file() and in_scripts.stat().st_size > 0) or (
            in_backend.is_file() and in_backend.stat().st_size > 0
        ), f"Missing required module {mod_name}"

    assert int(DEFAULT_CONFIG["setup_timeout_seconds"]) >= 300
    assert int(DEFAULT_CONFIG["min_remote_disk_mb"]) >= 1024
    assert int(DEFAULT_CONFIG["max_upload_size_bytes"]) > 0


def test_probe_video_metadata_and_input_fingerprint(tmp_path: Path, monkeypatch):
    video = _make_sample_video(tmp_path, "clip_1.mp4")

    # 1. Mock ffprobe unavailable/error -> safe header fallback for tiny fixtures
    monkeypatch.setattr(
        "backend.cloud.cloud_contract.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="invalid data"),
    )
    meta_fallback = probe_video_metadata(video)
    assert meta_fallback["exists"] is True
    assert meta_fallback["size_bytes"] == video.stat().st_size
    assert meta_fallback["header_valid"] is True
    assert meta_fallback["extension"] == ".mp4"
    assert meta_fallback["probe_method"] == "header_fallback"

    fp_fallback = build_input_fingerprint(str(video))
    assert fp_fallback["kind"] == "local_file"
    assert fp_fallback["extension"] == ".mp4"
    assert fp_fallback["size_bytes"] == video.stat().st_size
    assert fp_fallback["sha256"] == _sha256_file(video)

    # 2. Mock ffprobe returning complete media metadata
    ffprobe_json = json.dumps(
        {
            "format": {"duration": "42.5"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )
    monkeypatch.setattr(
        "backend.cloud.cloud_contract.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=ffprobe_json, stderr=""),
    )
    fp_probed = build_input_fingerprint(str(video))
    assert fp_probed["duration_seconds"] == pytest.approx(42.5)
    assert fp_probed["video_codec"] == "h264"
    assert fp_probed["audio_codec"] == "aac"
    assert fp_probed["width"] == 1920
    assert fp_probed["height"] == 1080

    # 3. URL fingerprint remains deterministic
    fp_url = build_input_fingerprint("https://example.com/v1.mp4", duration_seconds=12.0)
    assert fp_url["kind"] == "url"
    assert fp_url["duration_seconds"] == pytest.approx(12.0)


def test_probe_unicode_metadata_with_non_utf8_windows_locale(tmp_path: Path, monkeypatch):
    """ffprobe emits UTF-8 even when Windows defaults to a legacy code page."""
    video = _make_sample_video(tmp_path, "clip_中文.mp4")
    real_run = subprocess.run
    payload = json.dumps({"format": {"duration": "27.2", "tags": {"title": "中文"}},
                          "streams": [{"codec_type": "video", "codec_name": "h264"}]},
                         ensure_ascii=False).encode("utf-8")

    def emit_ffprobe_json(_argv, **kwargs):
        kwargs.setdefault("encoding", "cp1252")
        return real_run([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"], **kwargs)

    monkeypatch.setattr("backend.cloud.cloud_contract.subprocess.run", emit_ffprobe_json)
    metadata = probe_video_metadata(video)
    assert metadata["duration_seconds"] == pytest.approx(27.2)
    assert metadata["video_codec"] == "h264"


def test_cloud_runtime_bootstrap_preserves_notebook_python_and_reuses_verified_env(tmp_path, monkeypatch):
    from backend.cloud.gpu_scripts import bootstrap_runtime
    spec = {"python_version": "3.12.12", "uv_version": "0.9.10",
            "torch_index_url": "https://download.pytorch.org/whl/cu121",
            "torch_packages": {"torch": "2.5.1"}}
    calls = []
    runtime = tmp_path / ".autodub_venv"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "venv" in argv:
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python").touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap_runtime, "os", SimpleNamespace(name="posix", environ={}))
    monkeypatch.setattr(bootstrap_runtime.subprocess, "run", fake_run)
    python = bootstrap_runtime.ensure_runtime(tmp_path, spec)
    assert python == runtime / "bin" / "python"
    assert "--target" in calls[0]  # Bootstrap tooling stays outside notebook site-packages.
    assert all(cmd[0] == str(python) for cmd in calls[2:])
    before = len(calls)
    assert bootstrap_runtime.ensure_runtime(tmp_path, spec) == python
    assert len(calls) == before


def test_setup_env_refuses_local_production_env_without_flag(tmp_path: Path, monkeypatch):
    workdir = _prepare_slim_workdir(tmp_path)
    monkeypatch.setattr(setup_env, "_is_production_local_env", lambda: (True, "blocked local venv"))
    rc = setup_env.main(["--workdir", str(workdir)])
    assert rc == setup_env.EXIT_ENV_SETUP_FAILED
    res = json.loads((workdir / "env_manifest_result.json").read_text(encoding="utf-8"))
    assert res["ok"] is False
    assert res["error_code"] == CloudErrorCode.ENV_SETUP_FAILED.value


def test_post_install_verification_does_not_loop_install_on_broken_dependency(tmp_path, monkeypatch):
    workdir = _prepare_slim_workdir(tmp_path, manifest_overrides={
        "pinned_packages": [{"package": "pytest", "version": "0.0.0", "import_name": "pytest"}]
    })

    def unexpected_install(*args, **kwargs):
        pytest.fail("Post-install verification must report the broken dependency, not reinstall")

    monkeypatch.setattr(setup_env, "_install_dependencies", unexpected_install)
    rc = setup_env.main(["--workdir", str(workdir), "--allow-local-test", "--allow-any-gpu",
                         "--verify-installed", "--min-disk-mb", "1"])
    assert rc == setup_env.EXIT_PACKAGE_MISSING
    result = json.loads((workdir / "env_manifest_result.json").read_text(encoding="utf-8"))
    assert "version mismatch" in result["message"]


def test_setup_env_idempotent_state_cache_across_runs(tmp_path: Path, capsys):
    workdir = _prepare_slim_workdir(tmp_path)

    rc1 = setup_env.main(["--workdir", str(workdir), "--allow-local-test", "--allow-any-gpu", "--min-disk-mb", "1"])
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert setup_env.RESULT_PREFIX in out1
    res1 = json.loads((workdir / "env_manifest_result.json").read_text(encoding="utf-8"))
    assert res1["ok"] is True
    assert res1["pip_invoked"] is False
    assert (workdir / ".autodub_env_state.json").is_file()

    # Second run sees matching state fingerprint and skips installation idempotently
    rc2 = setup_env.main(["--workdir", str(workdir), "--allow-local-test", "--allow-any-gpu", "--min-disk-mb", "1"])
    _ = capsys.readouterr()
    assert rc2 == 0
    res2 = json.loads((workdir / "env_manifest_result.json").read_text(encoding="utf-8"))
    assert res2["ok"] is True
    assert res2["cache_hit"] is True
    assert res2["pip_invoked"] is False


def test_setup_env_distinct_failure_exit_codes(tmp_path: Path, monkeypatch):
    # 1. GPU mismatch (driver reports L4 vs expected A100) -> 10 / ENV_GPU_MISMATCH
    w_gpu = _prepare_slim_workdir(tmp_path / "gpu")
    monkeypatch.setattr(
        setup_env,
        "_detect_gpu",
        lambda: {
            "nvidia_smi_ok": True,
            "detected_names": ["NVIDIA L4"],
            "detected_name": "NVIDIA L4",
            "torch_installed": True,
            "cuda_available": True,
            "device_count": 1,
        },
    )
    rc_gpu = setup_env.main(["--workdir", str(w_gpu), "--allow-local-test", "--expected-gpu", "A100"])
    assert rc_gpu == setup_env.EXIT_GPU_MISMATCH
    assert json.loads((w_gpu / "env_manifest_result.json").read_text())["error_code"] == "ENV_GPU_MISMATCH"

    # 1b. Driver found T4, but torch.cuda.is_available() == False -> 10 / ENV_GPU_MISMATCH
    monkeypatch.setattr(
        setup_env,
        "_detect_gpu",
        lambda: {
            "nvidia_smi_ok": True,
            "detected_names": ["Tesla T4"],
            "detected_name": "Tesla T4",
            "torch_installed": True,
            "cuda_available": False,
            "device_count": 0,
        },
    )
    rc_cuda = setup_env.main(["--workdir", str(w_gpu), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "1"])
    assert rc_cuda == setup_env.EXIT_GPU_MISMATCH
    assert "torch.cuda" in json.loads((w_gpu / "env_manifest_result.json").read_text())["message"]

    # Reset healthy mock GPU for remaining checks
    monkeypatch.setattr(
        setup_env,
        "_detect_gpu",
        lambda: {
            "nvidia_smi_ok": True,
            "detected_names": ["Tesla T4"],
            "detected_name": "Tesla T4",
            "torch_installed": True,
            "cuda_available": True,
            "device_count": 1,
        },
    )

    # 2. Disk insufficient -> 11 / ENV_DISK_INSUFFICIENT
    w_disk = _prepare_slim_workdir(tmp_path / "disk")
    monkeypatch.setattr(setup_env.shutil, "disk_usage", lambda _p: SimpleNamespace(total=10**10, used=9**10, free=256 * 1024 * 1024))
    rc_disk = setup_env.main(["--workdir", str(w_disk), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "2048"])
    assert rc_disk == setup_env.EXIT_DISK_INSUFFICIENT
    assert json.loads((w_disk / "env_manifest_result.json").read_text())["error_code"] == "ENV_DISK_INSUFFICIENT"
    monkeypatch.setattr(setup_env.shutil, "disk_usage", lambda _p: SimpleNamespace(total=10**12, used=10**11, free=100 * 1024**3))

    # 3. Network failure in pip install -> 12 / ENV_NETWORK_ERROR
    w_net = _prepare_slim_workdir(
        tmp_path / "net",
        manifest_overrides={"pinned_packages": [{"package": "missing-pkg-xyz", "version": "1.0.0", "import_name": "missing_pkg_xyz"}]},
    )
    monkeypatch.setattr(
        setup_env.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="NewConnectionError: Failed to establish a new connection"),
    )
    rc_net = setup_env.main(["--workdir", str(w_net), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "1"])
    assert rc_net == setup_env.EXIT_NETWORK_ERROR
    assert json.loads((w_net / "env_manifest_result.json").read_text())["error_code"] == "ENV_NETWORK_ERROR"

    # 4. Corrupt wheel SHA256 -> 13 / ENV_PACKAGE_MISSING
    bad_whl = (tmp_path / "whl")
    w_pkg = _prepare_slim_workdir(
        bad_whl,
        manifest_overrides={
            "wheels": [
                {
                    "filename": "dummy-1.0.0-py3-none-any.whl",
                    "package": "dummy",
                    "version": "1.0.0",
                    "import_name": "dummy",
                    "sha256": "0" * 64,
                }
            ]
        },
    )
    (w_pkg / "dummy-1.0.0-py3-none-any.whl").write_bytes(b"corrupted wheel")
    rc_pkg = setup_env.main(["--workdir", str(w_pkg), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "1"])
    assert rc_pkg == setup_env.EXIT_PACKAGE_MISSING
    assert json.loads((w_pkg / "env_manifest_result.json").read_text())["error_code"] == "ENV_PACKAGE_MISSING"

    # 5. Model readiness failure -> 14 / ENV_MODEL_LOAD_FAILED
    w_mdl = _prepare_slim_workdir(
        tmp_path / "model",
        manifest_overrides={"models": {"required_model_files": [{"path": "models/weights.bin", "sha256": "a" * 64}]}},
    )
    rc_mdl = setup_env.main(["--workdir", str(w_mdl), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "1"])
    assert rc_mdl == setup_env.EXIT_MODEL_LOAD_FAILED
    assert json.loads((w_mdl / "env_manifest_result.json").read_text())["error_code"] == "ENV_MODEL_LOAD_FAILED"

    # 6. Upload / bundle verification failure -> 15 / UPLOAD_VERIFICATION_FAILED
    w_bnd = _prepare_slim_workdir(tmp_path / "bundle")
    uploaded_video = w_bnd / "input.mp4"
    uploaded_video.write_bytes(b"truncated")
    bundle_manifest = {
        "files": [],
        "expected_video": {
            "remote_filename": "input.mp4",
            "size_bytes": 999,
            "sha256": _sha256_bytes(b"original full bytes"),
        },
    }
    (w_bnd / "bundle_manifest.json").write_text(json.dumps(bundle_manifest), encoding="utf-8")
    rc_bnd = setup_env.main(
        ["--workdir", str(w_bnd), "--allow-local-test", "--expected-gpu", "T4", "--min-disk-mb", "1", "--verify-bundle", "bundle_manifest.json"]
    )
    assert rc_bnd == setup_env.EXIT_UPLOAD_VERIFICATION_FAILED
    assert json.loads((w_bnd / "env_manifest_result.json").read_text())["error_code"] == "UPLOAD_VERIFICATION_FAILED"


class _SpyCloudAdapter:
    def __init__(self, *, setup_exec_result=(True, "ok")):
        self.sessions_created = []
        self.sessions_stopped = []
        self.uploaded = {}
        self.exec_calls = []
        self.setup_exec_result = setup_exec_result

    def get_usage(self):
        return {"ok": True, "balance": 50.0, "stale": False}

    def create_session(self, session_name, gpu="T4", metadata=None):
        self.sessions_created.append((session_name, gpu, metadata))
        return True, f"created {session_name}"

    def stop_session(self, session_name):
        self.sessions_stopped.append(session_name)
        return True, "stopped"

    def get_sessions_status(self):
        active = [{"name": s} for s, _, _ in self.sessions_created if s not in self.sessions_stopped]
        return {"ok": True, "sessions": active}

    def upload_file(self, session_name, local_path, remote_filename):
        data = Path(local_path).read_bytes()
        self.uploaded[remote_filename] = data
        return True, "uploaded"

    def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
        self.exec_calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout, "env_vars": env_vars})
        if len(argv) >= 2 and argv[1] == "setup_env.py":
            return self.setup_exec_result
        return True, "PIPELINE_SUCCESS"

    def download_file(self, session_name, remote_filename, local_dir):
        dest = Path(local_dir) / Path(remote_filename).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(
            (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"dubbed-output-mp4")
            if remote_filename.endswith(".mp4")
            else b"{}"
        )
        return True, str(dest)


def _enqueue_and_claim(store: CloudJobStore, video_path: Path, *, owner_id: str = "w1"):
    job = store.enqueue(str(video_path), video_name=video_path.name, gpu_type="T4", cu_estimated=0.5)
    claimed = store.claim_job(job.job_id, owner_id=owner_id)
    assert claimed is not None
    return claimed


def test_orchestrator_local_preflight_blocks_empty_oversized_and_corrupt_bundle_with_0_cu(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "out"))

    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    adapter = _SpyCloudAdapter()
    orch = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])

    # 1. Empty 0-byte local video fails preflight cleanly before any GPU allocation
    empty_vid = tmp_path / "empty.mp4"
    empty_vid.write_bytes(b"")
    job1 = _enqueue_and_claim(store, empty_vid, owner_id="w1")
    assert orch.process_job(job1, owner_id="w1") is False
    saved1 = store.get_job(job1.job_id)
    assert saved1.state == "failed"
    assert saved1.error_code == CloudErrorCode.INVALID_INPUT.value
    assert saved1.cu_actual == 0.0
    assert adapter.sessions_created == []

    # 2. Oversized video rejected by preflight check (0 CU)
    big_vid = _make_sample_video(tmp_path, "big.mp4", payload=b"\x00\x00\x00\x18ftypmp42" + b"X" * 200)
    monkeypatch.setitem(DEFAULT_CONFIG, "max_upload_size_bytes", 64)
    job2 = _enqueue_and_claim(store, big_vid, owner_id="w1")
    assert orch.process_job(job2, owner_id="w1") is False
    saved2 = store.get_job(job2.job_id)
    assert saved2.state == "failed"
    assert saved2.error_code == CloudErrorCode.INVALID_INPUT.value
    assert adapter.sessions_created == []
    monkeypatch.setitem(DEFAULT_CONFIG, "max_upload_size_bytes", 2 * 1024 * 1024 * 1024)

    # 3. Corrupted local wheel fails preflight with ENV_PACKAGE_MISSING and 0 CU
    fake_backend = tmp_path / "fake_backend"
    fake_scripts = fake_backend / "cloud" / "gpu_scripts"
    fake_scripts.mkdir(parents=True)
    real_scripts = BACKEND_ROOT / "cloud" / "gpu_scripts"
    for f in real_scripts.iterdir():
        if f.is_file():
            (fake_scripts / f.name).write_bytes(f.read_bytes())
    for core_name in (
        "ass_utils.py",
        "subtitle_layout.py",
        "subtitle_text.py",
        "v1_ocr_geometry.py",
        "ocr_subtitle_locator.py",
        "v1_timing_solver.py",
        "v1_speech_guard.py",
    ):
        (fake_backend / core_name).write_bytes((BACKEND_ROOT / core_name).read_bytes())
    (fake_scripts / "capcut_tts_api-1.0.0-py3-none-any.whl").write_bytes(b"tampered-wheel-bytes")

    orch_bad = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])
    orch_bad.gpu_scripts_dir = fake_scripts
    ok_vid = _make_sample_video(tmp_path, "valid.mp4")
    job3 = _enqueue_and_claim(store, ok_vid, owner_id="w1")
    assert orch_bad.process_job(job3, owner_id="w1") is False
    saved3 = store.get_job(job3.job_id)
    assert saved3.state == "failed"
    assert saved3.error_code == CloudErrorCode.ENV_PACKAGE_MISSING.value
    assert adapter.sessions_created == []

    # 4. Restored wheel but missing V1 core module also fails preflight with ENV_PACKAGE_MISSING (0 CU)
    (fake_scripts / "capcut_tts_api-1.0.0-py3-none-any.whl").write_bytes(
        (real_scripts / "capcut_tts_api-1.0.0-py3-none-any.whl").read_bytes()
    )
    (fake_backend / "v1_speech_guard.py").unlink()
    job4 = _enqueue_and_claim(store, ok_vid, owner_id="w1")
    assert orch_bad.process_job(job4, owner_id="w1") is False
    saved4 = store.get_job(job4.job_id)
    assert saved4.state == "failed"
    assert saved4.error_code == CloudErrorCode.ENV_PACKAGE_MISSING.value
    assert adapter.sessions_created == []


def test_orchestrator_bundles_manifests_passes_setup_flags_and_preserves_source(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))

    src_vid = _make_sample_video(tmp_path / "input", "source_keep.mp4")
    expected_sha = _sha256_file(src_vid)

    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    adapter = _SpyCloudAdapter()
    orch = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])

    job = _enqueue_and_claim(store, src_vid, owner_id="w1")
    assert orch.process_job(job, owner_id="w1") is True

    # Inspect uploaded bundle_manifest.json & V1 modules inside cloud_bundle.tar.gz
    bundle_bytes = adapter.uploaded["cloud_bundle.tar.gz"]
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "cloud_env_manifest.json" in names
        assert "bundle_manifest.json" in names
        assert "requirements_cloud.txt" in names
        assert "setup_env.py" in names
        assert "full_pipeline.py" in names
        for core in (
            "ass_utils.py",
            "subtitle_layout.py",
            "subtitle_text.py",
            "v1_ocr_geometry.py",
            "ocr_subtitle_locator.py",
            "v1_timing_solver.py",
            "v1_speech_guard.py",
        ):
            assert core in names

        bm = json.loads(tar.extractfile("bundle_manifest.json").read().decode("utf-8"))
        assert bm["expected_gpu"] == "T4"
        assert bm["expected_video"]["remote_filename"] == "source_keep.mp4"
        assert bm["expected_video"]["size_bytes"] == src_vid.stat().st_size
        assert bm["expected_video"]["sha256"] == expected_sha

    setup_calls = [c for c in adapter.exec_calls if len(c["argv"]) >= 2 and c["argv"][1] == "setup_env.py"]
    assert len(setup_calls) == 1
    setup_argv = setup_calls[0]["argv"]
    assert "--expected-gpu" in setup_argv and "T4" in setup_argv
    assert "--min-disk-mb" in setup_argv
    assert "--verify-bundle" in setup_argv and "bundle_manifest.json" in setup_argv
    assert setup_calls[0]["timeout"] == int(DEFAULT_CONFIG["setup_timeout_seconds"])

    # Original local input file stays intact with identical SHA256
    assert src_vid.is_file()
    assert _sha256_file(src_vid) == expected_sha


def test_orchestrator_propagates_setup_error_code_and_cleans_up_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "out"))

    src_vid = _make_sample_video(tmp_path / "input", "fail_setup.mp4")
    setup_err_json = json.dumps({"ok": False, "error_code": "ENV_GPU_MISMATCH", "message": "detected CPU"})
    adapter = _SpyCloudAdapter(setup_exec_result=(False, f"log line\nSETUP_ENV_RESULT_JSON:{setup_err_json}"))
    store = CloudJobStore(db_path=str(tmp_path / "cloud.db"), rollout_controlled=False)
    orch = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])

    job = _enqueue_and_claim(store, src_vid, owner_id="w1")
    assert orch.process_job(job, owner_id="w1") is False

    saved = store.get_job(job.job_id)
    assert saved.state == "failed"
    assert saved.error_code == CloudErrorCode.ENV_GPU_MISMATCH.value
    assert "detected CPU" in saved.error
    assert saved.cleanup_status == "verified"
    assert len(adapter.sessions_stopped) == 1
