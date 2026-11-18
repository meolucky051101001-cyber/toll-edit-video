"""Regression tests for the production Cloud Dashboard hidden launcher."""
import asyncio
import json
import threading
from unittest.mock import Mock

from fastapi import FastAPI

import run_cloud_node as node


def test_desktop_lifespan_does_not_start_worker_or_probe_provider(monkeypatch):
    monkeypatch.setattr(node, "resolve_local_control_token", lambda: "offline-fixture-control-token")
    monkeypatch.setattr(node, "get_cloud_worker", Mock(side_effect=AssertionError("worker started at desktop open")))
    monkeypatch.setattr(node, "peek_cloud_worker", lambda: None)
    monkeypatch.setattr(node, "get_usage", Mock(side_effect=AssertionError("provider probed")), raising=False)
    monkeypatch.setattr(node, "get_sessions", Mock(side_effect=AssertionError("provider probed")), raising=False)

    async def exercise():
        app = FastAPI()
        app.state.autostart_worker = False
        app.state.owns_process_worker = True
        async with node.lifespan(app):
            assert app.state.node_ready is True
        assert app.state.node_ready is False
        assert app.state.node_shutdown_complete is True

    asyncio.run(exercise())


def test_desktop_shutdown_stops_only_an_existing_worker(monkeypatch):
    worker = Mock()
    worker.stop.return_value = {"stopped": True}
    monkeypatch.setattr(node, "resolve_local_control_token", lambda: "offline-fixture-control-token")
    monkeypatch.setattr(node, "get_cloud_worker", Mock(side_effect=AssertionError("worker created at desktop open")))
    monkeypatch.setattr(node, "peek_cloud_worker", lambda: worker)

    async def exercise():
        app = FastAPI()
        app.state.autostart_worker = False
        app.state.owns_process_worker = True
        async with node.lifespan(app):
            pass
        assert app.state.node_shutdown_complete is True

    asyncio.run(exercise())
    worker.stop.assert_called_once_with(timeout=node.SHUTDOWN_TIMEOUT_SECONDS)


def test_browser_and_startup_report_wait_for_authenticated_readiness(monkeypatch, tmp_path):
    report = tmp_path / "startup.json"
    monkeypatch.setattr(node, "_STARTUP_REPORT", report)
    opened = Mock()
    monkeypatch.setattr(node, "_open_browser", opened)
    monkeypatch.setattr(node, "_wait_ready", lambda *args, **kwargs: False)
    stopped = threading.Event()

    node._open_browser_when_ready("127.0.0.1", 8189, "http://127.0.0.1:8189/cloud", stopped)
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "failed"
    opened.assert_not_called()

    monkeypatch.setattr(node, "_wait_ready", lambda *args, **kwargs: True)
    node._open_browser_when_ready("127.0.0.1", 8189, "http://127.0.0.1:8189/cloud", stopped,
                                  open_browser=False)
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "status": "ready", "code": 0, "pid": node.os.getpid(), "ppid": node.os.getppid(),
    }
    opened.assert_not_called()


def test_runner_startup_report_contract_matches_hidden_launcher_over_venv_stub(monkeypatch, tmp_path):
    import launch_cloud_dashboard as launcher

    report = tmp_path / "venv-child.startup.json"
    monkeypatch.setattr(node, "_STARTUP_REPORT", report)
    monkeypatch.setattr(node.os, "getpid", lambda: 24672)
    monkeypatch.setattr(node.os, "getppid", lambda: 23740)

    node._report_startup("ready", 0)
    assert launcher.read_startup_report(report, 23740) == "ready"
    assert launcher.read_startup_report(report, 24672) == "ready"
    assert launcher.read_startup_report(report, 99999) is None


def test_stale_same_checkout_node_retires_cleanly_while_foreign_port_never_receives_token(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(node, "resolve_local_control_token", lambda: "secret-control-token-xyz")
    monkeypatch.setattr(node.secrets, "token_hex", lambda n: "c" * (n * 2))

    # 1. Foreign service on port 8089: invalid HMAC -> returns 'unverified' and never sends POST with token
    calls = []

    class FakeConnForeign:
        def __init__(self, host, port, timeout=1.0):
            pass

        def request(self, method, path, body=None, headers=None):
            calls.append((method, path, headers))

        def getresponse(self):
            bad = {
                "app": node.NODE_APP_ID,
                "ready": True,
                "workspace_id": node._checkout_workspace_id(),
                "identity": "old-id",
                "instance": "inst1",
                "proof": "forged-hmac",
            }
            return SimpleNamespace(status=200, read=lambda _n: json.dumps(bad).encode("utf-8"))

        def close(self):
            pass

    monkeypatch.setattr(node.http.client, "HTTPConnection", FakeConnForeign)
    assert node._retire_stale_same_checkout_node("127.0.0.1", 8089) == "unverified"
    assert len(calls) == 1 and calls[0][0] == "GET"

    # 2. Authenticated older revision of same checkout/workspace -> sends POST /api/cloud/node/retire and returns 'retired'
    calls.clear()
    valid_proof = node._readiness_proof("c" * 32, "older-revision-identity", "inst2", True)

    class FakeConnSameCheckout:
        def __init__(self, host, port, timeout=1.0):
            self._last_method = "GET"

        def request(self, method, path, body=None, headers=None):
            self._last_method = method
            calls.append((method, path, headers))

        def getresponse(self):
            if self._last_method == "GET":
                ok_ready = {
                    "app": node.NODE_APP_ID,
                    "ready": True,
                    "workspace_id": node._checkout_workspace_id(),
                    "identity": "older-revision-identity",
                    "instance": "inst2",
                    "revision": "old-rev",
                    "proof": valid_proof,
                }
                return SimpleNamespace(status=200, read=lambda _n: json.dumps(ok_ready).encode("utf-8"))
            return SimpleNamespace(status=200, read=lambda _n: b'{"retired":true}')

        def close(self):
            pass

    monkeypatch.setattr(node.http.client, "HTTPConnection", FakeConnSameCheckout)
    assert node._retire_stale_same_checkout_node("127.0.0.1", 8089) == "retired"
    assert [c[0] for c in calls] == ["GET", "POST"]
    assert calls[1][1] == "/api/cloud/node/retire"
