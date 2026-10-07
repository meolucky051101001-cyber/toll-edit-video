import os
import sys
import unittest
from pathlib import Path

# Add backend to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

from a2ui.protocol import (
    CreateSurface, UpdateComponents, UpdateDataModel,
    DeleteSurface, CallAgentFunction, A2UIMessage, parse_a2ui_message
)
from a2ui.catalog import MEDIA_COMPONENT_CATALOG, is_component_valid
from a2ui.mock_agent import get_preset_scenario
from a2ui.agent_generator import generate_a2ui_for_video
from main import app
from fastapi.testclient import TestClient


class TestA2UIProtocol(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_protocol_message_serialization(self):
        cs = CreateSurface(surfaceId="surf-1", catalogId="test-catalog")
        msg = A2UIMessage(cs)
        d = msg.to_dict()
        self.assertEqual(d["version"], "v1.0")
        self.assertIn("createSurface", d)
        self.assertEqual(d["createSurface"]["surfaceId"], "surf-1")

        parsed = parse_a2ui_message(d)
        self.assertEqual(parsed["createSurface"]["catalogId"], "test-catalog")

    def test_component_catalog_validation(self):
        self.assertIn("components", MEDIA_COMPONENT_CATALOG)
        self.assertTrue(is_component_valid("Card"))
        self.assertTrue(is_component_valid("VoiceSelectorCard"))
        self.assertTrue(is_component_valid("SubtitleReviewCard"))
        self.assertTrue(is_component_valid("AudioMixerCard"))
        self.assertFalse(is_component_valid("ArbitraryMaliciousScript"))

    def test_mock_scenarios(self):
        for name in ["multivoice", "subtitles", "rater"]:
            messages = get_preset_scenario(name)
            self.assertIsInstance(messages, list)
            self.assertGreater(len(messages), 0)
            self.assertIn("version", messages[0])

    def test_agent_generator(self):
        messages = generate_a2ui_for_video("sample_test.mp4")
        self.assertIsInstance(messages, list)
        self.assertGreaterEqual(len(messages), 3)
        self.assertIn("createSurface", messages[0])
        self.assertIn("updateComponents", messages[1])

    def test_a2ui_endpoints(self):
        # 1. Studio page
        res = self.client.get("/a2ui")
        self.assertEqual(res.status_code, 200)
        self.assertIn("A2UI Studio", res.text)

        # 2. Catalog endpoint
        res_cat = self.client.get("/api/a2ui/catalog")
        self.assertEqual(res_cat.status_code, 200)
        self.assertIn("components", res_cat.json())

        # 3. Scenario endpoint
        res_scen = self.client.get("/api/a2ui/scenario/multivoice")
        self.assertEqual(res_scen.status_code, 200)
        self.assertIn("messages", res_scen.json())

        # 4. Action RPC endpoint
        res_act = self.client.post("/api/a2ui/action", json={
            "name": "apply_dual_voices",
            "parameters": {"male": "rvc-1", "female": "edge-1"},
            "functionCallId": "test-call-123"
        })
        self.assertEqual(res_act.status_code, 200)
        data = res_act.json()
        self.assertIn("agentFunctionResponse", data)
        self.assertEqual(data["agentFunctionResponse"]["functionCallId"], "test-call-123")


if __name__ == "__main__":
    unittest.main()
