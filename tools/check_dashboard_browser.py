"""Run isolated dashboard browser acceptance with generated fixture media."""
import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright
import psutil

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--browser-executable', type=Path)
parser.add_argument('--output', type=Path, default=root / 'test_evidence' / 'dashboard')
args = parser.parse_args()
evidence = args.output
evidence.mkdir(parents=True, exist_ok=True)
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
url = f"http://127.0.0.1:{port}"
browser_exe = args.browser_executable
checks = []
with tempfile.TemporaryDirectory(prefix="dashboard_browser_") as tmp:
    runtime = Path(tmp)
    inputs = runtime / "input"
    inputs.mkdir()
    sample = inputs / "A thử.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=160x90:r=5",
                    "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.4", "-c:v", "mpeg4",
                    "-c:a", "aac", "-y", str(sample)], check=True, timeout=30)
    shutil.copyfile(sample, inputs / "B.mp4")
    logfile = (runtime / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(root / "tools" / "dashboard_preview.py"),
                             "--port", str(port), "--runtime", str(runtime)], cwd=root, stdout=logfile, stderr=logfile,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(80):
            if proc.poll() is not None:
                raise RuntimeError((runtime / "server.log").read_text(encoding="utf-8", errors="replace"))
            try:
                with urlopen(url + "/api/status", timeout=0.5) as res:
                    if res.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(browser_exe) if browser_exe else None)
            context = browser.new_context(viewport={"width": 1440, "height": 1080})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(url, wait_until="domcontentloaded")
            a = page.locator('input[data-filename="A thử.mp4"]')
            b = page.locator('input[data-filename="B.mp4"]')
            expect(a).to_be_visible(timeout=20000)
            expect(b).to_be_visible()
            expect(page.locator("#btnStartPhoi")).to_be_enabled(timeout=10000)
            checks.append("dashboard loaded status and both fixture videos")

            page.route("**/api/logs", lambda route: route.fulfill(status=503, json={"detail": "fixture"}))
            page.evaluate("fetchLogs()")
            expect(page.locator("#logSummaryBadge")).to_contain_text("Không tải được nhật ký", timeout=12000)
            expect(a).to_be_visible()
            expect(page.locator("#btnStartPhoi")).to_be_enabled()
            page.unroute("**/api/logs")
            checks.append("a failed logs response did not block status or input list")

            a.check()
            page.locator("#btnStartPhoi").click()
            expect(page.locator("#preRunSummaryModal")).to_be_visible(timeout=15000)
            writes = []
            def drop_response_after_commit(route):
                result = route.fetch()
                assert result.status == 201, result.text()
                writes.append(result.json()["batch_id"])
                route.abort()
            page.route("**/api/jobs/batch", drop_response_after_commit)
            page.locator("#btnScheduleBulk").click()
            expect(page.locator("#scheduleChip")).to_contain_text("Đã xác nhận lịch", timeout=20000)
            status = page.request.get(url + "/api/schedule").json()
            plan = page.request.get(url + "/api/batches/" + status["batch_id"]).json()
            assert writes == [status["batch_id"]], writes
            assert [item["filename"] for item in plan["items"]] == [sample.name], plan
            assert plan["state"] == "scheduled", plan
            checks.append("browser reconciled lost batch response and scheduled exactly the selected video")

            page.locator("#scheduleChip").get_by_role("button", name="Hủy").click()
            expect(page.locator("#scheduleChip")).to_contain_text("Đã hủy lịch", timeout=10000)
            assert page.request.get(url + "/api/schedule").json()["state"] == "canceled"
            assert not list((runtime / "output").glob("*.mp4"))
            assert sample.is_file() and (inputs / "B.mp4").is_file()
            checks.append("cancel retained both source files and did not start rendering")

            row = page.locator("#phoiTableContainer tr").filter(has_text=sample.name)
            row.get_by_role("button", name="Khôi phục").click()
            expect(page.locator("#recoverySourceInfo")).to_contain_text(sample.name, timeout=10000)
            expect(page.locator("#btnActionRecover")).to_be_disabled()
            expect(page.locator("#btnActionRestart")).to_be_enabled()
            checks.append("recovery preview resolved the exact Unicode filename and showed workspace capability")
            assert not errors, errors
            browser.close()
    finally:
        try:
            owner = psutil.Process(proc.pid)
            owned = owner.children(recursive=True) + [owner]
        except psutil.NoSuchProcess:
            owned = []
        for child in reversed(owned):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(owned, timeout=10)
        for child in alive:
            child.kill()
        psutil.wait_procs(alive, timeout=5)
        proc.wait(timeout=15)
        logfile.close()

result = {"tested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "passed": checks, "page_errors": errors, "isolated": True}
(evidence / "browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))

