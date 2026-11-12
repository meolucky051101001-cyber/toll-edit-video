import os

import httpx
import pytest

from backend.core.proxy_env import sanitize_no_proxy_env


def test_httpx_client_crashes_on_ipv6_no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    with pytest.raises(httpx.InvalidURL):
        httpx.Client(trust_env=True)


def test_sanitize_drops_ipv6_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", " localhost, [::1] ,127.0.0.1,::1,fe80::/10 ")
    dropped = sanitize_no_proxy_env()
    assert dropped == ["[::1]", "::1", "fe80::/10"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
    client = httpx.Client(trust_env=True)
    client.close()


def test_sanitize_keeps_valid_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,.localhost,127.0.0.0/8,example.com:8080")
    assert sanitize_no_proxy_env() == []
    assert os.environ["NO_PROXY"] == "localhost,.localhost,127.0.0.0/8,example.com:8080"


def test_sanitize_noop_when_unset(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert sanitize_no_proxy_env() == []
    assert "NO_PROXY" not in os.environ
    assert "no_proxy" not in os.environ


def test_sanitize_is_idempotent(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "[::1],localhost")
    assert sanitize_no_proxy_env() == ["[::1]"]
    assert sanitize_no_proxy_env() == []
    assert os.environ["NO_PROXY"] == "localhost"
