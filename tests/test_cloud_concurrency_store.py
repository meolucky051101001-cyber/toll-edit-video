"""Offline regression tests for the shared Cloud concurrency admission gate."""
import concurrent.futures
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from cloud.cloud_job_store import CloudJobStore, MAX_CONCURRENT_JOBS


def _store(tmp_path):
    store = CloudJobStore(tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(100)
    return store


def test_shared_limit_defaults_to_one_and_persists(tmp_path):
    store = _store(tmp_path)
    assert store.get_concurrency_limit() == 1
    assert store.set_concurrency_limit(3) == 3
    peer = CloudJobStore(store.db_path, rollout_controlled=False)
    assert peer.get_concurrency_limit() == 3
    first = store.enqueue("first.mp4")
    assert peer.claim_job(first.job_id, "peer") is not None
    # Reducing capacity is graceful: the active lease stays intact.
    store.set_concurrency_limit(1)
    second = store.enqueue("second.mp4")
    assert peer.claim_next() is None
    assert peer.claim_job(second.job_id, "peer-2") is None
    assert [job.job_id for job in store.list_active_jobs()] == [first.job_id]
    assert store.get_job(first.job_id).state == "provisioning"


@pytest.mark.parametrize("value", [0, MAX_CONCURRENT_JOBS + 1, True, False, 1.5, "2", None])
def test_invalid_limits_are_rejected_without_changing_setting(tmp_path, value):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set_concurrency_limit(value)
    assert store.get_concurrency_limit() == 1


def test_atomic_capacity_across_independent_store_connections(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(3)
    for number in range(8):
        store.enqueue(f"video-{number}.mp4")
    peers = [CloudJobStore(store.db_path, rollout_controlled=False) for _ in range(8)]
    gate = threading.Barrier(len(peers))

    def claim(peer):
        gate.wait(timeout=10)
        return peer.claim_next()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(peers)) as pool:
        claimed = list(pool.map(claim, peers))
    job_ids = [job.job_id for job in claimed if job is not None]
    assert len(job_ids) == len(set(job_ids)) == 3
    assert len(store.list_active_jobs()) == 3
    assert len(store.list_cost_ledger()) == 3


@pytest.mark.parametrize("gpu, limit", [("T4", 2), ("L4", 3)])
def test_gpu_limit_applies_to_atomic_claims_across_store_connections(tmp_path, gpu, limit):
    store = _store(tmp_path)
    store.set_concurrency_limit(3)
    store.set_gpu_concurrency_limits({"T4": 2, "L4": 3})
    peers = [CloudJobStore(store.db_path, rollout_controlled=False) for _ in range(5)]
    jobs = [store.enqueue(f"{gpu}-{index}.mp4", gpu_type=gpu) for index in range(len(peers))]
    gate = threading.Barrier(len(peers))

    def claim(pair):
        peer, job = pair
        gate.wait(timeout=10)
        return peer.claim_job(job.job_id, f"worker-{job.job_id}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(peers)) as pool:
        claimed = list(pool.map(claim, zip(peers, jobs)))
    assert len([job for job in claimed if job]) == limit
    assert peers[0].get_gpu_concurrency_limits()[gpu] == limit


def test_saturated_gpu_does_not_block_another_gpu_in_the_queue(tmp_path):
    store = _store(tmp_path)
    store.set_gpu_concurrency_limits({"T4": 2, "L4": 3})
    first, second, waiting = [store.enqueue(f"t4-{n}.mp4", gpu_type="T4") for n in range(3)]
    l4 = store.enqueue("l4.mp4", gpu_type="L4")
    assert store.claim_next().job_id == first.job_id
    assert store.claim_next().job_id == second.job_id
    assert store.claim_next().job_id == l4.job_id
    assert store.get_job(waiting.job_id).state == "queued"
    assert store.claim_next() is None


def test_exact_job_and_channel_exclusion_share_capacity(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    telegram = store.enqueue("telegram.mp4", options={"source_channel": "cloud_telegram_bot"})
    legacy_telegram = store.enqueue("legacy-telegram.mp4", idempotency_key="cloudbot:legacy")
    dashboard = store.enqueue("dashboard.mp4")
    last = store.enqueue("last.mp4")
    assert store.claim_next(exclude_source_channel="cloud_telegram_bot").job_id == dashboard.job_id
    assert store.claim_next(job_id=telegram.job_id, owner_id="telegram").job_id == telegram.job_id
    assert store.claim_next(exclude_source_channel="cloud_telegram_bot") is None
    assert store.get_job(legacy_telegram.job_id).state == "queued"
    assert store.claim_next(job_id=last.job_id) is None
    assert store.get_job(last.job_id).state == "queued"
    assert store.claim_next(job_id="missing") is None


def test_lowering_shared_limit_waits_for_active_dashboard_and_bot_jobs(tmp_path):
    store = _store(tmp_path)
    web = CloudJobStore(store.db_path, rollout_controlled=False)
    bot = CloudJobStore(store.db_path, rollout_controlled=False)
    web.set_concurrency_limit(3)
    dash = store.enqueue("dashboard.mp4")
    tele = store.enqueue("telegram.mp4", options={"source_channel": "cloud_telegram_bot"})
    waiting = store.enqueue("waiting.mp4", options={"source_channel": "cloud_telegram_bot"})
    assert web.claim_next("web", exclude_source_channel="cloud_telegram_bot").job_id == dash.job_id
    assert bot.claim_next("bot", job_id=tele.job_id)

    web.set_concurrency_limit(1)
    assert {job.job_id for job in bot.list_active_jobs()} == {dash.job_id, tele.job_id}
    assert bot.claim_next("bot", job_id=waiting.job_id) is None
    assert web.update_progress(dash.job_id, state="failed", owner_id="web", attempt=1)
    assert bot.claim_next("bot", job_id=waiting.job_id) is None
    assert bot.update_progress(tele.job_id, state="failed", owner_id="bot", attempt=1)
    assert bot.claim_next("bot", job_id=waiting.job_id).job_id == waiting.job_id


def test_immediate_claim_cannot_bypass_shared_capacity(tmp_path):
    store = _store(tmp_path)
    store.enqueue("busy.mp4")
    assert store.claim_next() is not None
    with pytest.raises(Exception) as error:
        store.enqueue_or_get("new.mp4", immediate_owner_id="second")
    assert "cấp lượt" in str(error.value)
    assert len(store.list_jobs()) == 1


def test_concurrent_provider_cu_reservations_do_not_reuse_same_balance(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    first = store.enqueue("first.mp4", cu_estimated=0.4, cu_buffer=0.0)
    second = store.enqueue("second.mp4", cu_estimated=0.4, cu_buffer=0.0)
    usage = {"ok": True, "balance": 0.6}
    assert store.claim_job(first.job_id, "one", provider_usage=usage) is not None
    assert store.claim_job(second.job_id, "two", provider_usage=usage) is None
    rejection = store.consume_last_claim_rejection(second.job_id)
    assert rejection["code"] == "INSUFFICIENT_CU"
    assert len(store.list_active_jobs()) == 1
    assert store.get_job(second.job_id).state == "queued"


def _admit_dashboard(store, key):
    from cloud.cloud_contract import admit_and_enqueue_job
    permit = store.set_rollout(arm=True)["permit_id"]
    return admit_and_enqueue_job(
        store, video_path="https://www.tiktok.com/@sample/video/123456789",
        source_channel="dashboard", rollout_permit=permit, idempotency_key=key,
    )[0]


@pytest.mark.parametrize("limit", [2, 3, 4])
def test_dashboard_rearm_and_claim_with_healthy_registered_sessions(tmp_path, limit):
    store = CloudJobStore(tmp_path / "healthy.db")
    store.set_concurrency_limit(limit)
    for index in range(limit):
        job = _admit_dashboard(store, f"dash-healthy-{index}")
        claimed = store.claim_job(job.job_id, f"owner-{index}")
        assert claimed is not None
        assert store.register_session(job.job_id, claimed.attempt, claimed.owner_id, f"audit-session-{index}")
    assert len(store.list_active_jobs()) == limit
    with pytest.raises(Exception, match="hoàn tất job"):
        store.set_rollout(arm=True)


def test_dashboard_claim_after_other_lane_registers_session(tmp_path):
    store = CloudJobStore(tmp_path / "queued_before_registration.db")
    store.set_concurrency_limit(2)
    first = _admit_dashboard(store, "dash-first-before-register")
    claimed = store.claim_job(first.job_id, "owner-a")
    second = _admit_dashboard(store, "dash-second-before-register")
    assert store.register_session(first.job_id, claimed.attempt, claimed.owner_id, "audit-session-a")
    peer = CloudJobStore(store.db_path)
    assert peer.claim_job(second.job_id, "owner-b") is not None


@pytest.mark.parametrize("mutation", [
    "UPDATE cloud_jobs SET lease_expires_at = 0",
    "UPDATE cloud_jobs SET deadline_at = 0",
    "UPDATE cloud_jobs SET owner_id = 'other-owner'",
    "UPDATE cloud_jobs SET state = 'cancelling'",
    "UPDATE cloud_cleanup_events SET attempt = attempt + 1",
    "UPDATE cloud_cleanup_events SET attempts = 1",
    "UPDATE cloud_cleanup_events SET status = 'failed'",
    "UPDATE cloud_cost_ledger SET status = 'unreconciled'",
])
def test_unhealthy_session_or_cleanup_debt_still_blocks_dashboard(tmp_path, mutation):
    store = CloudJobStore(tmp_path / "unsafe.db")
    store.set_concurrency_limit(2)
    first = _admit_dashboard(store, "dash-unsafe-first")
    claimed = store.claim_job(first.job_id, "owner-a")
    assert store.register_session(first.job_id, claimed.attempt, claimed.owner_id, "audit-session-a")
    with store._connect() as db:
        db.execute(mutation)
        db.commit()
    with pytest.raises(Exception, match="hoàn tất job"):
        store.set_rollout(arm=True)


def test_dashboard_can_arm_while_telegram_occupies_another_lane(tmp_path):
    telegram = _store(tmp_path)
    telegram.set_concurrency_limit(2)
    first = telegram.enqueue("telegram.mp4", options={"source_channel": "cloud_telegram_bot"})
    claimed = telegram.claim_job(first.job_id, "telegram-owner")
    assert telegram.register_session(first.job_id, claimed.attempt, claimed.owner_id, "audit-telegram-session")
    dashboard = CloudJobStore(telegram.db_path)
    assert dashboard.get_rollout()["phase"] == "paused"
    second = _admit_dashboard(dashboard, "dash-beside-telegram")
    assert dashboard.claim_job(second.job_id, "dashboard-owner") is not None


def test_stale_claim_payload_cannot_overwrite_a_newer_lower_balance(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    first = store.enqueue("first.mp4", cu_estimated=0.4, cu_buffer=0.0)
    second = store.enqueue("second.mp4", cu_estimated=0.4, cu_buffer=0.0)
    old_usage = {"ok": True, "balance": 1.0}
    assert store.claim_job(first.job_id, "one", provider_usage=old_usage) is not None
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6})
    assert store.claim_job(second.job_id, "two", provider_usage=old_usage) is None
    assert store.get_provider_usage_snapshot()["balance"] == pytest.approx(0.6)
    assert store.consume_last_claim_rejection(second.job_id)["code"] == "INSUFFICIENT_CU"


def test_out_of_order_provider_sample_cannot_restore_balance(tmp_path):
    import time
    store = _store(tmp_path)
    now = time.time()
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6, "observed_at": now}, now=now)
    returned = store.record_provider_usage_snapshot(
        {"ok": True, "balance": 10, "observed_at": now - 60}, now=now + 1,
    )
    assert returned["balance"] == pytest.approx(0.6)
    assert returned["observed_at"] == now
    newer = store.record_provider_usage_snapshot(
        {"ok": True, "balance": 2.0, "observed_at": now + 2}, now=now + 2,
    )
    assert newer["balance"] == pytest.approx(2.0)


@pytest.mark.parametrize("replayed_balance", [0.3, 10.0])
def test_untimestamped_claim_payload_does_not_refresh_expired_snapshot(tmp_path, replayed_balance):
    import time
    store = _store(tmp_path)
    now = time.time()
    store.record_provider_usage_snapshot({"ok": True, "balance": 1.0, "age_seconds": 10000}, now=now)
    job = store.enqueue("queued.mp4", cu_estimated=0.2, cu_buffer=0.0)
    assert store.claim_job(job.job_id, "worker", provider_usage={"ok": True, "balance": replayed_balance}) is None
    assert store.get_provider_usage_snapshot()["is_stale"] is True
    assert store.consume_last_claim_rejection(job.job_id)["code"] == "BUDGET_UNCERTAIN"


def test_preflight_rechecks_total_holds_without_double_reserving_daily_budget(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    store.set_cu_budget_daily(0.8)
    for index in range(2):
        job = store.enqueue(f"video-{index}.mp4", cu_estimated=0.4, cu_buffer=0.0)
        assert store.claim_job(job.job_id, f"worker-{index}", provider_usage={"ok": True, "balance": 2.0}) is not None
    assert store.check_reserved_provider_balance()["allowed"] is True
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6})
    gate = store.check_reserved_provider_balance()
    assert gate["allowed"] is False
    assert gate["code"] == "INSUFFICIENT_CU"
    assert len(store.list_active_jobs()) == 2


