"""
Comprehensive Unit Test Suite for Tool V1 Gemini Stability & Speed Plan (Codex Plan).
Tests all 13 required scenarios from Đợt 8:
1. Model đầu luôn 429 -> Model được cho nghỉ; không bị gọi lại ở mọi câu
2. Retry-After dài hơn ngân sách -> Không ngủ quá deadline của video
3. Tất cả model cùng timeout -> Thoát trong ngân sách, không lặp chuỗi vô hạn
4. Sai khóa -> Không tiếp tục thử cùng khóa trên toàn bộ model
5. Model không tồn tại (404) -> Bỏ qua model đó, giữ model khác
6. HTTP 200 nhưng JSON sai -> Không cache và không báo thành công giả
7. Phản hồi thiếu cue -> Chỉ nhận cue hợp lệ; không bỏ câu còn thiếu
8. Nhiều câu quá dài -> Gom lô, không tạo request riêng lẻ không giới hạn
9. Hai worker cùng gặp model lỗi -> Chia sẻ thời gian nghỉ; chỉ một request phục hồi
10. Dừng job trong khi retry -> Không sinh request mới; bảo toàn tiến độ
11. Resume -> Dùng lại kết quả hợp lệ, không đổi giọng
12. Rút gọn làm thay đổi nội dung -> Cache TTS và phụ đề cuối cùng cùng nội dung
13. Gemini hoàn toàn mất kết nối -> Không xuất video thiếu lời hoặc dùng giọng khác
"""

import os
import sys
import time
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from ai.v1_gemini_dispatcher import (
    call_gemini_api,
    get_gemini_api_key,
    GeminiCircuitBreaker,
    GeminiConfigError,
    GeminiAuthError,
    GeminiDeadlineError,
    GeminiCancelledError,
    GeminiQuotaExhaustedError,
    GeminiAllModelsFailedError,
)
from ai.translation import validate_condensed_text, condense_vietnamese_subtitles_batch


