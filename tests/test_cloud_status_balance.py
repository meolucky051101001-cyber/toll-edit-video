from backend.cloud import cloud_contract


def setup_function():
    cloud_contract.reset_provider_status_cache()


def teardown_function():
    cloud_contract.reset_provider_status_cache()


def test_provider_status_marks_real_balance_fresh():
    result = cloud_contract.fetch_provider_status_nonblocking(
        usage_fn=lambda: {"ok": True, "balance": 12.5, "usage_rate": 0.0},
        include_sessions=False,
        wait_timeout_seconds=1.0,
    )

    assert result["usage"]["balance"] == 12.5
    assert result["provider_cache"]["balance_available"] is True
    assert result["provider_cache"]["balance_stale"] is False
    assert result["provider_cache"]["balance_age_seconds"] is not None
    assert result["provider_cache"]["balance_updated_utc"]


def test_provider_status_keeps_old_balance_marked_stale_after_failed_refresh():
    cloud_contract.fetch_provider_status_nonblocking(
        usage_fn=lambda: {"ok": True, "balance": 7.25},
        include_sessions=False,
        wait_timeout_seconds=1.0,
    )
    with cloud_contract._PROVIDER_CACHE_LOCK:
        cloud_contract._PROVIDER_CACHE["usage_fetched_at"] -= 60

    result = cloud_contract.fetch_provider_status_nonblocking(
        usage_fn=lambda: {"ok": False, "error": "offline test"},
        include_sessions=False,
        wait_timeout_seconds=1.0,
    )

    assert result["usage"]["balance"] == 7.25
    assert result["provider_cache"]["balance_available"] is True
    assert result["provider_cache"]["balance_stale"] is True
    assert result["provider_cache"]["balance_age_seconds"] >= 60


def test_provider_status_does_not_report_fallback_zero_as_a_balance():
    result = cloud_contract.fetch_provider_status_nonblocking(
        usage_fn=lambda: {"ok": False, "error": "offline test"},
        include_sessions=False,
        wait_timeout_seconds=1.0,
    )

    assert result["usage"]["balance"] == 0.0
    assert result["provider_cache"]["balance_available"] is False
    assert result["provider_cache"]["balance_age_seconds"] is None
