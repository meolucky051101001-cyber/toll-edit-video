# -*- coding: utf-8 -*-
"""Kiểm thử ops_watchdog: probe sức khỏe, cảnh báo theo chuyển trạng thái và guardrail tự phục hồi."""
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import ops_watchdog


class WatchdogTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ws = Path(self.temporary.name)
        (self.ws / "control").mkdir(parents=True)
        (self.ws / "bot_system").mkdir(parents=True)
        self.now = 1_800_000_000.0
        self.notify = MagicMock()
        self.run_task = MagicMock(return_value=(True, "SUCCESS"))
        self.components = {
            "dashboard": {"severity": "ok", "status": "ok", "active": False, "detail": "đang phản hồi"},
            "control": {"severity": "ok", "status": "ok", "detail": "2 tool"},
            "telegram": {"severity": "ok", "status": "ok", "pids": [111], "detail": "polling bình thường"},
            "scriptbot": {"severity": "ok", "status": "ok", "pids": [222], "detail": "đang chạy"},
            "downloader": {"severity": "ok", "status": "ok", "pids": [333], "detail": "đang chạy"},
            "disk_c": {"severity": "ok", "status": "ok", "detail": "còn 100 GB"},
            "disk_d": {"severity": "ok", "status": "ok", "detail": "còn 100 GB"},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_watchdog(self, now=None, heal=False, **overrides):
        comps = {name: dict(comp) for name, comp in self.components.items()}
        comps.update({name: dict(comp) for name, comp in overrides.items()})
        order = ["scriptbot", "downloader"]
        with ExitStack() as stack:
            stack.enter_context(patch.object(ops_watchdog, "_dashboard_component", return_value=dict(comps["dashboard"])))
            stack.enter_context(patch.object(ops_watchdog, "_control_component", return_value=dict(comps["control"])))
            stack.enter_context(patch.object(ops_watchdog, "_telegram_component", return_value=dict(comps["telegram"])))
            stack.enter_context(
                patch.object(
                    ops_watchdog,
                    "_process_component",
                    side_effect=[dict(comps[name]) for name in order],
                )
            )
            stack.enter_context(
                patch.object(
                    ops_watchdog,
                    "_disk_components",
                    return_value={k: dict(v) for k, v in comps.items() if k.startswith("disk_")},
                )
            )
            stack.enter_context(patch.object(ops_watchdog, "_notify", new=self.notify))
            stack.enter_context(patch.object(ops_watchdog, "_run_task", new=self.run_task))
            return ops_watchdog.run_check(
                self.ws, now=self.now if now is None else now, heal=heal
            )

    def write_state(self, payload):
        path = ops_watchdog._state_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read_state(self):
        return json.loads(ops_watchdog._state_path(self.ws).read_text(encoding="utf-8"))

    def notified_levels(self):
        return [call.args[0] for call in self.notify.call_args_list]


class TestHealthState(WatchdogTestBase):
    def test_healthy_run_writes_state_and_no_alert(self):
        state = self.run_watchdog()
        self.assertEqual(state["overall"], "ok")
        self.assertEqual(self.notify.call_count, 0)
        on_disk = self.read_state()
        self.assertEqual(on_disk["overall"], "ok")
        self.assertIn("dashboard", on_disk["components"])
        self.assertEqual(on_disk["updated_at"], self.now)

    def test_scriptbot_down_is_warning(self):
        state = self.run_watchdog(
            scriptbot={"severity": "warning", "status": "down", "pids": [], "detail": "không có tiến trình"}
        )
        self.assertEqual(state["overall"], "warning")
        self.assertEqual(self.notified_levels(), ["warning"])

    def test_corrupt_state_file_is_tolerated(self):
        ops_watchdog._state_path(self.ws).write_text("not json", encoding="utf-8")
        state = self.run_watchdog()
        self.assertEqual(state["overall"], "ok")


class TestAlerts(WatchdogTestBase):
    def test_dashboard_down_alerts_once_then_repeats_after_window(self):
        down = {"severity": "critical", "status": "down", "detail": "không phản hồi"}
        self.run_watchdog(dashboard=down)
        self.assertEqual(len(self.notify.call_args_list), 1)
        self.assertEqual(self.notify.call_args.args[0], "critical")
        self.assertIn("Dashboard 8088", self.notify.call_args.args[1])

        self.run_watchdog(now=self.now + 10, dashboard=down)
        self.assertEqual(len(self.notify.call_args_list), 1)

        self.run_watchdog(now=self.now + ops_watchdog.ALERT_REPEAT_SECONDS + 11, dashboard=down)
        self.assertEqual(len(self.notify.call_args_list), 2)

    def test_recovery_emits_success_notification(self):
        down = {"severity": "critical", "status": "down", "detail": "không phản hồi"}
        self.run_watchdog(dashboard=down)
        self.run_watchdog(now=self.now + 5)
        levels = self.notified_levels()
        self.assertEqual(levels, ["critical", "success"])
        self.assertIn("đã phục hồi", self.notify.call_args.args[1])

    def test_ok_after_ok_does_not_notify(self):
        self.run_watchdog()
        self.run_watchdog(now=self.now + 5)
        self.assertEqual(self.notify.call_count, 0)


class TestHealGuardrails(WatchdogTestBase):
    down_dashboard = {"severity": "critical", "status": "down", "detail": "không phản hồi"}

    def test_disabled_heal_only_recommends(self):
        state = self.run_watchdog(dashboard=self.down_dashboard)
        self.assertEqual(self.run_task.call_count, 0)
        self.assertIn("--heal", state["components"]["dashboard"]["recommendation"])

    def test_heal_runs_scheduled_task_when_batch_idle(self):
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.run_task.assert_called_once_with("AutoDubV1-dashboard")
        self.assertIn("đã chạy AutoDubV1-dashboard", state["components"]["dashboard"]["heal"])
        self.assertIn(self.now, self.read_state()["heal"]["dashboard"])

    def test_heal_blocked_while_batch_active(self):
        (self.ws / "bot_system" / "job_status.json").write_text(
            json.dumps({"active": True}), encoding="utf-8"
        )
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertEqual(self.run_task.call_count, 0)
        self.assertIn("batch đang chạy", state["components"]["dashboard"]["heal"])

    def test_heal_blocked_by_batch_lock(self):
        (self.ws / "control" / "batch.lock").write_text("", encoding="utf-8")
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertEqual(self.run_task.call_count, 0)
        self.assertIn("batch.lock", state["components"]["dashboard"]["heal"])

    def test_v1_pause_disables_dashboard_probe_and_heal(self):
        (self.ws / "control" / "v1.pause").write_text("", encoding="utf-8")
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertTrue(state["paused"])
        self.assertEqual(state["components"]["dashboard"]["status"], "disabled")
        self.assertEqual(state["overall"], "ok")
        self.assertEqual(self.run_task.call_count, 0)

    def test_ops_pause_flag_skips_heal_entirely(self):
        (self.ws / "control" / "ops.pause").write_text("", encoding="utf-8")
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertTrue(state["heal_paused"])
        self.assertEqual(self.run_task.call_count, 0)

    def test_v1_pause_blocks_heal_for_all_components(self):
        (self.ws / "control" / "v1.pause").write_text("", encoding="utf-8")
        state = {
            "components": {
                "scriptbot": {"severity": "warning", "status": "down"},
                "downloader": {"severity": "warning", "status": "down"},
            },
            "heal": {},
        }
        with patch.object(ops_watchdog, "_run_task") as run_task:
            ops_watchdog._apply_heal(state, self.ws, self.now, True)
        self.assertTrue(state["heal_paused"])
        run_task.assert_not_called()

    def test_heal_respects_cooldown(self):
        self.write_state({"heal": {"dashboard": [self.now - 60]}})
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertEqual(self.run_task.call_count, 0)
        self.assertIn("cooldown", state["components"]["dashboard"]["heal"])

    def test_heal_respects_hourly_rate_limit(self):
        self.write_state({"heal": {"dashboard": [self.now - 30, self.now - 600, self.now - 1200]}})
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.assertEqual(self.run_task.call_count, 0)
        self.assertIn("3 lần", state["components"]["dashboard"]["heal"])

    def test_corrupt_heal_history_is_ignored(self):
        self.write_state({"heal": {"dashboard": "not-a-list"}})
        state = self.run_watchdog(heal=True, dashboard=self.down_dashboard)
        self.run_task.assert_called_once_with("AutoDubV1-dashboard")
        self.assertIn("đã chạy", state["components"]["dashboard"]["heal"])

    def test_heal_failure_is_reported(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(ops_watchdog, "_dashboard_component", return_value=dict(self.down_dashboard)))
            stack.enter_context(patch.object(ops_watchdog, "_control_component", return_value=dict(self.components["control"])))
            stack.enter_context(patch.object(ops_watchdog, "_telegram_component", return_value=dict(self.components["telegram"])))
            stack.enter_context(patch.object(ops_watchdog, "_process_component", side_effect=[dict(self.components["scriptbot"]), dict(self.components["downloader"])]))
            stack.enter_context(patch.object(ops_watchdog, "_disk_components", return_value={}))
            stack.enter_context(patch.object(ops_watchdog, "_notify"))
            stack.enter_context(patch.object(ops_watchdog, "_run_task", return_value=(False, "Access denied")))
            state = ops_watchdog.run_check(self.ws, now=self.now, heal=True)
        self.assertIn("thất bại", state["components"]["dashboard"]["heal"])
        self.assertIn("Access denied", state["components"]["dashboard"]["heal"])


class TestTelegramProbe(WatchdogTestBase):
    def _write_heartbeat(self, payload):
        (self.ws / "control" / "v1.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_process_is_critical(self):
        with patch.object(ops_watchdog, "_pids_for", return_value=[]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "critical")
        self.assertEqual(comp["status"], "down")

    def test_fresh_heartbeat_with_polling_is_ok(self):
        self._write_heartbeat({"pid": 111, "at": self.now - 2, "polling": True})
        with patch.object(ops_watchdog, "_pids_for", return_value=[111]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "ok")
        self.assertEqual(comp["status"], "ok")

    def test_stale_heartbeat_is_warning(self):
        self._write_heartbeat({"pid": 111, "at": self.now - 120, "polling": True})
        with patch.object(ops_watchdog, "_pids_for", return_value=[111]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "warning")
        self.assertIn("heartbeat cũ", comp["detail"])

    def test_pid_mismatch_is_warning(self):
        self._write_heartbeat({"pid": 999, "at": self.now - 2, "polling": True})
        with patch.object(ops_watchdog, "_pids_for", return_value=[111]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "warning")
        self.assertIn("không khớp", comp["detail"])

    def test_paused_tool_is_disabled_even_without_process(self):
        with patch.object(ops_watchdog, "_pids_for", return_value=[]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, True)
        self.assertEqual(comp["severity"], "ok")
        self.assertEqual(comp["status"], "disabled")

    def test_disabled_flag_short_circuits_telegram_probe(self):
        (self.ws / "control" / "telegram.disabled").write_text("", encoding="utf-8")
        with patch.object(ops_watchdog, "_pids_for") as pids:
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "ok")
        self.assertEqual(comp["status"], "disabled")
        pids.assert_not_called()

    def test_newest_telemetry_wins(self):
        self._write_heartbeat({"pid": 111, "at": self.now - 500})
        newer = self.ws / "bot_system" / "control"
        newer.mkdir(parents=True)
        (newer / "v1.json").write_text(
            json.dumps({"pid": 222, "at": self.now - 2, "polling": True}), encoding="utf-8"
        )
        with patch.object(ops_watchdog, "_pids_for", return_value=[222]):
            comp = ops_watchdog._telegram_component(self.ws, self.now, False)
        self.assertEqual(comp["severity"], "ok")


class TestProcessAndHttpProbes(WatchdogTestBase):
    def test_disabled_flag_short_circuits_process_probe(self):
        (self.ws / "control" / "downloader.disabled").write_text("", encoding="utf-8")
        with patch.object(ops_watchdog, "_pids_for") as pids:
            comp = ops_watchdog._process_component(Path("x.py"), self.ws, "downloader.disabled")
        self.assertEqual(comp["status"], "disabled")
        self.assertEqual(comp["severity"], "ok")
        pids.assert_not_called()

    def test_missing_process_is_warning(self):
        with patch.object(ops_watchdog, "_pids_for", return_value=[]):
            comp = ops_watchdog._process_component(Path("x.py"), self.ws, "scriptbot.disabled")
        self.assertEqual(comp["severity"], "warning")
        self.assertEqual(comp["status"], "down")

    def test_paused_missing_process_is_disabled(self):
        with patch.object(ops_watchdog, "_pids_for", return_value=[]):
            comp = ops_watchdog._process_component(Path("x.py"), self.ws, "scriptbot.disabled", True)
        self.assertEqual(comp["severity"], "ok")
        self.assertEqual(comp["status"], "disabled")

    def test_running_process_is_ok(self):
        with patch.object(ops_watchdog, "_pids_for", return_value=[42]):
            comp = ops_watchdog._process_component(Path("x.py"), self.ws, "scriptbot.disabled")
        self.assertEqual(comp["severity"], "ok")
        self.assertEqual(comp["pids"], [42])

    def test_dashboard_http_failure_is_critical(self):
        with patch.object(ops_watchdog, "_http_json", side_effect=OSError("refused")):
            comp = ops_watchdog._dashboard_component(self.now)
        self.assertEqual(comp["severity"], "critical")
        self.assertEqual(comp["status"], "down")

    def test_control_http_failure_is_critical(self):
        with patch.object(ops_watchdog, "_http_json", side_effect=OSError("refused")):
            comp = ops_watchdog._control_component(self.now)
        self.assertEqual(comp["severity"], "critical")

    def test_dashboard_http_reports_active_batch(self):
        with patch.object(ops_watchdog, "_http_json", return_value={"active": True}):
            comp = ops_watchdog._dashboard_component(self.now)
        self.assertEqual(comp["severity"], "ok")
        self.assertTrue(comp["active"])


if __name__ == "__main__":
    unittest.main()