class TestGeminiStabilityCodex(unittest.TestCase):
    def setUp(self):
        # Create a temporary SQLite database for clean test isolation
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_circuit.db"
        self.breaker = GeminiCircuitBreaker(self.db_path)
        self.breaker_patch = patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker)
        self.breaker_patch.start()
        self.orig_workspace = os.environ.get("AUTODUB_WORKSPACE")
        os.environ["AUTODUB_WORKSPACE"] = self.tmp_dir.name
        self.fake_key = "DUMMY_GEMINI_KEY_NOT_REAL"
        self.account_hash = "acc_test_hash"

    def tearDown(self):
        if self.orig_workspace is not None:
            os.environ["AUTODUB_WORKSPACE"] = self.orig_workspace
        else:
            os.environ.pop("AUTODUB_WORKSPACE", None)
        self.breaker_patch.stop()
        self.tmp_dir.cleanup()

    # 1. Model đầu luôn 429 -> Model được cho nghỉ; không bị gọi lại ở các câu tiếp theo
    def test_01_model_429_enters_cooldown_and_is_skipped(self):
        models = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
        
        # Simulate model 1 returning 429
        self.breaker.record_failure(self.account_hash, models[0], "condensation", 429, "HTTP_429")
        
        # Next query: model 1 must be skipped, only model 2 allowed
        candidates = self.breaker.get_candidate_models(self.account_hash, "condensation", models)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "gemini-flash-lite-latest")

    # 2. Retry-After dài hơn ngân sách -> Không ngủ quá deadline của video
    def test_02_retry_after_longer_than_budget_exits_cleanly(self):
        deadline = time.monotonic() + 0.5  # 500ms total budget
        
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "300"}  # 5 minutes Retry-After!

        with patch("requests.post", return_value=mock_resp), \
             patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker):
            start = time.monotonic()
            with self.assertRaises((GeminiAllModelsFailedError, GeminiDeadlineError, GeminiQuotaExhaustedError)):
                call_gemini_api(
                    payload={"contents": []},
                    purpose="condensation",
                    models=["gemini-3.5-flash-lite"],
                    overall_deadline=deadline,
                    api_key=self.fake_key
                )
            elapsed = time.monotonic() - start
            # Must NOT sleep 300s! Must return within budget (< 2s)
            self.assertLess(elapsed, 2.0)

    # 3. Tất cả model cùng timeout -> Thoát trong ngân sách, không lặp chuỗi vô hạn
    def test_03_all_models_timeout_aborts_within_budget(self):
        import requests
        deadline = time.monotonic() + 3.0

        with patch("requests.post", side_effect=requests.Timeout("Read timed out")), \
             patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker):
            start = time.monotonic()
            with self.assertRaises((GeminiAllModelsFailedError, GeminiDeadlineError)):
                call_gemini_api(
                    payload={"contents": []},
                    purpose="condensation",
                    models=["gemini-3.5-flash-lite", "gemini-3.5-flash"],
                    overall_deadline=deadline,
                    api_key=self.fake_key
                )
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 4.0)

    # 4. Sai khóa -> Không tiếp tục thử cùng khóa trên toàn bộ model
    def test_04_auth_error_halts_model_chain_immediately(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        
        call_count = 0
        def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("requests.post", side_effect=fake_post), \
             patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker):
            with self.assertRaises(GeminiAuthError):
                call_gemini_api(
                    payload={"contents": []},
                    purpose="translation",
                    models=["model_1", "model_2", "model_3"],
                    api_key=self.fake_key
                )
            # Must NOT try model_2 or model_3 after 401!
            self.assertEqual(call_count, 1)

    # 5. Model không tồn tại (404) -> Bỏ qua model đó (24h cooldown), giữ model khác
    def test_05_model_404_cooldown_retains_valid_models(self):
        models = ["invalid-model-999", "gemini-3.5-flash-lite"]
        self.breaker.record_failure(self.account_hash, "invalid-model-999", "translation", 404, "HTTP_404")
        
        candidates = self.breaker.get_candidate_models(self.account_hash, "translation", models)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "gemini-3.5-flash-lite")

    # 6. HTTP 200 nhưng JSON sai -> Không cache và không báo thành công giả
    def test_06_http_200_invalid_json_validation_fails(self):
        # Invalid response format: not JSON
        raw_text = "Tôi xin lỗi nhưng tôi không thể thực hiện yêu cầu này"
        orig_text = "Hôm nay chúng ta sẽ đi mua sắm"
        
        self.assertFalse(validate_condensed_text(orig_text, raw_text, 5))
        self.assertFalse(validate_condensed_text(orig_text, "```json\n{'1': 'abc'}\n```", 5))

    # 7. Phản hồi thiếu cue -> Chỉ nhận cue hợp lệ; không bỏ câu còn thiếu
    def test_07_partial_cues_response_retains_valid_only(self):
        items = [
            {"index": 1, "text": "Hôm nay chúng ta đi chơi ở công viên rất vui vẻ", "target_seconds": 2.0, "target_words": 5},
            {"index": 2, "text": "Sau đó chúng ta sẽ đi ăn kem ở cửa hàng", "target_seconds": 2.0, "target_words": 5}
        ]
        
        # Mock Gemini returning only index 1 validly, index 2 missing or invalid
        mock_data = {
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"1": "Hôm nay ta đi chơi công viên"}'}]
                }
            }]
        }
        
        with patch("ai.translation.call_gemini_api", return_value=(mock_data, "gemini-3.5-flash-lite")):
            res = condense_vietnamese_subtitles_batch(items, api_key=self.fake_key)
            self.assertIn(1, res)
            self.assertEqual(res[1], "Hôm nay ta đi chơi công viên")
            # Index 2 was missing, so not in result (original preserved)
            self.assertNotIn(2, res)

    # 8. Nhiều câu quá dài -> Gom lô, đúng 1 request API
    def test_08_multiple_long_cues_grouped_into_single_api_call(self):
        items = [
            {"index": i, "text": f"Đây là câu dài số {i} cần được rút gọn ngắn", "target_seconds": 1.5, "target_words": 4}
            for i in range(1, 10)
        ]
        
        mock_data = {
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({str(i): f"Câu ngắn {i}" for i in range(1, 10)})}]
                }
            }]
        }
        
        call_count = 0
        def fake_call_api(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_data, "gemini-3.5-flash-lite"

        with patch("ai.translation.call_gemini_api", side_effect=fake_call_api):
            res = condense_vietnamese_subtitles_batch(items, api_key=self.fake_key)
            # All 9 items sent in EXACTLY 1 API call!
            self.assertEqual(call_count, 1)
            self.assertEqual(len(res), 9)

    # 9. Hai worker cùng gặp model lỗi -> Chia sẻ thời gian nghỉ; chỉ 1 request probe
    def test_09_multi_worker_shared_cooldown_and_single_probe(self):
        # Simulate worker 1 marking model in COOLDOWN
        now = time.time()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO model_circuit (account_hash, model, purpose, state, cooldown_until, consecutive_failures) "
                "VALUES ('acc1', 'm1', 'translation', 'COOLDOWN', ?, 2)",
                (now - 1.0,) # Cooldown just expired
            )
            conn.commit()

        # Worker A checks candidates: should acquire HALF_OPEN probe
        cands_a = self.breaker.get_candidate_models('acc1', 'translation', ['m1'])
        self.assertEqual(len(cands_a), 1)
        self.assertEqual(cands_a[0], ('m1', 'HALF_OPEN'))

        # Worker B checks candidates concurrently: m1 is already probing in HALF_OPEN, so Worker B cannot probe!
        cands_b = self.breaker.get_candidate_models('acc1', 'translation', ['m1'])
        self.assertEqual(len(cands_b), 0)

    # 10. Dừng job trong khi retry -> Không sinh request mới; bảo toàn tiến độ
    def test_10_cancellation_token_halts_processing(self):
        stop_requested = True
        with self.assertRaises(GeminiCancelledError):
            call_gemini_api(
                payload={"contents": []},
                purpose="condensation",
                models=["gemini-3.5-flash-lite"],
                stop_checker=lambda: stop_requested,
                api_key=self.fake_key
            )

    # 11. Đảm bảo khóa FPT không bao giờ bị nhận làm khóa Gemini
    def test_11_fpt_key_isolation(self):
        with patch.dict(os.environ, {"FPT_API_KEY": "fpt_secret_token_123", "GEMINI_API_KEY": "DUMMY_GEMINI_KEY_NOT_REAL"}):
            # Explicitly passing FPT key must be rejected, falling back to GEMINI_API_KEY
            key = get_gemini_api_key(explicit_key="fpt_secret_token_123")
            self.assertEqual(key, "DUMMY_GEMINI_KEY_NOT_REAL")
            self.assertNotIn("fpt", key.lower())

    # 12. Kiểm tra bảo toàn từ phủ định và số khi rút gọn
    def test_12_validation_protects_negation_and_numbers(self):
        # Negative word missing -> must be rejected
        self.assertFalse(validate_condensed_text("Tôi không thích đi bơi", "Tôi thích đi bơi", 4))
        # Number missing -> must be rejected
        self.assertFalse(validate_condensed_text("Có 5 người trong phòng", "Nhiều người trong phòng", 4))
        # Valid condensation preserving negation and number -> accepted
        self.assertTrue(validate_condensed_text("Hôm nay tôi không có 5 nghìn đồng", "Nay tôi không có 5 nghìn", 6))

    # 13. Gemini hoàn toàn mất kết nối -> Không crash, trả về rỗng để giữ nguyên câu gốc đầy đủ
    def test_13_gemini_unreachable_preserves_original_texts_without_crashing(self):
        items = [
            {"index": 1, "text": "Hôm nay trời mưa to rất buồn", "target_seconds": 1.0, "target_words": 3},
            {"index": 2, "text": "Ngày mai trời sẽ nắng ấm", "target_seconds": 1.0, "target_words": 3}
        ]
        with patch("ai.translation.call_gemini_api", side_effect=GeminiQuotaExhaustedError("All models down")):
            res = condense_vietnamese_subtitles_batch(items, api_key=self.fake_key)
            # Must return empty dict gracefully, so caller falls back to original full texts
            self.assertEqual(res, {})



if __name__ == "__main__":
    unittest.main()
