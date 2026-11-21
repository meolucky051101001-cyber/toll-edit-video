"""Isolated regression checks for the SubExtract → Tool V1 handoff receiver.

Never loads models, never renders, never touches the live workspace: the Studio
module is replaced by a stub whose WORKSPACE points at a TemporaryDirectory, and
the path helpers are the real ones lifted from workflow_api.py / workflow_helpers.py source.
"""
import ast
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import handoff_api as hf  # noqa: E402
import v1_ocr_geometry as geometry  # noqa: E402

TMP = tempfile.TemporaryDirectory(prefix="v1-handoff-tests-")
BASE = Path(TMP.name).resolve()
WORKSPACE = BASE / "workspace"
OUTPUT_DIR = BASE / "output"
SX_JOBS = BASE / "subextract-jobs"
for _directory in (WORKSPACE, OUTPUT_DIR, SX_JOBS):
    _directory.mkdir(parents=True, exist_ok=True)


def _studio_stub():
    """safe_stem/artifact_stem/resolve_job_dir/format_srt_timestamp thật, gốc workspace tạm."""
    nodes = []
    for source_name in ("workflow_helpers.py", "workflow_api.py"):
        source = (ROOT / "backend" / source_name).read_text(encoding="utf-8-sig")
        nodes.extend(ast.parse(source).body)
    namespace = {"Path": Path, "Any": __import__("typing").Any,
                 "Optional": __import__("typing").Optional,
                 "HTTPException": HTTPException, "WORKSPACE": WORKSPACE,
                 "re": __import__("re")}
    body = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            names = [getattr(target, "id", "") for target in node.targets]
            if any(name.startswith("_STEM_") for name in names):
                body.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in {
            "safe_stem", "artifact_stem", "resolve_job_dir", "format_srt_timestamp",
        }:
            node.decorator_list = []
            body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]), "isolated-studio", "exec"), namespace)
    stub = types.SimpleNamespace(
        INPUT_DIR=BASE / "input",
        OUTPUT_DIR=OUTPUT_DIR,
        safe_stem=namespace["safe_stem"],
        artifact_stem=namespace["artifact_stem"],
        resolve_job_dir=namespace["resolve_job_dir"],
        format_srt_timestamp=namespace["format_srt_timestamp"],
        _wait_pipeline_free=lambda job_id: True,
    )
    stub.INPUT_DIR.mkdir(parents=True, exist_ok=True)

    def use_workspace(path: Path) -> Path:
        """Mỗi test một workspace để không kế thừa tệp (và hardlink) của test trước."""
        path.mkdir(parents=True, exist_ok=True)
        namespace["WORKSPACE"] = path
        stub.WORKSPACE = path
        stub.DOWNLOADS_DIR = path / "downloads"
        stub.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        return path

    stub.use_workspace = use_workspace
    use_workspace(WORKSPACE)
    return stub


STUDIO = _studio_stub()


def isolated_workspace(case: str) -> Path:
    return BASE / "ws" / re.sub(r"\W", "_", case)


def allowlisted_env(**extra):
    env = {
        "SUBEXTRACT_JOBS_DIR": str(SX_JOBS),
        "AUTODUB_WORKSPACE": str(WORKSPACE),
        "AUTODUB_OUTPUT_DIR": str(OUTPUT_DIR),
        "V1_HANDOFF_ALLOWED_DIRS": "",
    }
    env.update(extra)
    return patch.dict(os.environ, env, clear=False)


def write_video(directory: Path, name: str = "source.mp4") -> Path:
    path = directory / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 512)
    return path


