from __future__ import annotations

import sys
import json
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator


class _WarmAdapter:
    def exec_argv(self, session_name, argv, *, cwd="/content", timeout=None):
        return True, "ok"


@pytest.mark.parametrize(
    ("keepalive_seconds", "maximum_seconds", "expected_ttl"),
    [(300, 1800, 300), (3600, 1800, 1800)],
)
def test_warm_session_keepalive_is_parked_and_clamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keepalive_seconds: int,
    maximum_seconds: int,
    expected_ttl: int,
):
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_keepalive_seconds", keepalive_seconds)
    monkeypatch.setitem(DEFAULT_CONFIG, "warm_session_keepalive_max_seconds", maximum_seconds)
    store = CloudJobStore(db_path=tmp_path / "warm.db", rollout_controlled=False)
    store.set_cu_budget_daily(10.0)
    orchestrator = CloudOrchestrator(
        store=store,
        adapter=_WarmAdapter(),
        enable_warm_session_reuse=True,
    )

    assert orchestrator._park_warm_session_on_success("warm-session", "T4") is True
    parked = store.list_warm_sessions()[0]
    assert parked["expires_at"] - parked["parked_at"] == expected_ttl

    orchestrator._cancel_warm_timer("warm-session")
    store.remove_warm_session("warm-session")


def test_warm_session_keepalive_is_skipped_when_daily_cu_is_near_limit(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "warm-budget.db", rollout_controlled=False)
    store.set_cu_budget_daily(1.05)
    now = time.time()
    store.record_warm_idle_usage(
        "already-spent",
        "T4",
        parked_at=now - 60,
        ended_at=now,
        hourly_rate=60.0,
    )
    orchestrator = CloudOrchestrator(
        store=store,
        adapter=_WarmAdapter(),
        enable_warm_session_reuse=True,
    )

    assert orchestrator._park_warm_session_on_success("warm-session", "T4") is False
    assert store.list_warm_sessions() == []


def test_list_warm_sessions_includes_idle_seconds_and_cu_estimate(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "warm-list.db", rollout_controlled=False)
    now = time.time()
    store.park_warm_session("warm-session", "T4", ttl_seconds=3600, now=now - 1800)

    warm = store.list_warm_sessions(now=now)[0]

    assert warm["idle_seconds"] == pytest.approx(1800)
    assert warm["cu_rate_per_hour"] == pytest.approx(1.15)
    assert warm["idle_cu_estimate"] == pytest.approx(0.575)
    assert warm["expires_in_seconds"] == pytest.approx(1800)


def _run_monitored_with_poll_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict],
    *,
    max_poll: float,
    progress_callback=None,
):
    from backend.cloud import colab_bridge

    waits: list[float] = []
    monkeypatch.setattr(colab_bridge, "_write_wsl_temp_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(colab_bridge, "_remove_wsl_temp_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(colab_bridge, "_wait_interruptible", lambda seconds, **_kwargs: waits.append(seconds) or False)

    def fake_run(colab_args, **_kwargs):
        args = list(colab_args)
        script_path = args[args.index("-f") + 1]
        if "colab_launch_" in script_path:
            return 0, 'AUTODUB_LAUNCH_JSON:{"launched": true, "sup_pid": 4321}', ""
        payload = payloads.pop(0)
        return 0, "AUTODUB_POLL_JSON:" + json.dumps(payload), ""

    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", fake_run)
    result = colab_bridge.run_argv_monitored(
        "cjobwarmtest",
        ["python3", "pipeline.py"],
        timeout=120,
        poll_interval_seconds=5,
        max_poll_interval_seconds=max_poll,
        progress_callback=progress_callback,
    )
    return result, waits


def test_adaptive_polling_ignores_progress_callback_exceptions(monkeypatch: pytest.MonkeyPatch):
    payloads = [
        {
            "done": True,
            "progress": {"percent": 10, "message": "step"},
            "returncode": 0,
            "log_tail": "ok",
        }
    ]

    def raise_callback(_percent, _message):
        raise RuntimeError("progress callback failed")

    result, _waits = _run_monitored_with_poll_payloads(
        monkeypatch,
        payloads,
        max_poll=30,
        progress_callback=raise_callback,
    )

    assert result == (True, "ok")


def test_adaptive_polling_grows_for_unchanged_progress_and_caps(monkeypatch: pytest.MonkeyPatch):
    payloads = [
        {"done": False, "progress": None},
        {"done": False, "progress": None},
        {"done": False, "progress": None},
        {"done": False, "progress": None},
        {"done": True, "returncode": 0, "log_tail": "ok"},
    ]

    (ok, output), waits = _run_monitored_with_poll_payloads(monkeypatch, payloads, max_poll=12)

    assert ok is True
    assert output == "ok"
    assert waits == pytest.approx([5, 7.5, 11.25, 12, 12])


def test_adaptive_polling_resets_when_progress_changes(monkeypatch: pytest.MonkeyPatch):
    payloads = [
        {"done": False, "progress": {"percent": 10, "message": "step one"}},
        {"done": False, "progress": {"percent": 10, "message": "step one"}},
        {"done": False, "progress": {"percent": 20, "message": "step two"}},
        {"done": False, "progress": {"percent": 20, "message": "step two"}},
        {"done": True, "returncode": 0, "log_tail": "ok"},
    ]

    (ok, _output), waits = _run_monitored_with_poll_payloads(monkeypatch, payloads, max_poll=30)

    assert ok is True
    assert waits == pytest.approx([5, 5, 7.5, 5, 7.5])


class _UploadRetryAdapter:
    pass


def _run_upload_retry_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter,
    *,
    cancel_event: threading.Event | None = None,
):
    from tests.test_cloud_phase5_quality_export import (
        _Phase5Adapter,
        _enqueue_and_claim,
        _make_sample_video,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "deliverables"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_upload_retry_delays_seconds", [0, 0])
    source = _make_sample_video(tmp_path / "input", "upload_retry.mp4")
    store = CloudJobStore(db_path=tmp_path / "upload-retry.db", rollout_controlled=False)
    orchestrator = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])
    job = _enqueue_and_claim(store, source)
    result = orchestrator.process_job(job, owner_id="w1", cancel_event=cancel_event)
    return result, store.get_job(job.job_id), _Phase5Adapter


