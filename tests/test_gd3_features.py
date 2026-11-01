# -*- coding: utf-8 -*-
"""Kiểm thử GĐ3: F7 học sửa tay Glossary, F10 chống trùng, F12 quản lý workspace, F14 ổ đĩa."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main
import duplicate_guard
import glossary_learning_service as gls
import workspace_usage_service as wus


class TestGlossaryLearning(unittest.TestCase):
    def test_extract_edit_pair_single_short_replace(self):
        pair = gls.extract_edit_pair("cô ấy là tiền bối của tôi", "cô ấy là đàn chị của tôi")
        self.assertEqual(pair, {"before": "tiền bối", "after": "đàn chị"})

    def test_extract_edit_pair_rejects_complex_or_long(self):
        self.assertIsNone(gls.extract_edit_pair("a b c", "a b c"))               # giống hệt
        self.assertIsNone(gls.extract_edit_pair("x y z", "y x q"))               # nhiều chỗ đổi
        self.assertIsNone(gls.extract_edit_pair("một hai ba", "hoàn toàn khác đấy nhé bạn ơi"))  # quá dài
        self.assertIsNone(gls.extract_edit_pair("", "abc"))                      # rỗng

    def test_record_and_suggestions_and_dismiss(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = Path(tmp) / "control"
            with patch.object(gls, "SUGGEST_FILE", ctrl / "glossary_suggestions.json"), \
                 patch.object(gls, "CONTROL_DIR", ctrl):
                for _ in range(3):
                    gls.record_edit("cách nói này hay", "cách diễn đạt này hay", "video_a")
                items = gls.get_suggestions()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["count"], 3)
                # dưới ngưỡng thì không hiện
                gls.record_edit("abc qi", "abc qí", "v")
                self.assertEqual(len(gls.get_suggestions()), 1)
                # bỏ qua -> biến mất
                gls.dismiss_suggestion(items[0]["before"], items[0]["after"])
                self.assertEqual(gls.get_suggestions(), [])

    def test_accept_writes_custom_glossary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ctrl = tmp_path / "control"
            ctrl.mkdir()
            with patch.object(gls, "SUGGEST_FILE", ctrl / "glossary_suggestions.json"), \
                 patch.object(gls, "CONTROL_DIR", ctrl), \
                 patch.object(gls, "WORKSPACE", tmp_path):
                for _ in range(3):
                    gls.record_edit("gói đồ nhỏ xinh", "gói quà nhỏ xinh", "video_b")
                items = gls.get_suggestions()
                self.assertTrue(items)
                res = gls.accept_suggestion(items[0]["before"], items[0]["after"])
                self.assertTrue(res["added"])
                from ai.translation import load_custom_glossary
                entries = load_custom_glossary(str(tmp_path))
                # SequenceMatcher trích cặp tối thiểu: "gói đồ nhỏ" -> "gói quà nhỏ" thành "đồ" -> "quà"
                self.assertEqual(entries.get("đồ"), "quà")
                self.assertEqual(gls.get_suggestions(), [])


class TestDuplicateGuard(unittest.TestCase):
    def test_same_content_detected_across_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = Path(tmp) / "control"
            ctrl.mkdir()
            with patch.object(duplicate_guard, "REGISTRY_FILE", ctrl / "seen.json"), \
                 patch.object(duplicate_guard, "CONTROL_DIR", ctrl):
                src = Path(tmp) / "src.mp4"
                src.write_bytes(b"FAKEVIDEO" * 1000)
                fp = duplicate_guard.register("output", "video_goc.mp4", src)
                self.assertTrue(fp)
                copy = Path(tmp) / "reup.mp4"
                copy.write_bytes(b"FAKEVIDEO" * 1000)  # nội dung giống hệt
                hit = duplicate_guard.check_path(copy)
                self.assertIsNotNone(hit)
                self.assertEqual(hit["kind"], "output")
                # nội dung khác -> không match
                other = Path(tmp) / "khac.mp4"
                other.write_bytes(b"COMPLETELY DIFFERENT CONTENT" * 10)
                self.assertIsNone(duplicate_guard.check_path(other))

    def test_upload_self_registration_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = Path(tmp) / "control"
            ctrl.mkdir()
            with patch.object(duplicate_guard, "REGISTRY_FILE", ctrl / "seen.json"), \
                 patch.object(duplicate_guard, "CONTROL_DIR", ctrl):
                f = Path(tmp) / "clip.mp4"
                f.write_bytes(b"XYZ" * 500)
                duplicate_guard.register("upload", f.name, f)
                # chính file vừa upload không bị coi là trùng của chính nó
                self.assertIsNone(duplicate_guard.check_path(f))


class TestWorkspaceUsage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        job = self.root / "job_alpha"
        (job / "bs_roformer").mkdir(parents=True)
        (job / "bs_roformer" / "stem.wav").write_bytes(b"x" * (3 * 1024 * 1024))
        (job / "dubbing").mkdir()
        (job / "dubbing" / "1.mp3").write_bytes(b"y" * 100)
        (job / "translated.srt").write_text("1\n00:00:00 --> 00:00:01\nXin chào\n", encoding="utf-8")
        (self.root / "bot_system").mkdir()  # phải bị bỏ qua
        self._patch = patch.object(wus, "_workspace_roots", lambda: [self.root])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.temporary.cleanup()

    def test_list_usage(self):
        d = wus.list_job_usage()
        self.assertEqual(len(d["jobs"]), 1)
        j = d["jobs"][0]
        self.assertEqual(j["name"], "job_alpha")
        self.assertGreater(j["stems_mb"], 2.0)
        self.assertTrue(j["has_srt"] and j["has_dubbing"])

    def test_cleanup_stems_keeps_srt_and_dubbing(self):
        res = wus.cleanup_jobs(["job_alpha"], "stems")
        self.assertEqual(res["results"][0]["status"], "ok")
        self.assertGreater(res["total_freed_mb"], 2.0)
        job = self.root / "job_alpha"
        self.assertFalse((job / "bs_roformer").exists())
        self.assertTrue((job / "translated.srt").exists())
        self.assertTrue((job / "dubbing").exists())

    def test_cleanup_full_and_guards(self):
        res = wus.cleanup_jobs(["job_alpha"], "full")
        self.assertEqual(res["results"][0]["status"], "ok")
        self.assertFalse((self.root / "job_alpha").exists())
        # Tên lạ / đang chạy bị từ chối
        res2 = wus.cleanup_jobs(["../evil", "job_beta"], "full", active_names=["job_beta.mp4"])
        self.assertEqual(res2["results"][0]["status"], "rejected")
        self.assertEqual(res2["results"][1]["status"], "rejected")


class TestGd3Api(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def test_duplicate_check_rejects_bad_filename(self):
        res = self.client.get("/api/phoi/duplicate-check", params={"filename": "../secret.mp4"})
        self.assertEqual(res.status_code, 400)

    def test_disk_usage_shape(self):
        res = self.client.get("/api/disk-usage")
        self.assertEqual(res.status_code, 200)
        drives = res.json()["drives"]
        self.assertEqual(len(drives), 3)
        self.assertEqual([d["label"] for d in drives], ["input", "workspace", "output"])

    def test_workspace_cleanup_validates_payload(self):
        res = self.client.post("/api/workspace/cleanup", json={"jobs": [], "mode": "stems"})
        self.assertEqual(res.status_code, 400)
        res2 = self.client.post("/api/workspace/cleanup", json={"jobs": ["x"], "mode": "weird"})
        self.assertEqual(res2.status_code, 400)

    def test_glossary_suggestions_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = Path(tmp) / "control"
            ctrl.mkdir()
            with patch.object(gls, "SUGGEST_FILE", ctrl / "glossary_suggestions.json"), \
                 patch.object(gls, "CONTROL_DIR", ctrl):
                gls.record_edit("aa bb", "aa cc", "v")
                res = self.client.get("/api/glossary/suggestions", params={"min_count": 1})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(len(res.json()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
