# Biến môi trường mà backend đọc

File này được sinh tự động bởi `python scripts/gen_env_docs.py`. Không sửa tay;
`tests/test_env_var_docs.py` báo lỗi khi code đọc biến mới mà file chưa được sinh lại.

Cột *Mặc định* là giá trị viết trong code. Nhiều giá trị nghĩa là các module dùng
mặc định khác nhau cho cùng một biến; nên xem lại những dòng đó.

Tổng: 141 biến.

| Biến | Mặc định | Nơi đọc |
|---|---|---|
| `AUTODUB_ALLOWED_VIDEO_ROOTS` | — | `backend/script_api.py` |
| `AUTODUB_ALLOW_REMOTE_CLOUD_NODE` | — | `run_cloud_node.py` |
| `AUTODUB_ALLOW_TEST_PROGRESS_FILE` | — | `backend/cloud/gpu_scripts/full_pipeline.py` |
| `AUTODUB_CLOUD_ALLOWED_ROOTS` | — | `backend/cloud/cloud_security.py` |
| `AUTODUB_CLOUD_BILLING_ROUNDING_QUANTUM_CU` | `0.02` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_BUDGET_TIMEZONE` | `Asia/Ho_Chi_Minh` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_CU_BUDGET` | `10.0` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_DEFAULT_GPU` | `T4` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_DOWNLOAD_TIMEOUT` | `1800` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_EXEC_TIMEOUT` | `7200` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_MAX_UPLOAD_BYTES` | `(tính lúc chạy)` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_MIN_DISK_MB` | `4096` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_MIN_PROVIDER_BALANCE_CU` | `0.0` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_OUTPUT_DIR` | — | `backend/cloud/cloud_config.py`, `backend/cloud/cloud_orchestrator.py`, `run_cloud_node.py` |
| `AUTODUB_CLOUD_PROVIDER_BALANCE_STALE_SECONDS` | `300` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_RUNTIME_BUDGET_CHECK_INTERVAL_SECONDS` | `15` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_SESSION_TIMEOUT` | `7200` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_SETUP_TIMEOUT` | `600` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_STOP_LATENCY_BUFFER_SECONDS` | `60` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_STOP_TIMEOUT` | `60` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_UPLOAD_TIMEOUT` | `1800` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_WARM_HANDOFF_SECONDS` | `180` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_WARM_KEEPALIVE_SECONDS` | `0` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_WSL_DISTRO` | `Ubuntu` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CLOUD_WSL_USER` | `root` | `backend/cloud/cloud_config.py` |
| `AUTODUB_CONTROL_DIR` | `(tính lúc chạy)` | `backend/tool_control_runtime.py` |
| `AUTODUB_CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088` | `backend/main.py` |
| `AUTODUB_DISABLE_STOP_CHECK` | `0` | `backend/shared_state.py`, `backend/social_downloader.py` |
| `AUTODUB_INPUT_DIR` | `D:\video_input` | `backend/batch_processor.py`, `backend/main.py`, `backend/telegram_bot.py` |
| `AUTODUB_LOCAL_TOKEN` | — | `backend/cloud/cloud_security.py`, `backend/main.py`, `backend/script_api.py`, `backend/telegram_script_bot.py` |
| `AUTODUB_LOG_FILE` | — | `backend/main.py` |
| `AUTODUB_MIN_FREE_GB` | `(tính lúc chạy)` | `backend/disk_guard.py` |
| `AUTODUB_OPS_AUTOHEAL` | `0` | `backend/ops_watchdog.py` |
| `AUTODUB_OUTPUT_DIR` | `D:\banve` | `backend/batch_processor.py`, `backend/cloud/cloud_config.py`, `backend/cloud/cloud_orchestrator.py`, `backend/cloud/cloud_security.py` (+3) |
| `AUTODUB_PORT` | `8088` | `backend/main.py` |
| `AUTODUB_TELEGRAM_UPLOAD_VIDEO` | `false` | `backend/telegram_bot.py` |
| `AUTODUB_WORKSPACE` | `(tính lúc chạy)` | `backend/ai/doodle_engine.py`, `backend/ai/script_tts_service.py`, `backend/ai/translation.py`, `backend/ai/v1_auto_voice.py` (+33) |
| `BOT_TOKEN` | — | `backend/telegram_script_bot.py` |
| `CAPCUT_API_DIR` | — | `backend/ai/script_tts_service.py` |
| `CLOUD_BOT_ALLOWED_USER_IDS` | — | `backend/cloud/cloud_telegram_bot.py` |
| `CLOUD_BOT_TOKEN` | — | `backend/cloud/cloud_security.py` |
| `CLOUD_PORT` | `8089` | `run_cloud_node.py` |
| `CLOUD_TTS_CONCURRENCY` | `5` | `backend/cloud/gpu_scripts/full_pipeline.py` |
| `CRAFT_MODEL_PATH` | `(tính lúc chạy)` | `backend/whiteboard_animator/craft_detector.py` |
| `DEEPSEEK_API_KEY` | — | `backend/ai/translation.py`, `backend/pipeline_v2/video_pipeline.py`, `backend/telegram_bot.py` |
| `DOWNLOADER_BOT_TOKEN` | — | `telebot_video_downloader.py` |
| `DOWNLOADER_SEND_VIDEO_TO_CHAT` | `false` | `telebot_video_downloader.py` |
| `ENABLE_SMART_SKIP_OCR` | `true` | `backend/ocr_utils.py` |
| `ENABLE_VIDEO_ROUTER` | `true` | `backend/v1_video_router.py` |
| `FFMPEG_BINARY` | — | `backend/handdraw/render.py` |
| `FPT_API_KEY` | — | `backend/ai/v1_gemini_dispatcher.py`, `backend/cloud/cloud_orchestrator.py`, `backend/model_workers/v1_voice_worker.py`, `backend/v1_orchestrator.py` |
| `GEMINI_API_KEY` | — | `backend/ai/scriptwriting.py`, `backend/ai/translation.py`, `backend/ai/v1_gemini_dispatcher.py`, `backend/ai/voice_cloning.py` (+14) |
| `GEMINI_API_KEYS` | — | `backend/ai/translation.py`, `backend/ai/v1_gemini_dispatcher.py`, `backend/cloud/cloud_orchestrator.py`, `backend/cloud/gpu_scripts/caption_reviewer.py` (+1) |
| `GEMINI_HOOK_MODEL` | `(tính lúc chạy)` | `backend/ai/scriptwriting.py` |
| `GEMINI_MAX_RETRIES` | `4` | `backend/whiteboard_animator/detect_regions.py` |
| `GEMINI_MODEL` | `gemini-3-flash-preview` / `gemini-3.7-flash` / `gemini-3.8-flash` | `backend/ai/scriptwriting.py`, `backend/batch_processor.py`, `backend/job_tracker.py`, `backend/main.py` |
| `GEMINI_RETRY_DELAY_SEC` | `3` | `backend/whiteboard_animator/detect_regions.py` |
| `GEMINI_REWRITE_MODEL` | `(tính lúc chạy)` | `backend/ai/scriptwriting.py` |
| `GEMINI_SCRIPT_MODEL` | `(tính lúc chạy)` | `backend/ai/scriptwriting.py` |
| `GEMINI_VISION_MODEL` | `(tính lúc chạy)` | `backend/ai/scriptwriting.py` |
| `GOOGLE_API_KEY` | — | `backend/whiteboard_animator/cli.py` |
| `LLM_PROVIDER` | `auto` | `backend/ai/translation.py`, `backend/pipeline_v2/video_pipeline.py`, `backend/telegram_bot.py` |
| `LOCALAPPDATA` | — | `backend/subtitle_layout.py` |
| `META_API_BASE_URL` | `(tính lúc chạy)` | `backend/ai/muse_spark_service.py` |
| `META_API_KEY` | — | `backend/ai/muse_spark_service.py` |
| `MUSE_MODEL` | `muse-spark-1.3-contributor` | `backend/ai/muse_spark_service.py` |
| `OCR_TRACK_INTERVAL` | `0.2` | `backend/ocr_utils.py` |
| `OPENAI_API_KEY` | — | `backend/ai/translation.py`, `backend/pipeline_v2/video_pipeline.py`, `backend/telegram_bot.py` |
| `OPENROUTER_API_KEY` | — | `backend/ai/v1_free_gpt.py` |
| `PYTEST_CURRENT_TEST` | — | `backend/ai/v1_asr_isolated.py`, `backend/cloud/cloud_orchestrator.py`, `backend/cloud/gpu_scripts/full_pipeline.py`, `backend/job_tracker.py` (+3) |
| `QC_GATE_POLICY` | `block` | `backend/pipeline_v2/qc.py` |
| `SAVE_DIRECTORY` | — | `telebot_video_downloader.py` |
| `SCRIPT_MODE` | `default` | `backend/ai/translation.py`, `backend/ai/voice_cloning.py`, `backend/batch_processor.py`, `backend/cloud/cloud_orchestrator.py` (+1) |
| `SCRIPT_OUTPUT_COPY_DIR` | — | `backend/ai/doodle_engine.py` |
| `SCRIPT_REMOTION_ENABLED` | `0` | `backend/ai/remotion_renderer.py` |
| `SCRIPT_SATORI_BAND_A` | `0` | `backend/ai/doodle_engine.py`, `backend/ai/satori_renderer.py`, `backend/ai/whiteboard_hand_engine.py` |
| `SO9_RESOLVER_TIMEOUT` | `50` | `backend/social_downloader.py` |
| `SOCIAL_DOWNLOAD_TIMEOUT_SECONDS` | `120` | `backend/social_downloader.py` |
| `SOCIAL_STREAM_TIMEOUT_SECONDS` | `180` | `backend/social_downloader.py` |
| `SUBEXTRACT_JOBS_DIR` | `(tính lúc chạy)` | `backend/handoff_api.py` |
| `SystemRoot` | `C:\Windows` | `backend/ops_watchdog.py`, `backend/v1_shutdown.py` |
| `TELEGRAM_BOT_TOKEN` | — | `backend/notification_service.py` |
| `TELEGRAM_SCRIPT_AI_ENGINE` | `muse` | `backend/telegram_script_bot.py` |
| `TELEGRAM_SCRIPT_ALLOWED_CHAT_IDS` | — | `backend/telegram_script_bot.py` |
| `TELEGRAM_SCRIPT_API_BASE` | `http://127.0.0.1:8088` | `backend/telegram_script_bot.py` |
| `TELEGRAM_SCRIPT_BOT_TOKEN` | — | `backend/telegram_script_bot.py` |
| `TOOL_V1_CONTROL_DIR` | `(tính lúc chạy)` | `backend/tool_control_runtime.py` |
| `TOOL_V2_RENDER_HISTORY` | `(tính lúc chạy)` | `backend/render_history.py` |
| `TTS_CONCURRENCY` | `3` | `backend/pipeline_v2/tts.py` |
| `V1_ALLOW_EMERGENCY_SPEED` | `True` | `backend/ai/voice_cloning.py` |
| `V1_ALLOW_TRANSLATION_FALLBACK` | `false` | `backend/ai/translation.py` |
| `V1_ASR_CHUNKING` | `true` | `backend/ai/transcription.py`, `backend/main.py` |
| `V1_ASR_JOB_OPTIONS` | `{}` | `backend/model_workers/v1_asr_worker.py` |
| `V1_ASR_MODEL` | — | `backend/ai/v1_sensevoice_adapter.py` |
| `V1_ASR_PARALLEL_SENSEVOICE` | `1` | `backend/ai/transcription.py` |
| `V1_ASR_REUSE` | `1` | `backend/ai/v1_asr_cache.py` |
| `V1_ASYNC_PROGRESS_NOTIFY` | `1` | `backend/v1_orchestrator.py` |
| `V1_BLUR_CPU_INPLACE` | `True` | `backend/v1_gpu_subtitle_blur.py` |
| `V1_CAPCUT_WORKERS` | `6` | `backend/ai/voice_cloning.py` |
| `V1_DELOGO_MODE` | — | `backend/cloud/cloud_orchestrator.py`, `backend/v1_orchestrator.py` |
| `V1_EDGE_WORKERS` | `4` | `backend/ai/voice_cloning.py` |
| `V1_GEMINI_VISION_DOMAIN_CLASSIFY` | `0` | `backend/ai/translation.py` |
| `V1_GPU_LOCALIZED_BLUR` | `True` | `backend/v1_gpu_subtitle_blur.py` |
| `V1_HANDOFF_ALLOWED_DIRS` | — | `backend/handoff_api.py` |
| `V1_MAX_NATURAL_SPEED` | `(tính lúc chạy)` | `backend/v1_speech_guard.py` |
| `V1_MAX_VIDEO_DURATION_MINUTES` | `60.0` | `backend/v1_video_router.py` |
| `V1_MEDIUM_MAX_SECONDS` | `420.0` | `backend/v1_video_router.py` |
| `V1_METRICS_VIDEO_ID` | `untracked` | `backend/v1_resource_control.py` |
| `V1_NVENC_PRESET` | `p3` | `backend/video_utils.py` |
| `V1_OCR_KEEP_WARM` | `1` | `backend/ocr_utils.py` |
| `V1_OCR_KEEP_WARM_MIN_RAM_MB` | `1500` | `backend/ocr_utils.py` |
| `V1_OCR_KEEP_WARM_MIN_VRAM_MB` | `1800` | `backend/ocr_utils.py` |
| `V1_OCR_MAX_SAMPLES_PER_SEGMENT` | `8` | `backend/ocr_utils.py` |
| `V1_OCR_ROI_DIFF_SKIP` | `1` | `backend/ocr_utils.py` |
| `V1_OCR_SOURCE_MIN_GAP_FRAMES` | `2` | `backend/ocr_utils.py` |
| `V1_OCR_TIMELINE_FAST_REC` | `1` | `backend/ocr_utils.py` |
| `V1_OCR_TIMEOUT_SECONDS` | `900` | `backend/ocr_utils.py` |
| `V1_OCR_TRACK_INTERVAL` | `0.55` | `backend/ocr_utils.py` |
| `V1_OCR_VISUAL_BATCH_SIZE` | `8` | `backend/ocr_utils.py` |
| `V1_PADDLE_DETECTION_MODEL` | `PP-OCRv6_tiny_det` | `backend/job_tracker.py` |
| `V1_PADDLE_RECOGNITION_MODEL` | — | `backend/job_tracker.py` |
| `V1_PARAFORMER_CO_ASR` | `selective` | `backend/ai/transcription.py` |
| `V1_QC_POLICY` | `(tính lúc chạy)` / `OFF` | `backend/main.py`, `backend/v1_quality_gate.py` |
| `V1_RESUME_MANIFEST_SCAN_LIMIT` | `50` | `backend/v1_orchestrator.py` |
| `V1_ROFORMER_BATCH_SIZE` | `0` | `backend/model_workers/v1_separator_worker.py` |
| `V1_ROFORMER_OVERLAP` | `2` | `backend/model_workers/v1_separator_worker.py` |
| `V1_SCRIPT_MODE` | `default` | `backend/model_workers/v1_voice_worker.py` |
| `V1_SEPARATOR_BACKEND` | — | `backend/video_utils.py` |
| `V1_SEPARATOR_KEEP_WARM` | `(tính lúc chạy)` | `backend/video_utils.py` |
| `V1_SEPARATOR_KEEP_WARM_MIN_RAM_MB` | `2000` | `backend/ai/v1_separator_session.py` |
| `V1_SHORT_MAX_SECONDS` | `420.0` | `backend/ai/transcription.py`, `backend/ocr_utils.py`, `backend/v1_orchestrator.py`, `backend/v1_video_router.py` |
| `V1_SMART_SKIP_OCR` | `(tính lúc chạy)` / `true` | `backend/main.py`, `backend/ocr_utils.py` |
| `V1_STRICT_OCR_GEOMETRY` | `1` | `backend/v1_ocr_geometry.py` |
| `V1_SUBTITLE_COVER_STYLE` | — | `backend/ass_utils.py`, `backend/cloud/cloud_orchestrator.py`, `backend/cloud/gpu_scripts/full_pipeline.py`, `backend/job_config_service.py` (+2) |
| `V1_TRANSLATION_MEANING_POLICY` | `warn` | `backend/ai/v1_translation_meaning.py` |
| `V1_TTS_WORKERS` | `6` | `backend/ai/voice_cloning.py`, `backend/main.py` |
| `V1_VISUAL_OCR_MAX_SAMPLES` | `20000` | `backend/ocr_utils.py` |
| `V1_VOICE_DIALOGUE_MODE` | — | `backend/ai/v1_auto_voice.py` |
| `V1_WHISPER_BEAM_SIZE` | `2` | `backend/ai/transcription.py` |
| `VOICE_API_KEY` | — | `backend/ai/v1_gemini_dispatcher.py`, `backend/cloud/cloud_orchestrator.py`, `backend/model_workers/v1_voice_worker.py`, `backend/v1_orchestrator.py` |
| `WINDIR` | `C:/Windows` / `C:\Windows` | `backend/ai/satori_renderer.py`, `backend/cloud/cloud_orchestrator.py`, `backend/subtitle_layout.py` |
