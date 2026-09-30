import tempfile
import unittest
from pathlib import Path

from backend.pipeline_v2 import ArtifactStore, FingerprintSet, ManifestStore
from backend.pipeline_v2.config import PipelineMode, PipelineSettings, QCGatePolicy
from backend.pipeline_v2.orchestrator import (
    PipelineContext,
    PipelineOrchestrator,
    StageDefinition,
    StageOutcome,
)
from backend.pipeline_v2.stage_status import StageStatus


class PipelineSettingsTests(unittest.TestCase):
    def test_v2_is_the_default_for_the_v2_release_branch(self):
        settings = PipelineSettings.from_env({})
        self.assertEqual(settings.mode, PipelineMode.V2)
        self.assertFalse(settings.enable_stage_cache)
        self.assertTrue(settings.preserve_source_resolution)
        self.assertFalse(settings.enable_auto_gender)
        self.assertTrue(settings.enable_timing_solver)
        self.assertEqual(settings.qc_gate_policy, QCGatePolicy.BLOCK)

    def test_auto_gender_can_be_disabled_explicitly(self):
        settings = PipelineSettings.from_env({"ENABLE_AUTO_GENDER": "false"})
        self.assertFalse(settings.enable_auto_gender)

    def test_auto_gender_requires_explicit_opt_in(self):
        self.assertFalse(PipelineSettings().enable_auto_gender)
        self.assertTrue(PipelineSettings.from_env({"ENABLE_AUTO_GENDER": "true"}).enable_auto_gender)

    def test_rejects_unsafe_atempo_bounds(self):
        with self.assertRaises(ValueError):
            PipelineSettings.from_env({"ATEMPO_MAX": "2.1"})


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_orders_stages_and_reuses_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            manifest_store = ManifestStore(job)
            manifest = manifest_store.create(
                "job",
                FingerprintSet("source", "config", {}),
                stage_names=("extract", "render"),
            )
            artifact_store = ArtifactStore(job / "artifacts")
            calls = []

            def extract(context):
                calls.append("extract")
                artifact = context.artifact_store.put_text("audio.wav", "audio")
                return StageOutcome([artifact], {"audio": artifact.key})

            def render(context):
                calls.append("render")
                self.assertEqual(context.values["audio"], "audio.wav")
                artifact = context.artifact_store.put_text("video.mp4", "video")
                return StageOutcome([artifact])

            stages = [
                StageDefinition("extract", extract),
                StageDefinition("render", render, dependencies=("extract",)),
            ]
            context = PipelineContext(manifest, manifest_store, artifact_store)
            await PipelineOrchestrator(stages).run(context)
            self.assertEqual(calls, ["extract", "render"])

            calls.clear()
            cached_context = PipelineContext(
                manifest_store.load(), manifest_store, artifact_store
            )
            await PipelineOrchestrator(stages).run(cached_context)
            self.assertEqual(calls, [])

    async def test_failure_is_checkpointed(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            store = ManifestStore(job)
            manifest = store.create(
                "job",
                FingerprintSet("source", "config", {}),
                stage_names=("explode",),
            )

            def explode(_context):
                raise RuntimeError("boom")

            context = PipelineContext(manifest, store, ArtifactStore(job / "artifacts"))
            with self.assertRaises(RuntimeError):
                await PipelineOrchestrator(
                    [StageDefinition("explode", explode)]
                ).run(context)
            saved = store.load().stage("explode")
            self.assertEqual(saved.status, StageStatus.FAILED)
            self.assertEqual(saved.error["message"], "boom")


if __name__ == "__main__":
    unittest.main()
