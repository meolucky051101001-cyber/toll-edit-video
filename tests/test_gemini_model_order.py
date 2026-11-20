import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.ai import v1_gemini_dispatcher as dispatcher
from backend.v1_retry_policy import is_transient_network_error


class GeminiModelOrderTests(unittest.TestCase):
    def test_503_moves_to_the_next_model_without_trying_every_key(self):
        with tempfile.TemporaryDirectory() as directory:
            breaker = dispatcher.GeminiCircuitBreaker(Path(directory) / "circuit.db")
            success = MagicMock(status_code=200)
            success.json.return_value = {"result": "fixture"}
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["first", "second"]), \
                 patch.object(dispatcher, "_read_model_catalog", return_value=None), \
                 patch("requests.post", side_effect=[MagicMock(status_code=503, headers={}), success]) as post:
                _, model = dispatcher.call_gemini_api({}, models=["busy", "working"])
            self.assertEqual(model, "working")
            self.assertEqual(post.call_count, 2)
            self.assertIn("/working:", post.call_args.args[0])

    def test_timeout_followed_by_404_still_goes_to_job_backoff(self):
        import requests

        with tempfile.TemporaryDirectory() as directory:
            breaker = dispatcher.GeminiCircuitBreaker(Path(directory) / "circuit.db")
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["first"]), \
                 patch.object(dispatcher, "_read_model_catalog", return_value=None), \
                 patch("requests.post", side_effect=[requests.ReadTimeout("timed out"), MagicMock(status_code=404, headers={})]):
                with self.assertRaises(dispatcher.GeminiAllModelsFailedError) as caught:
                    dispatcher.call_gemini_api({}, models=["slow", "absent"])
            self.assertIsInstance(caught.exception.__cause__, TimeoutError)
            self.assertTrue(is_transient_network_error(caught.exception))

    def test_catalog_uses_only_keys_that_support_the_requested_model(self):
        with tempfile.TemporaryDirectory() as directory:
            breaker = dispatcher.GeminiCircuitBreaker(Path(directory) / "circuit.db")
            response = MagicMock(status_code=200)
            response.json.return_value = {"result": "fixture"}
            catalogs = {"primary": {"fallback"}, "alternate": {"preferred"}}
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=list(catalogs)), \
                 patch.object(dispatcher, "_read_model_catalog", side_effect=catalogs.get), \
                 patch("requests.post", return_value=response) as post:
                _, model = dispatcher.call_gemini_api({}, models=["missing", "preferred", "fallback"])
            self.assertEqual(model, "preferred")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "alternate")

    def test_catalog_refresh_is_shared_and_skips_non_generation_models(self):
        with tempfile.TemporaryDirectory() as directory:
            response = MagicMock()
            response.json.return_value = {"models": [
                {"name": "models/flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
            ]}
            with patch.object(dispatcher, "DB_DIR", Path(directory)), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["test-key"]), \
                 patch("requests.get", return_value=response) as fetch:
                catalog = dispatcher.refresh_gemini_model_catalog()
                self.assertEqual(dispatcher.refresh_gemini_model_catalog(), catalog)
                self.assertEqual(dispatcher._read_model_catalog("test-key"), {"flash"})
                self.assertEqual(fetch.call_count, 1)
                self.assertTrue(all("test-key" not in file.read_text() for file in Path(directory).rglob("*.json")))

    def test_unset_override_preserves_defaults_and_legacy_preference(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dispatcher.resolve_gemini_models("translation"), dispatcher.DEFAULT_TRANSLATION_MODELS)
            self.assertEqual(dispatcher.resolve_gemini_models("condensation"), dispatcher.DEFAULT_CONDENSATION_MODELS)
        with patch.dict(os.environ, {"GEMINI_MODEL": "preferred"}, clear=True):
            self.assertEqual(dispatcher.resolve_gemini_models("translation"), ["preferred"] + dispatcher.DEFAULT_TRANSLATION_MODELS)

    def test_overrides_trim_deduplicate_and_replace_legacy_preference(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "ignored", "V1_GEMINI_TRANSLATION_MODELS": " stable,preview,stable, ",
                                    "V1_GEMINI_CONDENSATION_MODELS": "compact, stable"}, clear=True):
            self.assertEqual(dispatcher.resolve_gemini_models("translation"), ["stable", "preview"])
            self.assertEqual(dispatcher.resolve_gemini_models("condensation"), ["compact", "stable"])
        with patch.dict(os.environ, {"V1_GEMINI_TRANSLATION_MODELS": ", ,"}, clear=True):
            with self.assertRaises(dispatcher.GeminiConfigError):
                dispatcher.resolve_gemini_models("translation")

    def test_translation_passes_override_to_dispatcher_and_cache_key(self):
        from backend.ai import translation
        from backend.ai import v1_translation_cache as cache
        cause = dispatcher.GeminiAllModelsFailedError("status=503")
        with patch.dict(os.environ, {"V1_GEMINI_TRANSLATION_MODELS": "stable,preview"}, clear=True), \
             patch.object(cache, "read_cache", return_value=None), \
             patch.object(cache, "cache_key", wraps=cache.cache_key) as key, \
             patch.object(translation, "extract_video_frames_base64", return_value=[]), \
             patch.object(translation, "mark_gemini_unhealthy"), \
             patch.object(dispatcher, "call_gemini_api", side_effect=cause) as call:
            failures = []
            self.assertIsNone(translation.translate_with_gemini(["你好"], api_key="fixture", failure_causes=failures,
                                                               stop_checker=lambda: False, overall_deadline=9999999999))
            self.assertEqual(call.call_args.kwargs["models"], ["stable", "preview"])
            self.assertEqual(key.call_args.args[1], ["stable", "preview"])
            self.assertIs(failures[-1], cause)

    def test_503_cooldown_skips_preview_then_probes_after_healthy_models(self):
        with tempfile.TemporaryDirectory() as directory:
            breaker = dispatcher.GeminiCircuitBreaker(Path(directory) / "circuit.db")
            account = dispatcher._get_account_hash("fixture")
            with patch.object(dispatcher.time, "time", return_value=1000):
                breaker.record_failure(account, "preview", "translation", 503, "HTTP_503")
            success = MagicMock(status_code=200)
            success.json.return_value = {"result": "fixture"}
            failure = MagicMock(status_code=503, headers={})
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["fixture"]), \
                 patch.object(dispatcher, "_record_dispatcher_success_model"), \
                 patch("requests.post", return_value=success) as post, \
                 patch.object(dispatcher.time, "time", return_value=1001):
                _, model = dispatcher.call_gemini_api({}, models=["preview", "stable"])
                self.assertEqual(model, "stable")
                self.assertEqual(post.call_count, 1)
                self.assertIn("/stable:", post.call_args.args[0])
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["fixture"]), \
                 patch.object(dispatcher, "_record_dispatcher_success_model"), \
                 patch("requests.post", side_effect=[failure, success]) as post, \
                 patch.object(dispatcher.time, "time", return_value=1061):
                self.assertEqual(breaker.get_candidate_models(account, "translation", ["preview", "stable"], reserve_probe=False),
                                 [("stable", "ACTIVE"), ("preview", "HALF_OPEN")])
                _, model = dispatcher.call_gemini_api({}, models=["preview", "stable"])
                self.assertEqual(model, "preview")
                self.assertIn("/stable:", post.call_args_list[0].args[0])
                self.assertIn("/preview:", post.call_args_list[1].args[0])

    def test_all_cooling_returns_transient_without_sleep_or_early_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            breaker = dispatcher.GeminiCircuitBreaker(Path(directory) / "circuit.db")
            account = dispatcher._get_account_hash("fixture")
            with patch.object(dispatcher.time, "time", return_value=1000):
                breaker.record_failure(account, "preview", "translation", 503, "HTTP_503")
            with patch.object(dispatcher, "circuit_breaker", breaker), \
                 patch.object(dispatcher, "get_gemini_api_keys", return_value=["fixture"]), \
                 patch.object(dispatcher.time, "time", return_value=1001), \
                 patch.object(dispatcher.time, "sleep") as sleep, patch("requests.post") as post:
                with self.assertRaises(dispatcher.GeminiAllModelsFailedError) as caught:
                    dispatcher.call_gemini_api({}, models=["preview"])
                self.assertTrue(is_transient_network_error(caught.exception))
                post.assert_not_called()
                sleep.assert_not_called()
            with patch.object(dispatcher.time, "time", return_value=1061):
                self.assertEqual(breaker.get_candidate_models(account, "translation", ["preview"]), [("preview", "HALF_OPEN")])
                self.assertEqual(breaker.get_candidate_models(account, "translation", ["preview"]), [])
