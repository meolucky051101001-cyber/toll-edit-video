from __future__ import annotations

import sys
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


def test_list_warm_sessions_includes_idle_seconds_and_cu_estimate(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "warm-list.db", rollout_controlled=False)
    now = time.time()
    store.park_warm_session("warm-session", "T4", ttl_seconds=3600, now=now - 1800)

    warm = store.list_warm_sessions(now=now)[0]

    assert warm["idle_seconds"] == pytest.approx(1800)
    assert warm["cu_rate_per_hour"] == pytest.approx(1.15)
    assert warm["idle_cu_estimate"] == pytest.approx(0.575)
    assert warm["expires_in_seconds"] == pytest.approx(1800)
