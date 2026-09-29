# Tool V2 backend: current layout and safe migration plan

## Current layout

Tool V2 already has useful boundaries, but they are not yet consistently
packaged. The current structure is:

| Responsibility | Current location | Direction |
| --- | --- | --- |
| API and dashboard composition | `backend/main.py`, `backend/dashboard_monitor.py`, `backend/workflow_api.py` | Gradually extract routers into `backend/app/` |
| Business services | `backend/*_service.py`, `backend/telegram_jobs.py` | Move reusable application rules into `backend/services/` with import shims |
| Media pipeline | `backend/pipeline_v2/` | Keep as the canonical pipeline package; do not duplicate it under `pipelines/` |
| AI/model integrations | `backend/ai/`, `backend/model_workers/`, `backend/social_downloader.py` | Gradually put provider adapters behind `backend/integrations/` |
| Shared configuration | `backend/environment.py`, `backend/pipeline_v2/config.py` | `backend/config/` now owns shared filesystem path resolution; continue centralizing validated settings here |

The existing `pipeline_v2/` and `ai/` code is used by both direct scripts and
package imports. Moving all files at once would risk breaking Telegram, the API,
Electron startup, batch mode, and resume checkpoints. The migration therefore
keeps compatibility imports and changes one boundary at a time.

## Path and environment rules

- Process environment variables take precedence over `backend/.env`.
- Absolute paths are used as-is; relative path values resolve from the repository
  root, never from the caller's current working directory.
- Without overrides, workspace, new model cache, and shared assets use the OS
  user-data area; input/output default to `~/Videos/AutoDubV2/input` and
  `output`.
- Set `AUTODUB_DATA_DIR`, `AUTODUB_WORKSPACE`, `AUTODUB_INPUT_DIR`,
  `AUTODUB_OUTPUT_DIR`, `AUTODUB_MODEL_CACHE`, or `AUTODUB_SHARED_ASSETS_DIR`
  to customize locations. Set `AUTODUB_SHARED_WORKSPACE_DIR` only when V2 is
  intentionally meant to read V1's shared history/voice assets. An existing
  repository-level `models/` directory is preserved as the model-cache default
  so existing weights are not silently stranded by the path migration.
- `.env.cpu.example` and `.env.gpu.example` are small resource overlays. They do
  not change ASR, OCR, translation, separation, or voice model selections.

## Dependency profiles

- `requirements.txt`: normal bot/runtime dependencies.
- `requirements-dev.txt`: lint tools and lightweight focused-test dependencies;
  it does not install model weights or the entire production stack.
- `requirements-models.txt`: convenience entrypoint to the existing isolated
  `backend/requirements-models.txt` profile. Keep heavyweight model packages in
  `backend/model_venv`.
- Install the PyTorch CPU or CUDA wheel separately using the command matching
  the installed driver from the official PyTorch selector; do not let pip replace
  the machine's CUDA build implicitly.

## Staged module migration

1. **Foundation (this change):** central paths, focused tests, dependency
   profiles, and a lint/unit/smoke CI workflow.
2. **Integration adapters:** extract downloader, OCR, ASR, translation and TTS
   adapters behind stable interfaces while keeping their current entry modules
   as shims. Add fallback tests before moving each adapter.
3. **Application services:** move queue/workflow/QC rules into `services/`; keep
   Telegram and HTTP handlers as thin callers.
4. **API composition:** split the large API/dashboard modules into `app/routers/`
   and leave startup/lifespan wiring in the composition root.
5. **Pipeline cleanup:** retain `pipeline_v2/` as the canonical `pipelines`
   boundary unless a later change proves that renaming it adds value; test resume
   manifests and artifact compatibility before any storage/schema migration.

## Focused checks

```powershell
python -m pip install -r requirements-dev.txt
ruff check backend/config tests/test_app_paths.py tests/test_pipeline_v2_config.py scripts/smoke_import_v2.py
python -m unittest tests.test_app_paths tests.test_pipeline_v2_config tests.test_refactored_boundaries tests.test_pipeline_v2_download tests.test_ocr_subtitle_locator.ChineseSubtitleLocatorTests
python scripts/smoke_import_v2.py
```

The end-to-end render smoke test remains a separate, opt-in check because it
requires a source video, model runtimes, API credentials, FFmpeg and (for the
production GPU profile) a compatible NVIDIA driver.
