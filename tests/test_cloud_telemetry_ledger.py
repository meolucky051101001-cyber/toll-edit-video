"""Telemetry per stage + CU ledger theo usage_rate thực và CU giữ ấm (0 CU, adapter giả)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_contract import (
    GPU_CU_RATES,
    CloudCostMeasurementSource,
    CloudErrorCode,
    CloudLedgerStatus,
)
from backend.cloud.cloud_job_store import (
    CloudJobStore,
    _normalize_provider_usage_payload,
)
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.gpu_scripts import full_pipeline, setup_env


def _make_sample_video(tmp_path: Path, name: str = "sample.mp4") -> Path:
    video = tmp_path / name
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return video


def test_provider_snapshot_keeps_usage_rate_and_assignments(tmp_path: Path):
    snap = _normalize_provider_usage_payload(
        {"ok": True, "balance": 10.0, "usage_rate": "1.79", "active_assignments": 1}
    )
    assert snap["usage_rate"] == 1.79
    assert snap["active_assignments"] == 1
    assert _normalize_provider_usage_payload({"ok": True, "usage_rate": "bad"})["usage_rate"] is None

    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    stored = store.record_provider_usage_snapshot({"ok": True, "balance": 9.0, "usage_rate": 2.0, "active_assignments": 1})
    assert stored["usage_rate"] == 2.0
    assert store.get_provider_usage_snapshot()["usage_rate"] == 2.0


def test_warm_idle_usage_is_ledgered_and_counted_in_daily_budget(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    now = time.time()
    res = store.record_warm_idle_usage("warm-1", "T4", parked_at=now - 1800, ended_at=now, source_job_id="job-a")
    assert res["cu"] == round(0.5 * GPU_CU_RATES["T4"], 4)
    store.record_warm_idle_usage("warm-1", "T4", parked_at=now - 1800, ended_at=now)

    rows = store.list_cost_ledger(job_id="warm:warm-1")
    assert len(rows) == 1
    assert rows[0]["status"] == CloudLedgerStatus.RECONCILED.value
    assert rows[0]["task_type"] == "warm_idle"
    assert abs(store.get_cu_spent_today(now=now) - res["cu"]) < 1e-3


def test_store_recover_reclaim_ledgers_warm_idle(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    now = time.time()
    store.park_warm_session("warm-2", "T4", ttl_seconds=1.0, source_job_id="job-b", now=now - 3600)

    class Adapter:
        def get_sessions_status(self):
            return {"ok": True, "sessions": [{"name": "warm-2", "is_active": True}], "error": None}

        def stop_session(self, name):
            return True, "stopped"

        def verify_session_stopped(self, name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    store.recover(adapter=Adapter(), now=now)
    rows = store.list_cost_ledger(job_id="warm:warm-2")
    assert len(rows) == 1
    assert rows[0]["cu_observed"] > 1.0


def test_orchestrator_stop_of_parked_session_ledgers_idle(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)

    class Adapter:
        def verify_session_stopped(self, name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    orch = CloudOrchestrator(store=store, adapter=Adapter(), allowed_roots=[tmp_path])
    orch._warm_parked_at["warm-3"] = time.time() - 600
    assert orch._stop_warm_session_with_retry_guard("warm-3", "T4", source_job_id="job-c") is True
    rows = store.list_cost_ledger(job_id="warm:warm-3")
    assert len(rows) == 1 and rows[0]["cu_observed"] > 0.0
    assert "warm-3" not in orch._warm_parked_at


def test_runtime_ledger_switches_to_provider_usage_rate(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(0.6)
    video = _make_sample_video(tmp_path)
    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.4, cu_buffer=0.04)
    claimed = store.claim_job(job.job_id, owner_id="w-pr")
    assert claimed is not None

    class ProviderRateAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0, "usage_rate": 360000.0, "active_assignments": 1}

        def create_session(self, session_name, gpu="T4"):
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None):
            return True, "uploaded"

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None, cancel_check=None):
            time.sleep(0.03)
            if cancel_check and cancel_check():
                return False, "aborted_by_budget"
            return True, "ok"

        def verify_session_stopped(self, session_name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    with patch.dict(DEFAULT_CONFIG, {"runtime_budget_check_interval_seconds": 0.01}), patch.dict(
        GPU_CU_RATES, {"T4": 0.0001}
    ):
        orch = CloudOrchestrator(store=store, adapter=ProviderRateAdapter(), allowed_roots=[tmp_path])
        assert orch.process_job(claimed, owner_id="w-pr") is False

    finished = store.get_job(claimed.job_id)
    assert finished.error_code == CloudErrorCode.BUDGET_EXCEEDED.value
    ledger = store.list_cost_ledger(job_id=claimed.job_id)
    assert ledger[0]["measurement_source"] == CloudCostMeasurementSource.PROVIDER_RATE_ESTIMATE.value


def test_provider_rate_ignored_when_other_assignments_active(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(0.6)
    video = _make_sample_video(tmp_path)
    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.4, cu_buffer=0.04)
    claimed = store.claim_job(job.job_id, owner_id="w-ma")

    class MultiAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0, "usage_rate": 360000.0, "active_assignments": 2}

        def create_session(self, session_name, gpu="T4"):
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None):
            return True, "uploaded"

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None, cancel_check=None):
            time.sleep(0.03)
            if cancel_check and cancel_check():
                return False, "aborted_by_budget"
            return False, "remote failed"

        def verify_session_stopped(self, session_name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    with patch.dict(DEFAULT_CONFIG, {"runtime_budget_check_interval_seconds": 0.01}), patch.dict(
        GPU_CU_RATES, {"T4": 0.0001}
    ):
        orch = CloudOrchestrator(store=store, adapter=MultiAdapter(), allowed_roots=[tmp_path])
        assert orch.process_job(claimed, owner_id="w-ma") is False

    finished = store.get_job(claimed.job_id)
    assert finished.error_code != CloudErrorCode.BUDGET_EXCEEDED.value
    ledger = store.list_cost_ledger(job_id=claimed.job_id)
    assert ledger[0]["measurement_source"] == CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value


def test_full_pipeline_step_timings_accumulate():
    full_pipeline._STEP_TIMINGS.clear()
    full_pipeline._STEP_TIMING_STATE.update({"key": None, "started": None})
    full_pipeline._record_step_start(1, now=0.0)
    full_pipeline._record_step_start(2, now=2.0)
    full_pipeline._record_step_start(1, now=7.0)
    full_pipeline._close_step_timing(8.5)
    assert full_pipeline._STEP_TIMINGS == {"step_1": 3.5, "step_2": 5.0}
    full_pipeline._STEP_TIMINGS.clear()


def test_setup_env_result_includes_inherited_phase_seconds(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(setup_env.SETUP_PHASES_ENV, json.dumps({"bootstrap_runtime": 4.5}))
    setup_env._SETUP_PHASES.clear()
    setup_env._SETUP_PHASES.update(setup_env._inherited_setup_phases())
    setup_env._record_phase("verify_models", time.monotonic())
    assert json.loads(setup_env._child_env_with_phases()[setup_env.SETUP_PHASES_ENV])["bootstrap_runtime"] == 4.5
    out = tmp_path / "env_result.json"
    setup_env._emit_and_exit(
        workdir=tmp_path, manifest_out=out, started_at=time.monotonic(),
        ok=False, exit_code=1, error_code="X", message="m",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["phase_seconds"]["bootstrap_runtime"] == 4.5
    assert "verify_models" in payload["phase_seconds"]
    setup_env._SETUP_PHASES.clear()
