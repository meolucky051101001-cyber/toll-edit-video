# Tool V1 backend: layout and conventions

Tool V2 was retired; this document describes the active Tool V1 backend only.

## Entry points

| Responsibility | Location |
| --- | --- |
| Dashboard + Workflow API (port 8088) | `backend/main.py` |
| Voice control plane (port 8090) | `backend/tool_control.py` |
| Supervisor for dashboard/bot services | `backend/background_service.py` |
| Telegram AutoDub bot | `backend/telegram_bot.py` |
| Telegram Studio Kịch Bản bot | `backend/telegram_script_bot.py` |
| Batch processing | `backend/batch_processor.py` |

`backend/workflow_api.py` serves `/quy-trinh`; `backend/script_api.py` serves
`/kich-ban`; phase routers (`phase_a_routes.py` … `phase_d_routes.py`) add
presets, voice preview, segment editing and stability endpoints.

## Module boundaries

| Responsibility | Location | Direction |
| --- | --- | --- |
| HTTP composition | `backend/main.py` + API routers | Keep startup/lifespan wiring in the composition root; extract routers incrementally |
| Business services | `backend/*_service.py`, `backend/telegram_jobs.py` | Move reusable rules into services with import shims |
| Media pipeline | `backend/pipeline_v2/` | Canonical pipeline package; do not duplicate it elsewhere |
| AI/model integrations | `backend/ai/`, `backend/model_workers/`, `backend/social_downloader.py` | Put provider adapters behind stable interfaces |
| Shared configuration | `backend/environment.py`, `backend/config/paths.py`, `backend/pipeline_v2/config.py` | `backend/config/` owns shared path resolution |

Moving modules in bulk would risk Telegram, the HTTP API, batch mode and resume
checkpoints. Change one boundary at a time and keep compatibility imports until
callers are migrated.

## Path and environment rules

- Process environment variables take precedence over `backend/.env`.
- Absolute paths are used as-is; relative values resolve from the repository
  root, not from the caller's current working directory.
- Main runtime data uses `AUTODUB_WORKSPACE`; input/output defaults come from
  `AUTODUB_INPUT_DIR` / `AUTODUB_OUTPUT_DIR` and the dashboard settings.
- Do not point production at test or benchmark directories.

## Dependency profiles

- `requirements.txt`: normal bot/runtime dependencies for `backend/venv`. It must
  not contain `paddleocr`; OCR runs in the isolated runtime.
- `requirements-dev.txt`: lint tools and lightweight test dependencies.
- `backend/requirements-v1-models.txt`: PP-OCRv6 runtime for `backend/model_venv`
  (`paddleocr==3.7.0`, `onnxruntime-gpu==1.26.0`).
- `backend/requirements-models.txt`: heavier model profile for `backend/model_venv`.
- Install the PyTorch CPU/CUDA wheel separately; do not let pip replace the
  machine's CUDA build implicitly.

## Focused checks

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q --tb=short -p no:cacheprovider tests/test_v1_checkpoint_orchestrator.py tests/test_v1_ocr_geometry_repair.py tests/test_v1_quality_gate.py tests/test_dashboard_hardening.py
```

The end-to-end render smoke test is opt-in: it needs a source video, model
runtimes, API credentials, FFmpeg and (for the production profile) an NVIDIA GPU.
