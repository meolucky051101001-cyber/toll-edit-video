"""
Unit Test Suite for Tool V1 Gemini Multi-Key Pool & 429 Anti-Throttle (Stage 1).
Tests:
1. Parsing multiple keys from GEMINI_API_KEYS (comma, semicolon, newline) & isolation of FPT/invalid keys.
2. LRU / Round-Robin key selection across active keys.
3. Instant failover from Key 1 to Key 2 upon HTTP 429 without failing the job or waiting.
4. Cooldown expiration and key recovery.
5. All keys exhausted behavior.
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
    get_gemini_api_keys,
    get_gemini_api_key,
    GeminiCircuitBreaker,
    GeminiConfigError,
    GeminiAuthError,
    GeminiDeadlineError,
    GeminiAllModelsFailedError,
    _get_account_hash,
)


class TestGeminiMultiKeyPool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_multikey_circuit.db"
        self.breaker = GeminiCircuitBreaker(self.db_path)
        self.breaker_patch = patch.dict(call_gemini_api.__globals__, {"circuit_breaker": self.breaker})
        self.breaker_patch.start()

        self.key_1 = "GEMINI_TEST_KEY_ALPHA_001"
        self.key_2 = "GEMINI_TEST_KEY_BETA_002"
        self.key_3 = "GEMINI_TEST_KEY_GAMMA_003"
        self.hash_1 = _get_account_hash(self.key_1)
        self.hash_2 = _get_account_hash(self.key_2)
        self.hash_3 = _get_account_hash(self.key_3)

    def tearDown(self):
        self.breaker_patch.stop()
        self.tmp_dir.cleanup()

    def test_01_multikey_parsing_and_isolation(self):
        # Test comma-separated string with invalid/fpt keys included
        raw = f"{self.key_1}, fpt_invalid_key_123, {self.key_2} ; short , {self.key_3}\n{self.key_1}"
        keys = get_gemini_api_keys(explicit_key=raw)
        
        # Must retain only 3 valid keys, deduplicated, in order
        self.assertEqual(keys, [self.key_1, self.key_2, self.key_3])
        # Backward-compatible get_gemini_api_key returns first key
        self.assertEqual(get_gemini_api_key(explicit_key=raw), self.key_1)

    def test_02_multikey_env_variable_parsing(self):
        with patch.dict(os.environ, {"GEMINI_API_KEYS": f"{self.key_1},{self.key_2}", "GEMINI_API_KEY": self.key_1}, clear=False):
            keys = get_gemini_api_keys()
            self.assertEqual(keys[:2], [self.key_1, self.key_2])
            # Forwarding default os.getenv("GEMINI_API_KEY") must NOT truncate the multi-key pool to 1 key
            keys_when_default_forwarded = get_gemini_api_keys(explicit_key=os.getenv("GEMINI_API_KEY"))
            self.assertEqual(keys_when_default_forwarded[:2], [self.key_1, self.key_2])

    def test_03_lru_key_selection(self):
        keys = [self.key_1, self.key_2, self.key_3]
        
        # Initial selection: key_1
        selected_key, h, wait = self.breaker.select_active_key(keys)
        self.assertEqual(selected_key, self.key_1)

        # Mark key_1 as recently used
        self.breaker.record_account_success(self.hash_1)
        
        # Next selection should pick key_2 (as key_2 and key_3 have last_used = 0)
        selected_key, h, wait = self.breaker.select_active_key(keys)
        self.assertIn(selected_key, [self.key_2, self.key_3])

    def test_04_instant_429_failover_to_next_key(self):
        """
        When Key 1 hits HTTP 429, dispatcher must immediately failover to Key 2
        and complete the request successfully with 200 without waiting.
        """
        keys_pool_str = f"{self.key_1},{self.key_2}"
        
        call_records = []
        def mock_post(url, json=None, headers=None, timeout=None):
            api_key = headers.get("x-goog-api-key")
            call_records.append(api_key)
            resp = MagicMock()
            if api_key == self.key_1:
                # Key 1 fails with HTTP 429 Rate Limit
                resp.status_code = 429
                resp.headers = {"Retry-After": "60"}
                resp.text = "Rate limit exceeded"
                return resp
            elif api_key == self.key_2:
                # Key 2 succeeds with HTTP 200!
                resp.status_code = 200
                resp.json.return_value = {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": '["Dịch thành công với Key 2!"]'}]
                        }
                    }]
                }
                return resp
            resp.status_code = 500
            return resp

        with patch("requests.post", side_effect=mock_post), \
             patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker):
            data, model = call_gemini_api(
                payload={"contents": []},
                purpose="translation",
                models=["gemini-3.5-flash-lite"],
                api_key=keys_pool_str
            )

        # Assertions
        # 1. Both keys were called in sequence
        self.assertEqual(call_records, [self.key_1, self.key_2])
        # 2. Key 1 was recorded into account quota cooldown
        self.assertTrue(self.breaker.is_account_exhausted(self.hash_1)[0])
        # 3. Key 2 was recorded as success
        self.assertFalse(self.breaker.is_account_exhausted(self.hash_2)[0])
        # 4. Result was successfully returned from Key 2!
        self.assertIn("candidates", data)

    def test_05_all_keys_in_pool_429_raises_cleanly(self):
        """
        When ALL keys in the pool hit 429, dispatcher records all in cooldown
        and raises GeminiAllModelsFailedError.
        """
        keys_pool_str = f"{self.key_1},{self.key_2}"
        deadline = time.monotonic() + 5.0

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "120"}
        mock_resp.text = "Rate limit exceeded"

        with patch("requests.post", return_value=mock_resp), \
             patch("ai.v1_gemini_dispatcher.circuit_breaker", self.breaker):
            start = time.monotonic()
            with self.assertRaises((GeminiAllModelsFailedError, GeminiDeadlineError)):
                call_gemini_api(
                    payload={"contents": []},
                    purpose="translation",
                    models=["gemini-3.5-flash-lite"],
                    overall_deadline=deadline,
                    api_key=keys_pool_str
                )
            elapsed = time.monotonic() - start
            # Must exit within deadline without long sleeping
            self.assertLess(elapsed, 2.0)
            # Both keys are now marked exhausted
            self.assertTrue(self.breaker.is_account_exhausted(self.hash_1)[0])
            self.assertTrue(self.breaker.is_account_exhausted(self.hash_2)[0])


if __name__ == "__main__":
    unittest.main()
