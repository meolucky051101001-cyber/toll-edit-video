# Pipeline v2 phases 1 and 2

The code in `backend/pipeline_v2` is additive and opt-in. The legacy Telegram,
batch and FastAPI entry points do not import it, so these phases do not change
current rendering behaviour.

## Phase 1: durable checkpoints

The package provides:

- `FingerprintSet` for source, configuration and model cache identity.
- `JobManifest` with guarded stage transitions and retry counters.
- `ManifestStore` for atomic `job_manifest.json` persistence.
- `ArtifactStore` for same-directory staging, SHA-256 verification and atomic
  publication with `os.replace()`.
- Crash recovery that changes an interrupted `running` stage to retryable
  `failed` state.
- Downstream invalidation without deleting previous artifacts.

Minimal integration shape for a later orchestrator phase:

```python
from backend.pipeline_v2 import ArtifactStore, FingerprintSet, ManifestStore

manifest_store = ManifestStore(job_directory)
artifact_store = ArtifactStore(job_directory / "artifacts")
manifest = manifest_store.create(job_id, fingerprints)

manifest.start_stage("extract_audio")
staged = artifact_store.staging_path("audio/original.wav")
# The stage writes and closes `staged` here.
artifact = artifact_store.commit_staged(staged, "audio/original.wav")
manifest.complete_stage("extract_audio", [artifact])
manifest_store.save(manifest)
```

No legacy entry point should use this example until the shadow orchestrator
phase is explicitly enabled.

## Phase 2: report-only QC

Run the standalone QC command after a render:

```powershell
python -m backend.pipeline_v2.qc `
  --video D:\banve\Dubbed_example.mp4 `
  --audio D:\workspace\example\mixed.wav `
  --segments D:\workspace\example\translated.srt `
  --ass D:\workspace\example\final.ass `
  --report D:\workspace\example\qc_report.json `
  --diagnostics-dir D:\workspace\example\qc_diagnostics
```

It checks:

- final video/audio streams and codecs from FFprobe;
- audio/video duration difference;
- integrated loudness, true peak and long silence from FFmpeg;
- missing, duplicated, empty or invalid segments;
- explicit ASS position anchors against a configurable safe area;
- atomically published first, middle and last diagnostic frames.

Every report is permanently marked with:

```json
{
  "mode": "report_only",
  "blocking": false,
  "delivery_allowed": true
}
```

Warnings and errors are visible in the report but never produce a failed
render or prohibit delivery in phase 2. The CLI exits successfully for QC
findings; invalid command-line usage or inability to publish the report remains
an operational error.

## Verification

```powershell
python -m unittest discover -s tests -v
python -m compileall -q backend tests
```
