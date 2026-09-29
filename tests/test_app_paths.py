import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.paths import AppPaths


class AppPathsTests(unittest.TestCase):
    def test_relative_overrides_resolve_from_project_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = AppPaths.from_environment(
                root,
                {
                    "AUTODUB_WORKSPACE": "state/workspace",
                    "AUTODUB_INPUT_DIR": "media/incoming",
                    "AUTODUB_OUTPUT_DIR": "media/rendered",
                    "AUTODUB_MODEL_CACHE": "cache/models",
                },
            )

        self.assertEqual(paths.workspace, (root / "state/workspace").resolve())
        self.assertEqual(paths.input_dir, (root / "media/incoming").resolve())
        self.assertEqual(paths.output_dir, (root / "media/rendered").resolve())
        self.assertEqual(paths.model_cache, (root / "cache/models").resolve())

    def test_absolute_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            external = Path(temporary) / "external-output"
            paths = AppPaths.from_environment(
                root, {"AUTODUB_OUTPUT_DIR": str(external)}
            )

        self.assertEqual(paths.output_dir, external.resolve())

    def test_shared_assets_and_model_cache_can_be_configured(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            shared_assets = Path(temporary) / "shared-assets"
            model_cache = Path(temporary) / "model-cache"
            shared_workspace = Path(temporary) / "shared-workspace"
            paths = AppPaths.from_environment(
                root,
                {
                    "AUTODUB_SHARED_ASSETS_DIR": str(shared_assets),
                    "AUTODUB_SHARED_WORKSPACE_DIR": str(shared_workspace),
                    "AUTODUB_MODEL_CACHE": str(model_cache),
                },
            )

        self.assertEqual(paths.shared_assets_dir, shared_assets.resolve())
        self.assertEqual(paths.shared_workspace_dir, shared_workspace.resolve())
        self.assertEqual(paths.model_cache, model_cache.resolve())

    def test_existing_repository_model_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            expected = root / "models"
            expected.mkdir(parents=True)
            paths = AppPaths.from_environment(root, {})

        self.assertEqual(paths.model_cache, expected.resolve())

    def test_existing_repository_workspace_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            expected = root / "workspace"
            expected.mkdir(parents=True)
            paths = AppPaths.from_environment(root, {})

        self.assertEqual(paths.workspace, expected.resolve())

    def test_data_root_override_selects_new_workspace_and_model_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            (root / "workspace").mkdir(parents=True)
            (root / "models").mkdir()
            data_root = Path(temporary) / "user-data"
            paths = AppPaths.from_environment(
                root, {"AUTODUB_DATA_DIR": str(data_root)}
            )

        self.assertEqual(paths.workspace, (data_root / "workspace").resolve())
        self.assertEqual(paths.model_cache, (data_root / "models").resolve())

    def test_dotenv_environment_variables_expand(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            target = Path(temporary) / "custom-workspace"
            with patch.dict(os.environ, {"AUTODUB_TEST_ROOT": temporary}):
                paths = AppPaths.from_environment(
                    root,
                    {
                        "AUTODUB_TEST_ROOT": temporary,
                        "AUTODUB_WORKSPACE": "$AUTODUB_TEST_ROOT/custom-workspace",
                    },
                )

        self.assertEqual(paths.workspace, target.resolve())

    def test_defaults_are_portable_and_outside_the_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            paths = AppPaths.from_environment(root, {})

        self.assertEqual(paths.project_root, root.resolve())
        self.assertNotEqual(paths.workspace, root / "workspace")
        self.assertEqual(paths.input_dir.name, "input")
        self.assertEqual(paths.output_dir.name, "output")
        self.assertTrue(paths.workspace.is_absolute())
        self.assertTrue(paths.input_dir.is_absolute())
        self.assertTrue(paths.output_dir.is_absolute())


if __name__ == "__main__":
    unittest.main()
