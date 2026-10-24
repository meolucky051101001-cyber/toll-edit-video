import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backend.render_history as render_history


class RenderHistoryWorkspaceDiscoveryTests(unittest.TestCase):
    def test_v1_manifest_is_registered_from_discovered_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            manifest_dir = workspace / "bot_system" / "manifests"
            manifest_dir.mkdir(parents=True)
            shared_workspace = workspace / "shared"
            shared_workspace.mkdir()
            manifest = {
                "video_name": "sample.mp4",
                "output_video_path": r"D:\banve\Dubbed_sample.mp4",
                "stages": {"render": {"duration_s": 42}},
            }
            (manifest_dir / "sample.manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with patch.object(render_history, "_workspace_roots", return_value=[workspace]), \
                 patch.object(render_history, "HISTORY_FILE", workspace / "empty-history.json"), \
                 patch.object(render_history, "PATHS", SimpleNamespace(shared_workspace_dir=shared_workspace)):
                durations = render_history.get_all_render_durations(workspace)

        self.assertEqual(durations["sample.mp4"], 42)
        self.assertEqual(durations["Dubbed_sample.mp4"], 42)


if __name__ == "__main__":
    unittest.main()