def test_upload_retries_twice_then_job_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter

    class RetryAdapter(_Phase5Adapter):
        def __init__(self):
            super().__init__()
            self.upload_attempts = 0

        def upload_file(self, session_name, local_path, remote_filename=None):
            self.upload_attempts += 1
            if self.upload_attempts <= 2:
                return False, "temporary upload failure"
            return super().upload_file(session_name, local_path, remote_filename)

    adapter = RetryAdapter()
    result, saved, _ = _run_upload_retry_job(tmp_path, monkeypatch, adapter)

    assert result is True
    assert saved is not None and saved.state == "completed"
    assert adapter.upload_attempts == 4


def test_upload_retries_exhaust_with_existing_upload_error_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend.cloud.cloud_contract import CloudErrorCode
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter

    class RetryAdapter(_Phase5Adapter):
        def __init__(self):
            super().__init__()
            self.upload_attempts = 0

        def upload_file(self, session_name, local_path, remote_filename=None):
            self.upload_attempts += 1
            return False, "temporary upload failure"

    adapter = RetryAdapter()
    result, saved, _ = _run_upload_retry_job(tmp_path, monkeypatch, adapter)

    assert result is False
    assert saved is not None and saved.state == "failed"
    assert saved.error_code == CloudErrorCode.UPLOAD_FAILED.value
    assert adapter.upload_attempts == 3


def test_cancel_during_upload_retry_prevents_another_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend.cloud import cloud_orchestrator
    from tests.test_cloud_phase5_quality_export import _Phase5Adapter

    class RetryAdapter(_Phase5Adapter):
        def __init__(self):
            super().__init__()
            self.upload_attempts = 0

        def upload_file(self, session_name, local_path, remote_filename=None):
            self.upload_attempts += 1
            return False, "temporary upload failure"

    cancel_event = threading.Event()

    def cancel_retry(_seconds, **_kwargs):
        cancel_event.set()
        return True

    monkeypatch.setattr(cloud_orchestrator, "_wait_interruptible", cancel_retry)
    adapter = RetryAdapter()
    result, saved, _ = _run_upload_retry_job(
        tmp_path,
        monkeypatch,
        adapter,
        cancel_event=cancel_event,
    )

    assert result is False
    assert saved is not None and saved.state == "cancelled"
    assert adapter.upload_attempts == 1


def test_budget_abort_during_upload_retry_is_not_user_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.cloud import cloud_orchestrator
    from backend.cloud.cloud_contract import CloudErrorCode
    from tests.test_cloud_phase5_quality_export import (
        _Phase5Adapter,
        _enqueue_and_claim,
        _make_sample_video,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestSecretKey_1234567890")
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "deliverables"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_upload_retry_delays_seconds", [0])
    monkeypatch.setitem(DEFAULT_CONFIG, "runtime_budget_check_interval_seconds", 0.01)

    source = _make_sample_video(tmp_path / "input", "budget_retry.mp4")
    store = CloudJobStore(db_path=tmp_path / "budget-retry.db", rollout_controlled=False)
    store.set_cu_budget_daily(0.6)
    job = _enqueue_and_claim(store, source)
    in_backoff = [False]
    record_runtime_usage = store.record_runtime_usage

    def trip_budget_during_backoff(*args, **kwargs):
        result = record_runtime_usage(*args, **kwargs)
        if in_backoff[0]:
            return {**result, "within_budget": False}
        return result

    monkeypatch.setattr(store, "record_runtime_usage", trip_budget_during_backoff)

    class RetryAdapter(_Phase5Adapter):
        def __init__(self):
            super().__init__()
            self.upload_attempts = 0

        def upload_file(self, session_name, local_path, remote_filename=None):
            self.upload_attempts += 1
            return False, "temporary upload failure"

    def interruptible_budget_backoff(_seconds, *, session_name=None, cancel_check=None):
        in_backoff[0] = True
        time.sleep(0.02)
        assert cancel_check is not None and cancel_check()
        return True

    monkeypatch.setattr(cloud_orchestrator, "_wait_interruptible", interruptible_budget_backoff)
    adapter = RetryAdapter()
    orchestrator = CloudOrchestrator(store=store, adapter=adapter, allowed_roots=[tmp_path])

    assert orchestrator.process_job(job, owner_id="w1") is False
    saved = store.get_job(job.job_id)
    assert saved is not None and saved.error_code == CloudErrorCode.BUDGET_EXCEEDED.value
    assert saved.error_code != CloudErrorCode.CANCELLED_BY_USER.value
    assert adapter.upload_attempts == 1
