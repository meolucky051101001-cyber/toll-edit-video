"""
Phase 7 Acceptance Matrix & Canary Gate tests (100% isolated, fake adapters + tmp SQLite, 0 CU).

Covers all 10 groups in the Phase 7 acceptance matrix plus bounded Canary readiness
& Local-vs-Cloud parity comparator:
1. Input / Command safety (quotes, Unicode, '&', traversal, shell metacharacters)
2. Concurrency (2 workers racing claim, HTTP idempotency retry, stale attempt overwrite rejected)
3. Cancellation across every non-terminal state (queued -> validating) + cleanup failure handling
4. Recovery (crash before VM, crash between VM creation & register_session, after register, lease expiry, offline provider)
5. Cost Ledger (failed, cancelled, soft-deleted, multi-attempt retry, midnight crossing, reservation integrity)
6. Translation & CapCut TTS quality gates + --allow-no-speech forwarding
7. Deliverable safety (collision preservation, partial download blocked, ENOSPC disk full leaves no .part/corrupt output)
8. UI / API resilience (slow hung provider, concurrent /api/cloud/status polling, repeated cancel idempotency)
9. Local mode isolation (Cloud OFF or broken Cloud backend never breaks Local pipeline/config)
10. Secret redaction across provider errors, logs, SQLite DB, staging artifacts, and Canary reports
11. Bounded Canary readiness gate + 6-dimension Local-vs-Cloud comparator
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import errno
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from cloud import cloud_routes, cloud_worker, colab_bridge  # noqa: E402
from cloud.cloud_canary import (  # noqa: E402
    CanaryPreconditionError,
    compare_local_vs_cloud_deliverables,
    render_canary_report_markdown,
    verify_canary_readiness,
)
from cloud.cloud_config import DEFAULT_CONFIG, WORKSPACE_DIR  # noqa: E402
from cloud.cloud_contract import (  # noqa: E402
    CloudErrorCode,
    admit_and_enqueue_job,
    reset_provider_status_cache,
)
from cloud.cloud_job_store import CloudJobStore  # noqa: E402
from cloud.cloud_orchestrator import CloudOrchestrator  # noqa: E402
from cloud.cloud_security import CloudValidationError, validate_video_input  # noqa: E402
from cloud.gpu_scripts import full_pipeline  # noqa: E402
from main import LOCAL_TOKEN, app  # noqa: E402
import telegram_bot  # noqa: E402
from v1_feature_flags import get_feature_flags, set_feature_flags  # noqa: E402


AUTH_HEADERS = {"X-Local-Control-Token": LOCAL_TOKEN}



def _caption_evidence(count):
    return dict(version=1, passed=True, cue_count=count,
                verified_cues=[str(i+1) for i in range(count)], unresolved_cues=[],
                sampled_frames=2, caption_samples=0, empty_samples=2, source_mask_count=0)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sample_video(directory: Path, name: str = "sample.mp4", payload: bytes | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    content = payload if payload is not None else (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"v1data" * 8)
    p.write_bytes(content)
    return p


@pytest.fixture(autouse=True)
def _clean_singletons(monkeypatch):
    reset_provider_status_cache()
    old_w = cloud_worker._WORKER_INSTANCE
    if old_w is not None and old_w.is_running():
        old_w.stop(timeout=2.0)
    monkeypatch.setattr(cloud_worker, "_WORKER_INSTANCE", None)
    yield
    w = cloud_worker._WORKER_INSTANCE
    if w is not None and w.is_running():
        w.stop(timeout=2.0)
    reset_provider_status_cache()


def _make_success_adapter(
    *,
    dl_payload: bytes,
    remote_meta: dict,
    on_create=None,
    on_upload=None,
    on_exec=None,
    on_download=None,
    stop_ok: bool = True,
):
    exec_calls: list[dict] = []
    created_sessions: list[str] = []
    stopped_sessions: list[str] = []

    class _Adapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0, "usage_rate": 0.0, "active_assignments": 0}

        def get_sessions_status(self):
            return {
                "ok": True,
                "sessions": [
                    {"name": s, "is_active": s not in stopped_sessions}
                    for s in created_sessions
                    if s not in stopped_sessions
                ],
                "error": "",
            }

        def create_session(self, session_name, gpu="T4", cancel_check=None):
            created_sessions.append(session_name)
            if on_create:
                on_create(session_name)
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None, cancel_check=None):
            if on_upload:
                on_upload(session_name, local_path, remote_filename)
            return True, "uploaded"

        def exec_argv(self, session_name, argv, cwd="/content", env_vars=None, timeout=None, cancel_check=None):
            exec_calls.append({"session": session_name, "argv": list(argv), "env": dict(env_vars or {})})
            if on_exec:
                on_exec(session_name, argv, env_vars)
            return True, "ok"

        def download_file(self, session_name, remote_name, local_dir, cancel_check=None):
            if on_download:
                on_download(session_name, remote_name, local_dir)
            out_dir = Path(local_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            if remote_name == "output_video.mp4":
                out_p = out_dir / "output_video.mp4"
                out_p.write_bytes(dl_payload)
                return True, str(out_p)
            if remote_name == "result_metadata.json":
                meta_p = out_dir / "result_metadata.json"
                meta_p.write_text(json.dumps(remote_meta, ensure_ascii=False), encoding="utf-8")
                return True, str(meta_p)
            return False, ""

        def verify_session_stopped(self, session_name):
            if stop_ok:
                stopped_sessions.append(session_name)
                return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}
            return {
                "verified_stopped": False,
                "cleanup_status": "failed_retry_needed",
                "attempts": 2,
                "error": "Provider refused stop",
            }

    return _Adapter(), exec_calls, created_sessions, stopped_sessions


# ---------------------------------------------------------------------------
# Group 1: Input & Command Safety
# ---------------------------------------------------------------------------


def test_p7_01_input_and_command_matrix(tmp_path: Path, monkeypatch):
    # Special characters: single quote, Unicode Vietnamese + CJK, spaces, '&'
    special_name = "Clip 'Hà Nội' & café 中文.mp4"
    video = _make_sample_video(tmp_path, special_name)
    validated = validate_video_input(str(video), allowed_roots=[tmp_path], require_exists=True)
    assert validated.display_name == special_name

    # Rejected unsafe paths & shell metacharacters
    bad_dir = tmp_path / "subdir"
    bad_dir.mkdir()
    for bad in (
        str(bad_dir),
        str(tmp_path / ".." / "escape.mp4"),
        "file:///etc/passwd",
        f"{video}\nrm -rf /",
    ):
        with pytest.raises(CloudValidationError):
            validate_video_input(bad, allowed_roots=[tmp_path], require_exists=True)

    for evil_cmd in ("python3 a.py; id", "echo $(whoami)", "echo `id`", "a | b"):
        with pytest.raises(CloudValidationError):
            colab_bridge.exec_cmd("cjobp7input1", evil_cmd)

    # exec_argv preserves special chars without shell=True
    seen_runs: list[dict] = []
    fake_key = "AIzaSyPhase7FakeSecretKey_0123456789"
    with (
        patch.object(colab_bridge, "upload_file", return_value=(True, "ok")),
        patch.object(
            colab_bridge.subprocess,
            "run",
            side_effect=lambda cmd, **kw: seen_runs.append({"cmd": cmd, **kw}) or SimpleNamespace(returncode=0, stdout=b"ok", stderr=b""),
        ),
    ):
        ok, _ = colab_bridge.exec_argv(
            "cjobp7input1",
            ["python3", "full_pipeline.py", "--input", f"/content/{special_name}", "--voice", "BV562_streaming"],
            env_vars={"GEMINI_API_KEY": fake_key},
        )
    assert ok is True
    assert len(seen_runs) == 1
    runner_code = seen_runs[0]["input"].decode("utf-8")
    assert "shell=False" in runner_code and "shell=True" not in runner_code
    assert all(call.get("shell") is False for call in seen_runs)
    assert fake_key not in runner_code

    # End-to-end orchestrator preserves source file and delivers atomic output for special name
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    job, _ = admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path], claim_immediately=True, owner_id="w1")
    out_dir = tmp_path / "deliverables"
    out_dir.mkdir()
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))

    dl_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"rendered-special" * 6
    remote_meta = {
        "status": "completed",
        "segments_count": 2,
        "source_caption_evidence": _caption_evidence(2),
        "subtitle_timing": {"passed": True, "cue_count": 2},
        "translated_count": 2,
        "tts_verified_count": 2,
        "output_video_probe": {"video_codec": "h264", "audio_codec": "aac", "size_bytes": len(dl_bytes), "sha256": _sha256_bytes(dl_bytes)},
    }
    adapter, exec_calls, _, stopped = _make_success_adapter(dl_payload=dl_bytes, remote_meta=remote_meta)
    orch = CloudOrchestrator(store, adapter=adapter, allowed_roots=[tmp_path])
    monkeypatch.setattr(
        orch,
        "validate_downloaded_output",
        lambda dl_f, st_dir, **kw: {
            "sha256": _sha256_bytes(dl_bytes),
            "size_bytes": len(dl_bytes),
            "remote_metadata": remote_meta,
            "local_probe": {"duration_seconds": 6.0, "video_codec": "h264", "audio_codec": "aac"},
            "qc_report": {"passed": True},
        },
    )

    before_src_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    assert orch.process_job(job, owner_id="w1") is True
    assert hashlib.sha256(video.read_bytes()).hexdigest() == before_src_sha
    assert any("--input" in c["argv"] for c in exec_calls)
    assert len(stopped) == 1
    assert store.get_job(job.job_id).state == "completed"


# ---------------------------------------------------------------------------
# Group 2: Concurrency (2 workers, HTTP retry idempotency, stale attempt)
# ---------------------------------------------------------------------------


def test_p7_02_concurrency_two_workers_http_retry_and_stale_attempt(tmp_path: Path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "conc.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "get_usage", lambda: {"ok": True, "balance": 100.0, "usage_rate": 0.0, "active_assignments": 0})
    monkeypatch.setattr(cloud_routes, "get_sessions", lambda: [])

    class _NoopWorker:
        def is_running(self):
            return True

        def start(self):
            pass

        def get_current_job(self):
            return None

        def get_worker_state(self):
            return "running"

        def is_accepting_jobs(self):
            return True

        def stop(self, timeout=2.0):
            return True

    monkeypatch.setattr(cloud_worker, "_WORKER_INSTANCE", _NoopWorker())

    dummy = _make_sample_video(WORKSPACE_DIR, f"p7_conc_{uuid.uuid4().hex[:6]}.mp4")
    try:
        client = TestClient(app)
        payload = {"video_path": str(dummy), "gpu_type": "T4", "voice": "BV562_streaming"}
        headers = {**AUTH_HEADERS, "X-Idempotency-Key": "p7-http-retry-001"}

        def _post_enq():
            return client.post("/api/cloud/enqueue", json=payload, headers=headers).json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(_post_enq) for _ in range(4)]
            responses = [f.result(timeout=5.0) for f in futs]

        job_ids = {r["job"]["job_id"] for r in responses}
        assert len(job_ids) == 1
        assert sum(1 for r in responses if r["deduplicated"] is False) == 1
        assert len(store.list_jobs()) == 1
        target_job_id = next(iter(job_ids))

        # Two workers race claim_next with Barrier
        barrier = threading.Barrier(2)

        def _race_claim(wid: str):
            barrier.wait(timeout=5.0)
            return store.claim_next(owner_id=wid, lease_seconds=0.1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_race_claim, "worker-alpha")
            f2 = pool.submit(_race_claim, "worker-beta")
            c1, c2 = f1.result(timeout=5.0), f2.result(timeout=5.0)

        claimed = [c for c in (c1, c2) if c is not None]
        assert len(claimed) == 1
        winner1 = claimed[0]
        assert winner1.job_id == target_job_id
        assert winner1.attempt == 1

        # Simulate lease expiry & requeue -> worker-2 claims attempt 2
        time.sleep(0.15)
        fake_adapter = SimpleNamespace(
            verify_session_stopped=lambda _s: {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}
        )
        assert store.release_expired_lease_for_retry(target_job_id, adapter=fake_adapter, now=time.time() + 5.0) is True
        winner2 = store.claim_next(owner_id="worker-gamma", lease_seconds=30.0)
        assert winner2 is not None
        assert winner2.attempt == 2

        # Stale worker attempt 1 cannot update progress, register session, or complete job
        assert store.register_session(target_job_id, 1, winner1.owner_id, "cjobstale1a1") is False
        assert (
            store.update_progress(
                target_job_id,
                state="completed",
                percent=100,
                owner_id=winner1.owner_id,
                attempt=1,
            )
            is False
        )
        cur = store.get_job(target_job_id)
        assert cur.attempt == 2
        assert cur.owner_id == "worker-gamma"
        assert cur.state == "provisioning"
    finally:
        dummy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Group 3: Cancel at EVERY state (queued -> validating) + cleanup failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cancel_stage",
    ["queued", "provisioning", "uploading", "preparing", "processing", "downloading", "validating_after_export"],
)
def test_p7_03_cancel_matrix_every_state_from_queued_to_validating(tmp_path: Path, monkeypatch, cancel_stage: str):
    store = CloudJobStore(db_path=tmp_path / f"cancel_{cancel_stage}.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path, f"v_{cancel_stage}.mp4")
    out_dir = tmp_path / "deliverables"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.5)

    if cancel_stage == "queued":
        req = store.request_cancel(job.job_id)
        assert req["cancelled"] is True and req["immediate"] is True
        assert store.get_job(job.job_id).state == "cancelled"
        assert store.claim_next(owner_id="w-late") is None
        assert store.update_progress(job.job_id, state="processing", percent=50) is False
        assert list(out_dir.glob("*.mp4")) == []
        return

    claimed = store.claim_next(owner_id="w1", lease_seconds=60.0)
    assert claimed is not None

    dl_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"cancel-dl" * 4
    remote_meta = {
        "status": "completed",
        "segments_count": 1,
        "source_caption_evidence": _caption_evidence(1),
        "subtitle_timing": {"passed": True, "cue_count": 1},
        "translated_count": 1,
        "tts_verified_count": 1,
        "output_video_probe": {"video_codec": "h264", "audio_codec": "aac", "size_bytes": len(dl_bytes), "sha256": _sha256_bytes(dl_bytes)},
    }

    adapter, _, _, stopped = _make_success_adapter(
        dl_payload=dl_bytes,
        remote_meta=remote_meta,
        on_create=(lambda _s: store.request_cancel(job.job_id)) if cancel_stage == "provisioning" else None,
        on_upload=(lambda _s, _lp, _rf: store.request_cancel(job.job_id)) if cancel_stage == "uploading" else None,
        on_exec=(
            lambda _s, argv, _env: store.request_cancel(job.job_id)
            if (
                (cancel_stage == "preparing" and any("setup_env.py" in str(a) for a in argv))
                or (cancel_stage == "processing" and any("full_pipeline.py" in str(a) for a in argv))
            )
            else None
        ),
        on_download=(
            lambda _s, rn, _ld: store.request_cancel(job.job_id)
            if cancel_stage == "downloading" and rn == "output_video.mp4"
            else None
        ),
    )

    orch = CloudOrchestrator(store, adapter=adapter, allowed_roots=[tmp_path])
    orig_export = orch.export_deliverable_atomic

    def _wrapped_export(*args, **kwargs):
        res_p = orig_export(*args, **kwargs)
        if cancel_stage == "validating_after_export":
            store.request_cancel(job.job_id)
        return res_p

    monkeypatch.setattr(orch, "export_deliverable_atomic", _wrapped_export)
    monkeypatch.setattr(
        orch,
        "validate_downloaded_output",
        lambda dl_f, st_dir, **kw: {
            "sha256": _sha256_bytes(dl_bytes),
            "size_bytes": len(dl_bytes),
            "remote_metadata": remote_meta,
            "local_probe": {"duration_seconds": 4.0, "video_codec": "h264", "audio_codec": "aac"},
            "qc_report": {"passed": True},
        },
    )

    ok = orch.process_job(claimed, owner_id="w1")
    assert ok is False
    final_job = store.get_job(job.job_id)
    assert final_job.state == "cancelled"
    assert final_job.cleanup_status == "verified"
    assert len(stopped) == 1
    assert list(out_dir.glob("*.mp4")) == []
    # Never revived by late update
    assert store.update_progress(job.job_id, state="completed", percent=100, owner_id="w1", attempt=1) is False


def test_p7_03b_cancel_cleanup_failure_never_marks_cancelled(tmp_path: Path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "cancel_fail_cleanup.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path, "cleanup_fail.mp4")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    job = store.enqueue(str(video), video_name=video.name)
    claimed = store.claim_next(owner_id="w1")

    adapter, _, _, stopped = _make_success_adapter(
        dl_payload=b"x",
        remote_meta={},
        on_create=lambda _s: store.request_cancel(job.job_id),
        stop_ok=False,
    )
    orch = CloudOrchestrator(store, adapter=adapter, allowed_roots=[tmp_path])
    assert orch.process_job(claimed, owner_id="w1") is False

    cur = store.get_job(job.job_id)
    assert cur.state == "cancelling"
    assert cur.cleanup_status == "failed_retry_needed"
    assert stopped == []
    assert len(store.list_pending_cleanup_events()) == 1


# ---------------------------------------------------------------------------
# Group 4: Recovery (before VM, between create & register, after register, offline)
# ---------------------------------------------------------------------------


def test_p7_04_recovery_crash_before_after_vm_lease_expiry_and_provider_offline(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "recover.db", rollout_controlled=False)
    store.set_concurrency_limit(3)
    video = _make_sample_video(tmp_path)

    # 1. Crash BEFORE VM creation (no session on provider)
    j_before = store.enqueue(str(video), video_name="before_vm.mp4")
    c_before = store.claim_next(owner_id="dead-1")

    # 2. Crash AFTER create_session created VM on provider, BEFORE session_name written to SQLite
    j_orphan_unreg = store.enqueue(str(video), video_name="unreg_vm.mp4")
    c_orphan_unreg = store.claim_next(owner_id="dead-2")
    expected_unreg_session = CloudJobStore._derive_expected_session_name(c_orphan_unreg.job_id, c_orphan_unreg.attempt)

    # 3. Crash AFTER register_session
    j_reg = store.enqueue(str(video), video_name="reg_vm.mp4")
    c_reg = store.claim_next(owner_id="dead-3")
    assert store.register_session(c_reg.job_id, c_reg.attempt, "dead-3", "cjobreg0001a1") is True

    stopped_calls: list[str] = []

    class _ProviderAdapter:
        def __init__(self, online: bool):
            self.online = online
            self.live = {expected_unreg_session, "cjobreg0001a1"}

        def get_sessions_status(self):
            if not self.online:
                return {"ok": False, "sessions": [], "error": "WSL timeout"}
            return {"ok": True, "sessions": [{"name": n, "is_active": True} for n in self.live], "error": ""}

        def verify_session_stopped(self, sname):
            stopped_calls.append(sname)
            self.live.discard(sname)
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    online_adapter = _ProviderAdapter(online=True)
    res = store.recover(adapter=online_adapter)
    assert len(res) == 3
    assert set(stopped_calls) == {expected_unreg_session, "cjobreg0001a1"}
    assert store.get_job(j_before.job_id).state == "failed"
    assert store.get_job(j_before.job_id).error_code == CloudErrorCode.LEASE_EXPIRED.value
    assert store.get_job(j_orphan_unreg.job_id).session_name == expected_unreg_session
    assert store.get_job(j_orphan_unreg.job_id).cleanup_status == "verified"
    assert store.get_job(j_reg.job_id).cleanup_status == "verified"

    # 4. Offline provider must mark provider_unreachable and keep job/cleanup open for reconciliation
    j_off = store.enqueue(str(video), video_name="offline.mp4")
    c_off = store.claim_next(owner_id="dead-4")
    assert store.register_session(c_off.job_id, c_off.attempt, "dead-4", "cjoboffl01a1") is True

    off_res = store.recover(adapter=_ProviderAdapter(online=False))
    assert any(r["job_id"] == j_off.job_id and r["cleanup_status"] == "provider_unreachable" for r in off_res)
    assert store.get_job(j_off.job_id).cleanup_status == "provider_unreachable"
    assert len(store.list_pending_cleanup_events()) >= 1


# ---------------------------------------------------------------------------
# Group 5: Cost Ledger Integrity
# ---------------------------------------------------------------------------


def test_p7_05_cost_ledger_error_cancel_delete_retry_midnight_matrix(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "ledger.db", rollout_controlled=False)
    store.set_cu_budget_daily(20.0)
    video = _make_sample_video(tmp_path)

    # Failed job keeps CU in ledger
    j_fail = store.enqueue(str(video), video_name="fail_cost.mp4", cu_estimated=0.5)
    c_fail = store.claim_next(owner_id="w1")
    store.update_progress(c_fail.job_id, state="failed", cu_actual=1.40, cleanup_status="verified", owner_id="w1", attempt=1)

    # Cancelled + soft-deleted job still keeps ledger & daily spend
    j_can = store.enqueue(str(video), video_name="cancel_cost.mp4", cu_estimated=0.5)
    c_can = store.claim_next(owner_id="w1")
    assert store.register_session(c_can.job_id, c_can.attempt, "w1", "cjobcan1a1") is True
    store.request_cancel(c_can.job_id)
    store.record_cleanup_result(
        c_can.job_id, c_can.attempt, "cjobcan1a1",
        verified_stopped=True, cleanup_status="verified", attempts=1,
    )
    store.update_progress(
        c_can.job_id,
        state="cancelled",
        cu_actual=0.75,
        cleanup_status="verified",
        owner_id="w1",
        attempt=1,
        allow_from_cancel=True,
    )
    assert store.delete_job(c_can.job_id) is True

    # Multi-attempt retry
    j_ret = store.enqueue(str(video), video_name="retry_cost.mp4", cu_estimated=0.5)
    c_r1 = store.claim_next(owner_id="w1", lease_seconds=0.05)
    store.record_runtime_usage(c_r1.job_id, 1, 0.35)
    time.sleep(0.08)
    fake_adapter = SimpleNamespace(verify_session_stopped=lambda _s: {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None})
    assert store.release_expired_lease_for_retry(c_r1.job_id, adapter=fake_adapter, now=time.time() + 5.0) is True
    c_r2 = store.claim_next(owner_id="w2")
    for st in ("uploading", "preparing", "processing", "downloading", "validating"):
        assert store.update_progress(c_r2.job_id, state=st, owner_id="w2", attempt=2) is True
    assert store.update_progress(c_r2.job_id, state="completed", cu_actual=0.65, cleanup_status="verified", owner_id="w2", attempt=2) is True

    assert len(store.list_cost_ledger(job_id=j_ret.job_id)) == 2
    assert store.get_cu_spent_today() == pytest.approx(1.40 + 0.75 + 0.35 + 0.65, abs=1e-3)

    # Midnight crossing pro-rata allocation (Nov 14 23:30 -> Nov 15 00:30 ICT = 2.0 CU split 1.0/1.0)
    mid_store = CloudJobStore(db_path=tmp_path / "midnight.db", rollout_controlled=False)
    tz_ict = dt.timezone(dt.timedelta(hours=7))
    t_start = dt.datetime(2026, 11, 14, 23, 30, 0, tzinfo=tz_ict).timestamp()
    t_end = dt.datetime(2026, 11, 15, 0, 30, 0, tzinfo=tz_ict).timestamp()
    jm = mid_store.enqueue(str(video), video_name="midnight.mp4", cu_estimated=2.0)
    cm = mid_store.claim_next(owner_id="wm")
    with mid_store._connect() as db:
        db.execute(
            "UPDATE cloud_cost_ledger SET status='reconciled', job_state='completed', cleanup_verified=1, "
            "cu_observed=2.0, cu_reconciled=2.0, usage_started_at=?, usage_ended_at=? WHERE job_id=? AND attempt=?",
            (t_start, t_end, cm.job_id, cm.attempt),
        )
    assert mid_store.get_cu_spent_today(now=dt.datetime(2026, 11, 14, 12, 0, tzinfo=tz_ict).timestamp()) == pytest.approx(1.0, abs=1e-3)
    assert mid_store.get_cu_spent_today(now=dt.datetime(2026, 11, 15, 12, 0, tzinfo=tz_ict).timestamp()) == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Group 6: Translation & CapCut TTS Quality Gates + --allow-no-speech
# ---------------------------------------------------------------------------


def test_p7_06_translation_and_tts_quality_gates_and_allow_no_speech_forwarding(tmp_path: Path, monkeypatch):
    # Translation missing cue & CJK leakage blocked
    segs = [{"id": 1, "start": 0.0, "end": 1.0, "text": "你好"}, {"id": 2, "start": 1.2, "end": 2.4, "text": "世界"}]
    with pytest.raises(full_pipeline.PipelineStageError) as exc1:
        full_pipeline.validate_translation_items(segs, [{"id": 1, "text": "Xin chào"}])
    assert exc1.value.error_code == "TRANSLATION_FAILED"
    with pytest.raises(full_pipeline.PipelineStageError) as exc2:
        full_pipeline.validate_translation_items(segs, [{"id": 1, "text": "Xin chào"}, {"id": 2, "text": "世界"}])
    assert exc2.value.error_code == "TRANSLATION_FAILED"

    # Corrupt TTS audio rejected by validate_tts_audio_file
    bad_tts = tmp_path / "corrupt.mp3"
    bad_tts.write_bytes(b"not-valid-audio")
    with pytest.raises(ValueError):
        full_pipeline.validate_tts_audio_file(str(bad_tts))

    # Empty segments without allow_no_speech -> NO_SPEECH_DETECTED
    with pytest.raises(full_pipeline.PipelineStageError) as exc3:
        full_pipeline.step5_generate_tts([], "BV562_streaming", str(tmp_path / "tts"), allow_no_speech=False)
    assert exc3.value.error_code == "NO_SPEECH_DETECTED"

    # Orchestrator forwards --allow-no-speech when job.options["allow_no_speech"] is True
    store = CloudJobStore(db_path=tmp_path / "nospeech.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path, "silent.mp4")
    job, _ = admit_and_enqueue_job(
        store,
        video_path=str(video),
        allowed_roots=[tmp_path],
        claim_immediately=True,
        owner_id="w1",
        options={"allow_no_speech": True},
    )
    out_dir = tmp_path / "out_ns"
    out_dir.mkdir()
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(out_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "res_ns"))
    dl_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"nospeech" * 4
    remote_meta = {
        "status": "completed",
        "segments_count": 0,
        "source_caption_evidence": _caption_evidence(0),
        "subtitle_timing": {"passed": True, "cue_count": 0},
        "no_speech_mode": True,
        "output_video_probe": {"video_codec": "h264", "audio_codec": "aac", "size_bytes": len(dl_bytes), "sha256": _sha256_bytes(dl_bytes)},
    }
    adapter, exec_calls, _, _ = _make_success_adapter(dl_payload=dl_bytes, remote_meta=remote_meta)
    orch = CloudOrchestrator(store, adapter=adapter, allowed_roots=[tmp_path])
    monkeypatch.setattr(
        orch,
        "validate_downloaded_output",
        lambda dl_f, st_dir, **kw: {
            "sha256": _sha256_bytes(dl_bytes),
            "size_bytes": len(dl_bytes),
            "remote_metadata": remote_meta,
            "local_probe": {"duration_seconds": 3.0, "video_codec": "h264", "audio_codec": "aac"},
            "qc_report": {"passed": True},
        },
    )
    assert orch.process_job(job, owner_id="w1") is True
    pipeline_cmds = [c["argv"] for c in exec_calls if "full_pipeline.py" in c["argv"]]
    assert len(pipeline_cmds) == 1
    assert "--allow-no-speech" in pipeline_cmds[0]


# ---------------------------------------------------------------------------
# Group 7: Deliverable Safety (collision, partial download, disk full)
# ---------------------------------------------------------------------------


def test_p7_07_deliverable_collision_partial_download_and_disk_full(tmp_path: Path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "deliver.db", rollout_controlled=False)
    orch = CloudOrchestrator(store, adapter=SimpleNamespace(), allowed_roots=[tmp_path])

    staging = tmp_path / "staging"
    staging.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    # Existing legacy file at old name must never be overwritten
    existing_legacy = out_dir / "Cloud_demo.mp4"
    existing_legacy.write_bytes(b"LEGACY_KEEP_INTACT")

    good_dl = staging / "output_video.mp4"
    good_payload = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"good" * 8
    good_dl.write_bytes(good_payload)

    exported = orch.export_deliverable_atomic(
        good_dl,
        out_dir,
        job_id="cloud_p7_abc123",
        attempt=2,
        video_name="demo.mp4",
        expected_sha256=_sha256_bytes(good_payload),
    )
    assert exported.exists()
    assert "cloud_p7_abc123" in exported.name and "_a2" in exported.name
    assert existing_legacy.read_bytes() == b"LEGACY_KEEP_INTACT"

    # Partial / truncated download blocked with qc_report.json
    trunc_dir = tmp_path / "trunc"
    trunc_dir.mkdir()
    trunc_file = trunc_dir / "output_video.mp4"
    trunc_file.write_bytes(b"\x00\x00\x00\x18ftypmp42PARTIAL")
    (trunc_dir / "result_metadata.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "artifacts": {"output_video.mp4": {"size_bytes": 999999, "sha256": "deadbeef"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as exc_trunc:
        orch.validate_downloaded_output(trunc_file, trunc_dir)
    assert getattr(exc_trunc.value, "error_code", "") == CloudErrorCode.DOWNLOAD_FAILED.value
    assert (trunc_dir / "qc_report.json").is_file()

    # ENOSPC disk full during atomic export leaves no .part or corrupt deliverable
    def _enospc(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", _enospc)
    with pytest.raises(Exception) as exc_disk:
        orch.export_deliverable_atomic(
            good_dl,
            out_dir,
            job_id="cloud_p7_diskfull",
            attempt=1,
            video_name="diskfull.mp4",
            expected_sha256=_sha256_bytes(good_payload),
        )
    assert getattr(exc_disk.value, "error_code", "") == CloudErrorCode.ENV_DISK_INSUFFICIENT.value
    assert list(out_dir.glob("*.part*")) == []
    assert existing_legacy.read_bytes() == b"LEGACY_KEEP_INTACT"


# ---------------------------------------------------------------------------
# Group 8: UI / API Slow Provider, Concurrent Polling, Repeated Cancel
# ---------------------------------------------------------------------------


def test_p7_08_ui_slow_provider_polling_and_repeated_cancel_idempotency(tmp_path: Path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "ui.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)

    release_provider = threading.Event()

    def slow_usage():
        release_provider.wait(timeout=20.0)
        return {"ok": True, "balance": 55.0, "usage_rate": 0.0, "active_assignments": 0}

    def slow_sessions():
        release_provider.wait(timeout=20.0)
        return []

    monkeypatch.setattr(cloud_routes, "get_usage", slow_usage)
    monkeypatch.setattr(cloud_routes, "get_sessions", slow_sessions)

    client = TestClient(app)
    dummy = _make_sample_video(WORKSPACE_DIR, f"p7_ui_{uuid.uuid4().hex[:6]}.mp4")
    try:
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(lambda: client.get("/api/cloud/status").json()) for _ in range(12)]
            polls = [f.result(timeout=3.0) for f in futs]
        assert (time.monotonic() - t0) < 2.0
        assert all(p["status"] == "ok" and p["provider_cache"]["stale"] is True for p in polls)

        enq = client.post("/api/cloud/enqueue", json={"video_path": str(dummy)}, headers=AUTH_HEADERS)
        assert enq.status_code == 200
        job_id = enq.json()["job"]["job_id"]

        # Cancel 3 times repeatedly on the same job -> stays idempotent & consistent
        c1 = client.post("/api/cloud/job/cancel", json={"job_id": job_id}, headers=AUTH_HEADERS).json()
        c2 = client.post("/api/cloud/job/cancel", json={"job_id": job_id}, headers=AUTH_HEADERS).json()
        c3 = client.post("/api/cloud/job/cancel", json={"job_id": job_id}, headers=AUTH_HEADERS).json()
        assert c1["cancelled"] is True and c1["state"] == "cancelled"
        assert c2["cancelled"] is True and c2["state"] == "cancelled" and c2.get("idempotent") is True
        assert c3["cancelled"] is True and c3["state"] == "cancelled" and c3.get("idempotent") is True
    finally:
        release_provider.set()
        time.sleep(0.05)
        reset_provider_status_cache()
        dummy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Group 9: Local Isolation when Cloud is OFF or Cloud Backend Broken
# ---------------------------------------------------------------------------


def test_p7_09_local_mode_unaffected_when_cloud_off_or_cloud_backend_broken(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws_local"
    ws.mkdir()
    out_dir = tmp_path / "out_local"
    out_dir.mkdir()
    vid = _make_sample_video(ws, "local_only.mp4")

    monkeypatch.setattr(telegram_bot, "WORKSPACE", str(ws))
    monkeypatch.setattr(telegram_bot, "OUTPUT_DIR", str(out_dir))

    # This test isolates dispatch from media decoding; the input is a header fixture.
    from pipeline_v2 import download_validation
    monkeypatch.setattr(download_validation, "probe_downloaded_video", lambda *a, **kw: {})

    # Default clean workspace keeps V1_CLOUD_MODE False
    assert get_feature_flags(str(ws))["V1_CLOUD_MODE"] is False

    # Break Cloud modules completely to prove Local never imports/touches Cloud path
    from cloud import cloud_contract, cloud_job_store

    monkeypatch.setattr(cloud_job_store, "CloudJobStore", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Cloud DB broken")))
    monkeypatch.setattr(cloud_contract, "admit_and_enqueue_job", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Cloud contract broken")))

    local_calls = {"count": 0}

    async def fake_local_pipeline(v_path, job, status_msg):
        local_calls["count"] += 1
        final_f = out_dir / "Dubbed_local_only.mp4"
        final_f.write_bytes(b"local-ok")
        return {"status": "success", "final_video": str(final_f), "mode": "local"}

    monkeypatch.setattr(telegram_bot, "run_pipeline_v1_for_telegram", fake_local_pipeline)
    monkeypatch.setattr(
        telegram_bot,
        "run_pipeline_cloud_for_telegram",
        AsyncMock(side_effect=AssertionError("Cloud pipeline must NOT be called when V1_CLOUD_MODE is False")),
    )

    job = {
        "type": "local_file",
        "job_id": "tg_local_only_p7",
        "video_path": str(vid),
        "filename": vid.name,
        "pos": 1,
    }
    res = asyncio.run(telegram_bot._process_telegram_job(job))
    assert local_calls["count"] == 1
    assert res["status"] == "success"
    assert Path(res["final_video"]).is_file()
    assert get_feature_flags(str(ws))["V1_CLOUD_MODE"] is False


# ---------------------------------------------------------------------------
# Group 10: Secret Redaction Across Provider Errors, Logs, DB & Artifacts
# ---------------------------------------------------------------------------


def test_p7_10_secret_redaction_in_provider_errors_logs_db_and_artifacts(tmp_path: Path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    fake_gemini = "AIzaSyPhase7LeakCheckSecret9988776655"
    fake_bot_token = "987654321:AAHFakePhase7BotSecretTokenXYZ"
    fake_cookie = "Cookie: SAPISID=phase7_super_cookie_secret"

    monkeypatch.setenv("GEMINI_API_KEY", fake_gemini)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", fake_bot_token)

    store = CloudJobStore(db_path=tmp_path / "secrets.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path, "secret_test.mp4")
    job, _ = admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path], claim_immediately=True, owner_id="w1")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "res_sec"))

    class _LeakyErrorAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 50.0, "usage_rate": 0.0, "active_assignments": 0}

        def create_session(self, session_name, gpu="T4", cancel_check=None):
            return True, "ok"

        def upload_file(self, *a, **kw):
            return False, f"Upload error key={fake_gemini} token={fake_bot_token} {fake_cookie}"

        def verify_session_stopped(self, session_name):
            return {
                "verified_stopped": False,
                "cleanup_status": "failed_retry_needed",
                "attempts": 1,
                "error": f"cleanup failed GEMINI_API_KEY={fake_gemini} token={fake_bot_token}",
            }

    orch = CloudOrchestrator(store, adapter=_LeakyErrorAdapter(), allowed_roots=[tmp_path])
    assert orch.process_job(job, owner_id="w1") is False

    # DB row must NOT contain secrets
    db_job = store.get_job(job.job_id)
    for field_val in (db_job.error, db_job.step_name, db_job.cleanup_last_error):
        if field_val:
            for secret in (fake_gemini, fake_bot_token, "phase7_super_cookie_secret"):
                assert secret not in field_val

    # Logs must NOT contain secrets
    full_logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (fake_gemini, fake_bot_token, "phase7_super_cookie_secret"):
        assert secret not in full_logs

    # Canary markdown report must NOT contain secrets
    md = render_canary_report_markdown(
        {
            "passed": False,
            "local_video_path": str(video),
            "cloud_result_path": "",
            "dimensions": {},
            "failures": [f"Provider leaked GEMINI_API_KEY={fake_gemini} and token={fake_bot_token}"],
            "disclaimer": "sample only",
        }
    )
    assert fake_gemini not in md
    assert fake_bot_token not in md


# ---------------------------------------------------------------------------
# Group 11: Canary Readiness Gate & Local-vs-Cloud Parity Comparator
# ---------------------------------------------------------------------------


def test_p7_11_canary_readiness_gate_and_local_vs_cloud_parity_comparator(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "canary.db", rollout_controlled=False)
    store.set_cu_budget_daily(5.0)

    short_vid = _make_sample_video(tmp_path, "short_10s.mp4")
    special_vid = _make_sample_video(tmp_path, "Tên đặc biệt 'clip' & 01.mp4")
    cancel_vid = _make_sample_video(tmp_path, "cancel_probe.mp4")

    # 1. Blocked when user_approved=False
    with pytest.raises(CanaryPreconditionError):
        verify_canary_readiness(
            store,
            user_approved=False,
            max_cu_cap=2.0,
            short_video_path=short_vid,
            special_name_video_path=special_vid,
            cancel_case_video_path=cancel_vid,
            provider_usage={"ok": True, "balance": 10.0, "stale": False},
            allowed_roots=[tmp_path],
        )

    # 2. Blocked when cap exceeds daily remaining budget or stale provider
    with pytest.raises(CanaryPreconditionError):
        verify_canary_readiness(
            store,
            user_approved=True,
            max_cu_cap=6.0,
            short_video_path=short_vid,
            special_name_video_path=special_vid,
            cancel_case_video_path=cancel_vid,
            provider_usage={"ok": True, "balance": 10.0, "stale": False},
            allowed_roots=[tmp_path],
        )
    with pytest.raises(CanaryPreconditionError):
        verify_canary_readiness(
            store,
            user_approved=True,
            max_cu_cap=2.0,
            short_video_path=short_vid,
            special_name_video_path=special_vid,
            cancel_case_video_path=cancel_vid,
            provider_usage={"ok": False, "stale": True, "error": "offline"},
            allowed_roots=[tmp_path],
        )

    # 3. Blocked if special_name_video has plain ASCII name
    plain_vid = _make_sample_video(tmp_path, "plain_ascii.mp4")
    with pytest.raises(CanaryPreconditionError):
        verify_canary_readiness(
            store,
            user_approved=True,
            max_cu_cap=2.0,
            short_video_path=short_vid,
            special_name_video_path=plain_vid,
            cancel_case_video_path=cancel_vid,
            provider_usage={"ok": True, "balance": 10.0, "stale": False},
            allowed_roots=[tmp_path],
        )

    # 4. Passes readiness gate when approved, within cap, fresh balance, and 3 valid scenarios
    manifest = verify_canary_readiness(
        store,
        user_approved=True,
        max_cu_cap=2.0,
        short_video_path=short_vid,
        special_name_video_path=special_vid,
        cancel_case_video_path=cancel_vid,
        provider_usage={"ok": True, "balance": 10.0, "stale": False},
        allowed_roots=[tmp_path],
    )
    assert manifest["ready"] is True
    assert manifest["max_cu_cap"] == 2.0
    assert set(manifest["scenarios"].keys()) == {"short_video", "special_name_video", "cancel_case"}

    # 5. Parity comparator PASS case
    local_cues = [
        {"id": 1, "start": 0.0, "end": 1.5, "vi": "Xin chào các bạn.", "role": "female"},
        {"id": 2, "start": 1.6, "end": 3.0, "vi": "Hôm nay trời rất đẹp.", "role": "male"},
    ]
    cloud_cues = [
        {"id": 1, "start": 0.05, "end": 1.52, "vi": "Xin chào các bạn.", "speaker": "nu"},
        {"id": 2, "start": 1.62, "end": 3.05, "vi": "Hôm nay trời rất đẹp.", "speaker": "nam"},
    ]
    cloud_out = _make_sample_video(tmp_path, "cloud_out_canary.mp4")
    ok_report = compare_local_vs_cloud_deliverables(
        local_video_path=short_vid,
        local_cues=local_cues,
        cloud_manifest={
            "qc_passed": True,
            "result_path": str(cloud_out),
            "size_bytes": cloud_out.stat().st_size,
            "duration_seconds": 10.2,
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
        },
        cloud_cues=cloud_cues,
        local_probe={"duration_seconds": 10.0, "width": 1080, "height": 1920},
        ledger_entry={"status": "reconciled", "cu_reconciled": 0.45},
        cleanup_status="verified",
        max_cu_cap=2.0,
    )
    assert ok_report["passed"] is True
    assert ok_report["dimensions"]["voice_role_parity"]["generalizable_accuracy_claim"] is False
    assert "PASS" in render_canary_report_markdown(ok_report)

    # 6. Parity comparator FAIL case (missing cue, CJK leak, role flip, timing drift, bad cleanup, CU > cap)
    bad_report = compare_local_vs_cloud_deliverables(
        local_video_path=short_vid,
        local_cues=local_cues,
        cloud_manifest={
            "qc_passed": False,
            "result_path": "",
            "size_bytes": 0,
            "duration_seconds": 15.0,
            "video_codec": "h264",
            "audio_codec": None,
        },
        cloud_cues=[{"id": 1, "start": 1.2, "end": 3.5, "vi": "Xin chào 世界", "role": "male"}],
        local_probe={"duration_seconds": 10.0, "width": 1080, "height": 1920},
        ledger_entry={"status": "unreconciled", "cu_observed": 3.5},
        cleanup_status="failed_retry_needed",
        max_cu_cap=2.0,
    )
    assert bad_report["passed"] is False
    for key in ("cue_completeness", "voice_role_parity", "timing_parity", "video_quality", "cost_and_cleanup"):
        assert bad_report["dimensions"][key]["passed"] is False
    assert len(bad_report["failures"]) >= 5
