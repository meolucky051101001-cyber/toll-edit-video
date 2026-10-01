import os
import unittest
from unittest import mock
from backend.ai import voice_cloning

class TTSConcurrencyTests(unittest.TestCase):
    def test_default_semaphores_values(self):
        # Default CapCut worker capacity should be 6
        self.assertEqual(voice_cloning.capcut_semaphore._value, 6)
        # Default Edge worker capacity should be 4
        self.assertEqual(voice_cloning.edge_semaphore._value, 4)

    def test_capcut_client_pool_adapter(self):
        client = voice_cloning._get_capcut_client()
        self.assertIsNotNone(client)
        if hasattr(client, "session") and client.session is not None:
            adapter = client.session.get_adapter("https://")
            self.assertGreaterEqual(getattr(adapter, "_pool_connections", 0), 20)
            self.assertGreaterEqual(getattr(adapter, "_pool_maxsize", 0), 20)

    def tearDown(self):
        voice_cloning._shared_capcut_client = None

if __name__ == "__main__":
    unittest.main()
