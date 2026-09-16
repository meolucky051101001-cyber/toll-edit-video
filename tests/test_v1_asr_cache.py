import unittest
from unittest.mock import Mock
from backend.ai.v1_asr_cache import ModelCache

class CacheTests(unittest.TestCase):
    def test_reuses_and_parks(self):
        cache = ModelCache(memory_ok=lambda: True)
        factory = Mock(return_value=Mock())
        try:
            with cache.lease("a", factory) as first:
                pass
            with cache.lease("a", factory) as second:
                self.assertIs(first, second)
            factory.assert_called_once()
            first.model.load_model.assert_called_once()
            self.assertEqual(first.model.unload_model.call_count, 2)
        finally:
            cache.close()

    def test_low_memory_discards(self):
        cache = ModelCache(memory_ok=lambda: False)
        with cache.lease("a", Mock(return_value=Mock())):
            pass
        self.assertIsNone(cache.model)

    def test_error_discards(self):
        cache = ModelCache(memory_ok=lambda: True)
        with self.assertRaises(ValueError):
            with cache.lease("a", Mock(return_value=Mock())):
                raise ValueError("test")
        self.assertIsNone(cache.model)

    def test_changed_key_and_idle_eviction(self):
        cache = ModelCache(memory_ok=lambda: True, ttl=0)
        factory = Mock(side_effect=lambda: Mock())
        try:
            with cache.lease("a", factory):
                pass
            with cache.lease("b", factory):
                pass
            self.assertEqual(factory.call_count, 2)
            cache.close()
            self.assertIsNone(cache.model)
        finally:
            cache.close()
