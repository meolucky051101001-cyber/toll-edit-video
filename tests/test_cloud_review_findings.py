"""Regressions for the warm-GPU budget, bot shutdown and wide-caption findings."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import numpy as np
import pytest

from backend.cloud.cloud_contract import GPU_CU_RATES
from backend.cloud.cloud_job_store import CloudJobStore, _day_bounds_epoch
from backend.cloud.cloud_telegram_bot import CloudTelegramBotService
from backend.cloud.gpu_scripts import caption_geometry as geometry

NOON = datetime(2026, 11, 21, 12, 0, tzinfo=timezone(timedelta(hours=7))).timestamp()


def _cu(seconds):
    return seconds * GPU_CU_RATES["T4"] / 3600


def test_parked_warm_gpu_counts_against_the_daily_budget(tmp_path):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    assert store.get_cu_budget_info(now=NOON)["committed_today"] == 0

    store.park_warm_session("warm-1", "T4", ttl_seconds=300, now=NOON)
    parked = store.get_cu_budget_info(now=NOON + 100)
    assert parked["spent_today"] == pytest.approx(_cu(100), abs=.002)
    assert parked["reserved_today"] == pytest.approx(_cu(200), abs=.002)

    assert store.pop_warm_session("T4", now=NOON + 100)["session_name"] == "warm-1"
    reused = store.get_cu_budget_info(now=NOON + 5000)
    assert reused["spent_today"] == pytest.approx(_cu(100), abs=.002)
    assert reused["reserved_today"] == 0


def test_standby_is_held_until_the_parked_gpu_is_taken_over(tmp_path):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(1.0)  # the job (0.58 CU) fits alone, not beside the 0.96 CU standby
    store.park_warm_session("warm-1", "T4", ttl_seconds=3000, now=NOON)

    # A job on another GPU leaves the standby running, so it must fit beside it.
    held = store.check_admission_allowed(cu_estimated=.5, gpu_type="L4", now=NOON + 100)
    assert held["code"] == "BUDGET_EXCEEDED"

    store.pop_warm_session("T4", now=NOON + 100)
    released = store.check_admission_allowed(cu_estimated=.5, gpu_type="L4", now=NOON + 100)
    assert released["allowed"] is True


def test_standby_after_midnight_is_not_held_against_today(tmp_path):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    midnight = _day_bounds_epoch(NOON, store.get_cu_budget_info(now=NOON)["budget_timezone"])[1]
    store.park_warm_session("warm-1", "T4", ttl_seconds=300, now=midnight - 120)

    today = store.get_cu_budget_info(now=midnight - 100)
    assert today["spent_today"] == pytest.approx(_cu(20), abs=.002)
    assert today["reserved_today"] == pytest.approx(_cu(100), abs=.002)


def test_warm_gpu_left_past_expiry_is_billed_only_until_its_stop_window(tmp_path):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    store.park_warm_session("warm-1", "T4", ttl_seconds=300, now=NOON)
    stale = store.get_cu_budget_info(now=NOON + 10000)
    assert stale["spent_today"] == pytest.approx(_cu(360), abs=.002)
    assert stale["reserved_today"] == 0


def test_cloud_bot_stop_releases_parked_warm_gpu(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(ws))
    store = CloudJobStore(db_path=ws / "cloud_jobs.db", rollout_controlled=False)
    svc = CloudTelegramBotService(workspace_dir=ws, store=store, adapter_factory=Mock)
    release = Mock(return_value=1)
    svc._cloud_worker.orchestrator.release_warm_sessions = release

    assert svc.stop(timeout=.5) is True
    release.assert_called_once_with()


def test_caption_wider_than_the_compact_lane_is_measured_in_full(monkeypatch):
    widths = []

    def detect(reader, crop, *, top, bottom, scale):
        widths.append(crop.shape[1])
        # Text spans 10%–90% of the frame; the 25%–75% lane only sees its middle.
        return [[0., .9, 1., .92]] if crop.shape[1] == 50 else [[.1, .9, .9, .92]]

    monkeypatch.setattr(geometry, "_detect", detect)
    measured = geometry.observe_caption_boxes(
        object(), np.zeros((100, 100, 3)), band_top=.87, band_bottom=.98, x_bounds=(.25, .75),
    )
    assert widths == [50, 50, 100, 100]
    assert measured[0] == pytest.approx([.1, .9, .9, .92])


def test_wider_pass_that_sees_nothing_keeps_the_measured_caption(monkeypatch):
    monkeypatch.setattr(
        geometry, "_detect",
        lambda reader, crop, *, top, bottom, scale: [[0., .9, 1., .92]] if crop.shape[1] == 50 else [],
    )
    measured = geometry.observe_caption_boxes(
        object(), np.zeros((100, 100, 3)), band_top=.87, band_bottom=.98, x_bounds=(.25, .75),
    )
    assert measured[0] == pytest.approx([.25, .9, .75, .92])