def wait_render_lock_free(timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if hf._RENDER_LOCK.acquire(blocking=False):
            hf._RENDER_LOCK.release()
            return True
        time.sleep(0.05)
    return False


def cue_payload(**overrides):
    payload = {
        "stem": "seed video",
        "source_video": "",
        "source_job_id": "sx0001",
        "video_width": 720,
        "video_height": 1280,
        "duration": 6.0,
        "target_lang": "vi",
        "cues": [
            {"start": 0.5, "end": 2.0, "text": "第一句", "trans": "câu một",
             "box": [0.0, 0.68, 1.0, 1.0]},
            {"start": 2.2, "end": 3.5, "text": "第二句", "trans": "câu hai",
             "box": {"x_pct": 0.02, "y_pct": 0.05, "max_x_pct": 0.62, "max_y_pct": 0.17}},
            {"start": 3.6, "end": 5.0, "text": "第三句", "trans": ""},
        ],
    }
    payload.update(overrides)
    return payload


class HelperTests(unittest.TestCase):
    def test_clamp_box_accepts_list_and_dict_and_sorts_edges(self):
        self.assertEqual(hf._clamp_box([0.1, 0.7, 0.9, 0.95]), [0.1, 0.7, 0.9, 0.95])
        self.assertEqual(
            hf._clamp_box({"x_pct": 0.9, "max_x_pct": 0.1, "y_pct": 0.95, "max_y_pct": 0.7}),
            [0.1, 0.7, 0.9, 0.95],
        )

    def test_clamp_box_clamps_out_of_range_and_keeps_min_size(self):
        self.assertEqual(hf._clamp_box([-2.0, 5.0, 9.0, -1.0]), [0.0, 0.0, 1.0, 1.0])
        self.assertEqual(hf._clamp_box([0.5, 0.9, 0.5, 0.9]), [0.5, 0.9, 0.505, 0.905])

    def test_clamp_box_rejects_garbage(self):
        for raw in (None, [0.1, 0.2, 0.3], {"x_pct": 0.1}, "not-a-box", 12, [0.1, None, 0.4, 0.6]):
            self.assertIsNone(hf._clamp_box(raw), raw)

    def test_normalize_cues_renumbers_and_coerces_times(self):
        cues = hf._normalize_cues([
            {"start": -3, "end": 1.0, "text": "  a  "},
            {"start": 5.0, "end": 5.0, "text": "", "trans": "  chỉ có bản dịch  "},
            {"start": 9.0, "end": 8.0, "trans": "dịch", "translation": "bỏ"},
            "rác",
            {"start": 20, "end": 21},
        ])
        self.assertEqual([c["index"] for c in cues], [1, 2, 3])
        self.assertEqual(cues[0]["start"], 0.0)
        self.assertEqual(cues[1]["text"], "")
        self.assertEqual(cues[1]["trans"], "chỉ có bản dịch")
        self.assertGreater(cues[2]["end"], cues[2]["start"])
        self.assertEqual(cues[2]["text"], "")

    def test_normalize_cues_rejects_empty_and_oversize(self):
        with self.assertRaises(HTTPException):
            hf._normalize_cues([])
        with self.assertRaises(HTTPException):
            hf._normalize_cues([{"start": 0, "end": 1, "text": ""}])
        with patch.object(hf, "_MAX_CUES", 2):
            with self.assertRaises(HTTPException):
                hf._normalize_cues([{"start": i, "end": i + 1, "text": "x"} for i in range(3)])

    def test_band_box_and_main_y_prefers_request_then_median(self):
        self.assertEqual(hf._band_box(0.84), [0.04, 0.795, 0.96, 0.885])
        cues = hf._normalize_cues(cue_payload()["cues"])
        self.assertAlmostEqual(hf._main_y_pct(cues, None), 0.84, places=3)
        self.assertEqual(hf._main_y_pct(cues, 0.5), 0.5)
        self.assertEqual(hf._main_y_pct([{"box": None}], "abc"), hf._FALLBACK_MAIN_Y)

    def test_srt_text_uses_studio_timestamp_and_skips_missing_field(self):
        with patch.object(hf, "_wf", lambda: STUDIO):
            cues = hf._normalize_cues(cue_payload()["cues"])
            original = hf._srt_text(cues, "text")
            translated = hf._srt_text(cues, "trans")
        self.assertIn("00:00:00,500 --> 00:00:02,000", original)
        self.assertEqual(original.count("-->"), 3)
        self.assertEqual(translated.count("-->"), 2)
        self.assertNotIn("第三句", translated)

    def test_source_video_must_be_absolute_existing_video_inside_allowlist(self):
        inside = write_video(SX_JOBS, "ok.mp4")
        outside = write_video(BASE, "outside.mp4")
        with allowlisted_env(), patch.object(hf, "_wf", lambda: STUDIO):
            self.assertEqual(hf._resolve_source_video(inside), inside.resolve())
            with self.assertRaises(HTTPException) as ctx:
                hf._resolve_source_video(outside)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("được phép", str(ctx.exception.detail))
            for raw, code in (("", 400), ("relative/source.mp4", 400),
                              (str(SX_JOBS / "khongco.mp4"), 404), (str(inside.parent), 404)):
                with self.assertRaises(HTTPException) as ctx:
                    hf._resolve_source_video(raw)
                self.assertEqual(ctx.exception.status_code, code, raw)
        outside.unlink(missing_ok=True)

    def test_link_prefers_existing_then_hardlink_then_copy_then_pointer(self):
        source = write_video(SX_JOBS, "video.mp4")
        target = WORKSPACE / "linked.mp4"
        self.assertEqual(hf._link_into_job_dir(source, target), "hardlink")
        self.assertEqual(target.stat().st_size, source.stat().st_size)
        self.assertEqual(hf._link_into_job_dir(source, target), "existing")
        target.unlink()
        with patch("os.link", side_effect=OSError(17, "cross device")):
            self.assertEqual(hf._link_into_job_dir(source, target), "copy")
        target.unlink()
        with patch("os.link", side_effect=OSError(17, "cross device")), \
                patch("disk_guard.ensure_free_space", side_effect=RuntimeError("hết ô")), \
                patch("shutil.copy2") as copied:
            self.assertEqual(hf._link_into_job_dir(source, target), "pointer")
            copied.assert_not_called()
        target.unlink(missing_ok=True)

    def test_attach_geometry_passes_strict_mask_validation(self):
        import srt
        segment = srt.Subtitle(index=1, start=__import__("datetime").timedelta(seconds=1),
                               end=__import__("datetime").timedelta(seconds=3), content="câu một")
        hf._attach_geometry(segment, [0.02, 0.05, 0.62, 0.17], 0.84)
        self.assertTrue(geometry.valid_box(segment.best_block))
        self.assertEqual(segment.ocr_mask_status, "located")
        self.assertEqual(segment.y_pct, 0.05)
        with patch.dict(os.environ, {"V1_STRICT_OCR_GEOMETRY": "1"}):
            geometry.validate_mask_geometry([segment, segment, segment])
        bad = srt.Subtitle(index=2, start=__import__("datetime").timedelta(seconds=3),
                           end=__import__("datetime").timedelta(seconds=5), content="khác")
        hf._attach_geometry(bad, None, 0.84)
        self.assertTrue(geometry.valid_box(bad.best_block))
        self.assertEqual([bad.best_block["y_pct"], bad.best_block["max_y_pct"]], [0.795, 0.885])

    def test_run_propagates_ffmpeg_failure_and_clears_process(self):
        import shutil as _shutil
        if not _shutil.which("ffmpeg"):
            self.skipTest("không có ffmpeg trên PATH")
        hf._register("procjob0001", "seed")
        with self.assertRaises(RuntimeError):
            hf._run(["ffmpeg", "-v", "error", "-i", str(SX_JOBS / "khong-ton-tai.mp4"), "out.wav"],
                    timeout=30, job_id="procjob0001")
        with hf._JOBS_LOCK:
            self.assertIsNone(hf._HANDOFF_JOBS["procjob0001"]["proc"])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.workspace = STUDIO.use_workspace(isolated_workspace(self.id()))
        self.video = write_video(SX_JOBS, "clip.mp4")
        patcher_allow = allowlisted_env()
        patcher_allow.start()
        self.addCleanup(patcher_allow.stop)
        patcher_wf = patch.object(hf, "_wf", lambda: STUDIO)
        patcher_wf.start()
        self.addCleanup(patcher_wf.stop)

    def test_import_writes_srt_files_and_handoff_metadata(self):
        result = hf._import_job(cue_payload(source_video=str(self.video)))
        job_dir = self.workspace / "seed video"
        self.assertEqual(result["stem"], "seed video")
        self.assertEqual(result["cue_count"], 3)
        self.assertEqual(result["translated_count"], 2)
        self.assertEqual(result["video_link"], "hardlink")
        self.assertEqual(result["main_y_pct"], 0.84)
        meta = json.loads((job_dir / "handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source"], "subextract")
        self.assertEqual(meta["source_job_id"], "sx0001")
        self.assertEqual(meta["counts"], {"cues": 3, "translated": 2, "with_box": 3})
        self.assertEqual(meta["cues"][1]["box"], [0.02, 0.05, 0.62, 0.17])
        self.assertEqual(meta["cues"][2]["box"], [0.04, 0.795, 0.96, 0.885])
        self.assertEqual((job_dir / f"{result['stem']}.mp4").stat().st_nlink >= 2, True)
        self.assertIn("第一句", (job_dir / "original.srt").read_text(encoding="utf-8"))
        self.assertNotIn("第三句", (job_dir / "translated.srt").read_text(encoding="utf-8"))

    def test_import_probes_video_when_metadata_missing(self):
        with patch.object(hf, "_probe_video", return_value=(1080, 1920, 42.5)):
            result = hf._import_job(cue_payload(source_video=str(self.video),
                                                video_width=0, video_height=0, duration=0))
        self.assertEqual((result["width"], result["height"], result["duration"]), (1080, 1920, 42.5))

    def test_import_rejects_traversal_stem_and_unknown_video(self):
        with self.assertRaises(HTTPException) as ctx:
            hf._import_job(cue_payload(source_video=str(self.video), stem="..\\..\\escape"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse((BASE / "escape").exists())
        with self.assertRaises(HTTPException) as ctx:
            hf._import_job(cue_payload(source_video=str(BASE / "nope.mp4")))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_import_copies_video_when_hardlink_impossible(self):
        with patch("os.link", side_effect=OSError(17, "cross device")):
            result = hf._import_job(cue_payload(source_video=str(self.video), stem="seed-copy"))
        self.assertEqual(result["video_link"], "copy")
        self.assertTrue((self.workspace / "seed-copy" / "seed-copy.mp4").is_file())


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(hf.router)
        cls.client = TestClient(app)

    def setUp(self):
        self.assertTrue(wait_render_lock_free())
        self.workspace = STUDIO.use_workspace(isolated_workspace(self.id()))
        with hf._JOBS_LOCK:
            hf._HANDOFF_JOBS.clear()
        self.video = write_video(SX_JOBS, "clip.mp4")
        patcher_allow = allowlisted_env()
        patcher_allow.start()
        self.addCleanup(patcher_allow.stop)
        patcher_wf = patch.object(hf, "_wf", lambda: STUDIO)
        patcher_wf.start()
        self.addCleanup(patcher_wf.stop)
        # Không bao giờ để render thật chạy trong test: chỉ ghi nhận lời gọi.
        calls = []
        self.calls = calls

        def recorder(job_id, stem, payload):
            calls.append({"job_id": job_id, "stem": stem, "payload": payload})
            return {"delivered": str(OUTPUT_DIR / f"Dubbed_{stem}.mp4")}

        patcher_render = patch.object(hf, "_execute_render", side_effect=recorder)
        patcher_render.start()
        self.addCleanup(patcher_render.stop)

    def _await_status(self, job_id, wanted, timeout=5.0):
        deadline = time.time() + timeout
        body = {}
        while time.time() < deadline:
            body = self.client.get(f"/api/handoff/status?job_id={job_id}").json()
            if body["status"] in wanted:
                return body
            time.sleep(0.05)
        return body

    def test_render_requires_import_and_reports_job_id(self):
        missing = self.client.post("/api/handoff/render", json={"stem": "chua-gui"})
        self.assertEqual(missing.status_code, 404)
        imported = self.client.post("/api/handoff/import",
                                    json=cue_payload(source_video=str(self.video)))
        self.assertEqual(imported.status_code, 200)
        started = self.client.post("/api/handoff/render",
                                   json={"stem": imported.json()["stem"], "voice_id": "edge-x"})
        self.assertEqual(started.status_code, 200)
        job_id = started.json()["job_id"]
        body = self._await_status(job_id, {"done", "error", "cancelled"})
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["progress"], 100)
        self.assertNotIn("proc", body)
        self.assertEqual(self.calls[-1]["stem"], "seed video")
        self.assertEqual(self.calls[-1]["payload"]["voice_id"], "edge-x")
        self.assertTrue((self.workspace / "seed video" / "handoff.json").is_file())

    def test_cancel_stops_worker_and_marks_job_cancelled(self):
        imported = self.client.post("/api/handoff/import",
                                    json=cue_payload(source_video=str(self.video),
                                                     stem="seed-cancel")).json()

        def wait_for_cancel(job_id, stem, payload):
            hf._update(job_id, status="running", progress=30, stage="Đang render...")
            while True:
                hf._check_cancel(job_id)
                time.sleep(0.02)

        with patch.object(hf, "_execute_render", side_effect=wait_for_cancel):
            job_id = self.client.post("/api/handoff/render",
                                      json={"stem": imported["stem"]}).json()["job_id"]
            self.assertEqual(self._await_status(job_id, {"running"})["status"], "running")
            canceled = self.client.post("/api/handoff/cancel", json={"job_id": job_id})
            self.assertEqual(canceled.json()["status"], "cancelling")
            with hf._JOBS_LOCK:
                self.assertTrue(hf._HANDOFF_JOBS[job_id]["cancel_requested"])
            body = self._await_status(job_id, {"cancelled", "error"})
        self.assertEqual(body["status"], "cancelled")
        self.assertIn("bản giao theo yêu cầu", body["stage"])
        self.assertTrue(wait_render_lock_free())

    def test_status_and_cancel_reject_unknown_job(self):
        self.assertEqual(self.client.get("/api/handoff/status?job_id=khongco").status_code, 404)
        self.assertEqual(self.client.post("/api/handoff/cancel", json={"job_id": "khongco"}).status_code, 404)

    def test_render_lock_held_returns_409(self):
        self.client.post("/api/handoff/import", json=cue_payload(source_video=str(self.video)))
        hf._RENDER_LOCK.acquire()
        self.addCleanup(hf._RENDER_LOCK.release)
        response = self.client.post("/api/handoff/render", json={"stem": "seed video"})
        self.assertEqual(response.status_code, 409)

    def test_worker_reports_done_and_error_states(self):
        imported = self.client.post("/api/handoff/import",
                                    json=cue_payload(source_video=str(self.video),
                                                     stem="seed-worker")).json()

        def fake_render(job_id, stem, payload):
            hf._update(job_id, status="running", progress=50, stage="Đang render...")
            return {"delivered": str(OUTPUT_DIR / f"Dubbed_{stem}.mp4"), "voiced_count": 2}

        with patch.object(hf, "_execute_render", side_effect=fake_render):
            job_id = self.client.post("/api/handoff/render", json={"stem": imported["stem"]}).json()["job_id"]
            body = self._await_status(job_id, {"done"})
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["progress"], 100)
        self.assertTrue(body["result"]["delivered"].endswith("Dubbed_seed-worker.mp4"))
        self.assertTrue(wait_render_lock_free())

        def boom(job_id, stem, payload):
            raise RuntimeError("FFmpeg lỗi")

        with patch.object(hf, "_execute_render", side_effect=boom):
            job_id = self.client.post("/api/handoff/render", json={"stem": imported["stem"]}).json()["job_id"]
            body = self._await_status(job_id, {"error"})
        self.assertEqual(body["status"], "error")
        self.assertIn("FFmpeg", body["error"])
        self.assertTrue(wait_render_lock_free())

    def test_info_exposes_temp_workspace_and_defaults(self):
        with patch.object(hf, "_resolve_voice", return_value=("edge", "vi-VN-HoaiMyNeural", "edge/Hoài My")):
            body = self.client.get("/api/handoff/info").json()
        self.assertEqual(Path(body["workspace"]), self.workspace)
        self.assertEqual(Path(body["output_dir"]), OUTPUT_DIR)
        self.assertIn(str(SX_JOBS.resolve()), body["allowed_source_dirs"])
        self.assertEqual(body["defaults"], {"original_volume_db": -14.0, "dubbing_volume_db": 1.0})
        self.assertEqual(body["voice"]["param"], "vi-VN-HoaiMyNeural")

    def test_import_endpoint_rejects_outside_source(self):
        outside = write_video(BASE, "camera.mp4")
        self.addCleanup(outside.unlink, missing_ok=True)
        response = self.client.post("/api/handoff/import", json=cue_payload(source_video=str(outside)))
        self.assertEqual(response.status_code, 400)


class SourceSafetyTests(unittest.TestCase):
    def test_router_is_mounted_without_touching_orchestrator(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8-sig")
        self.assertIn("from handoff_api import router as handoff_router", source)
        self.assertIn("app.include_router(handoff_router)", source)
        orchestrator = (ROOT / "backend" / "v1_orchestrator.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("handoff", orchestrator)


if __name__ == "__main__":
    unittest.main(verbosity=2)