def test_explicit_dashboard_channel_overrides_legacy_telegram_key_for_worker(tmp_path):
    store = _store(tmp_path)
    dashboard = store.enqueue("dashboard.mp4", options={"source_channel": "dashboard"}, idempotency_key="cloudbot:dashboard-audit")
    assert store.claim_next(exclude_source_channel="cloud_telegram_bot").job_id == dashboard.job_id


@pytest.mark.parametrize("invalid_time", ["bad", float("nan"), float("inf"), "2099-01-01T00:00:00+00:00"])
def test_invalid_snapshot_timestamp_cannot_restore_spending_authority(tmp_path, invalid_time):
    import time
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    first = store.enqueue("first.mp4", cu_estimated=0.4, cu_buffer=0.0)
    second = store.enqueue("second.mp4", cu_estimated=0.4, cu_buffer=0.0)
    assert store.claim_job(first.job_id, "one", provider_usage={"ok": True, "balance": 1.0})
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6, "observed_at": time.time()})
    assert store.claim_job(second.job_id, "two", provider_usage={"ok": True, "balance": 10, "observed_at": invalid_time}) is None
    assert store.get_provider_usage_snapshot()["balance"] == pytest.approx(0.6)


@pytest.mark.parametrize("invalid_balance", [None, "bad", float("nan"), float("inf"), -1])
def test_unknown_balance_at_same_timestamp_cannot_disable_budget_gate(tmp_path, invalid_balance):
    import time
    store = _store(tmp_path)
    now = time.time()
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6, "observed_at": now}, now=now)
    store.record_provider_usage_snapshot({"ok": True, "balance": invalid_balance, "observed_at": now}, now=now)
    job = store.enqueue("unknown.mp4", cu_estimated=0.2, cu_buffer=0.0)
    assert store.claim_job(job.job_id, "worker") is None
    assert store.get_provider_usage_snapshot()["ok"] is False


