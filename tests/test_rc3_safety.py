"""Isolated checks: never import main.py or touch production workspace."""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
import package_v1_release as package
import job_tracker
import render_history
from api_pipeline_guard import guarded_media_api
from fastapi import HTTPException

class RC3Checks(unittest.TestCase):
    def test_release_boundary(self):
        for name in ("backend/.env", "backend/.ENV.local", "backend/gdrive_token.json",
                     "v1_code_changes_only.patch", "backend/model_venv/x.py",
                     "../backend/main.py", "backend/client.key"):
            self.assertFalse(package.should_include(name), name)
        for name in ("backend/main.py", "backend/.env.example", "tests/test_rc3_safety.py"):
            self.assertTrue(package.should_include(name), name)

    def test_history_live_lock_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.lock"
            self.assertTrue(render_history._acquire_lock(path))
            os.utime(path, (1, 1))
            child = "import sys;sys.path.insert(0,sys.argv[1]);import render_history as h;from pathlib import Path;sys.exit(1 if h._acquire_lock(Path(sys.argv[2]),timeout=.2) else 0)"
            result = subprocess.run([sys.executable, "-c", child, str(ROOT / "backend"), str(path)], timeout=10)
            self.assertEqual(result.returncode, 0)
            render_history._release_lock(path)
            self.assertTrue(render_history._acquire_lock(path))
            render_history._release_lock(path)

    def test_api_lease_survives_disconnect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(job_tracker, "WORKSPACE", root), patch.object(job_tracker, "BATCH_LOCK_FILE", root / "batch.lock"):
                async def scenario():
                    entered, finish = asyncio.Event(), asyncio.Event()
                    @guarded_media_api
                    async def work():
                        entered.set()
                        await finish.wait()
                        return "ok"
                    first = asyncio.create_task(work())
                    await entered.wait()
                    with self.assertRaises(HTTPException) as conflict:
                        await work()
                    self.assertEqual(conflict.exception.status_code, 409)
                    # Another process using the bot/batch ownership mechanism also conflicts.
                    child = "import sys;sys.path.insert(0,sys.argv[1]);import job_tracker as j;from pathlib import Path;j.WORKSPACE=Path(sys.argv[2]);j.BATCH_LOCK_FILE=j.WORKSPACE/'batch.lock'\ntry:j._claim_batch_lock_unlocked('other')\nexcept j.JobAlreadyRunningError:sys.exit(0)\nsys.exit(1)"
                    result = subprocess.run([sys.executable, "-c", child, str(ROOT / "backend"), str(root)], timeout=10)
                    self.assertEqual(result.returncode, 0)
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    self.assertTrue(job_tracker.BATCH_LOCK_FILE.exists())
                    finish.set()
                    await asyncio.sleep(.05)
                    self.assertFalse(job_tracker.BATCH_LOCK_FILE.exists())
                asyncio.run(scenario())

if __name__ == "__main__":
    unittest.main()