def test_same_timestamp_cannot_clear_explicit_stale_flag(tmp_path):
    import time
    store = _store(tmp_path)
    now = time.time()
    store.record_provider_usage_snapshot({"ok": True, "balance": 1, "observed_at": now, "stale": True}, now=now)
    restored = store.record_provider_usage_snapshot({"ok": True, "balance": 1, "observed_at": now, "stale": False}, now=now)
    assert restored["is_stale"] is True


def test_telegram_claim_and_preflight_cannot_ignore_pending_cleanup(tmp_path):
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    first = store.enqueue("first.mp4")
    claimed = store.claim_job(first.job_id, "one")
    assert store.register_session(first.job_id, claimed.attempt, claimed.owner_id, "audit-cancelling-session")
    store.request_cancel(first.job_id)
    second = store.enqueue("second.mp4", options={"source_channel": "cloud_telegram_bot"})
    assert store.claim_job(second.job_id, "telegram") is None
    assert store.check_reserved_provider_balance()["allowed"] is False


@pytest.mark.parametrize("invalid_age", ["bad", float("nan"), float("inf")])
def test_ingress_stamp_does_not_turn_invalid_age_into_a_fresh_balance(tmp_path, monkeypatch, invalid_age):
    from cloud import cloud_routes
    store = _store(tmp_path)
    store.set_concurrency_limit(2)
    first = store.enqueue("first.mp4", cu_estimated=0.4, cu_buffer=0.0)
    second = store.enqueue("second.mp4", cu_estimated=0.4, cu_buffer=0.0)
    assert store.claim_job(first.job_id, "one", provider_usage={"ok": True, "balance": 1})
    store.record_provider_usage_snapshot({"ok": True, "balance": 0.6})
    monkeypatch.setattr(cloud_routes, "get_usage", lambda: {"ok": True, "balance": 10, "age_seconds": invalid_age})
    sample = cloud_routes._read_provider_usage_observation()
    assert "observed_at" not in sample
    assert store.claim_job(second.job_id, "two", provider_usage=sample) is None
    assert store.get_provider_usage_snapshot()["balance"] == pytest.approx(0.6)


@pytest.mark.parametrize("bad_fields", [
    {"observed_at": float("inf")}, {"observed_at": "bad"},
    {"balance": None}, {"balance": float("nan")}, {"balance": -1},
])
def test_legacy_invalid_snapshot_is_blocked_and_can_be_replaced_by_real_observation(tmp_path, bad_fields):
    import json
    import time
    store = _store(tmp_path)
    poisoned = {"ok": True, "balance": 10, "observed_at": time.time(), **bad_fields}
    with store._connect() as db:
        db.execute("INSERT INTO cloud_settings(key,value,updated_at) VALUES (?,?,?)",
                   ("provider_usage_snapshot", json.dumps(poisoned), time.time()))
        db.commit()
    assert store.get_provider_usage_snapshot()["ok"] is False
    job = store.enqueue("blocked.mp4", cu_estimated=0.2, cu_buffer=0.0)
    assert store.claim_job(job.job_id, "worker") is None
    recovered = store.record_provider_usage_snapshot({"ok": True, "balance": 1.0})
    assert recovered["ok"] is True
    assert recovered["is_stale"] is False
    assert store.claim_job(job.job_id, "worker") is not None
