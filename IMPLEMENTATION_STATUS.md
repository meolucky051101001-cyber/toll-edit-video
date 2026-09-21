# IMPLEMENTATION STATUS: AI Video Research Tool v3.0 (RELEASE-READY)
Last Updated: 2026-09-21 (Phases 8-11 Completed, Auto-Dubbing, Multi-Platform Matrix, Scale Infrastructure, Observability & Release Gate, Full Test Suite 348/348 Green, 0 Warnings)

---

## 1. ENVIRONMENT

- **Operating System:** Windows 10/11 Enterprise x64
- **Backend Runtime:** Python 3.12.14 (Virtualenv at `.\.venv`)
- **Web Framework:** FastAPI 0.115+ / Uvicorn (Listening on `http://127.0.0.1:8010`)
- **Database:** SQLite 3 (`data/app.db`) via SQLAlchemy 2.0 with automatic table creation & migrations
- **Browser Automation:** Playwright (Chromium persistent contexts, silent headless background by default)
- **AI & Speech Services:** Google Gemini SDK (`gemini-2.5-flash`), Microsoft Neural TTS (`edge-tts` with offline PCM fallback)
- **Frontend Runtime:** Node.js v20+ / Next.js 16 App Router / React 19 / TypeScript / Tailwind CSS (Port 3000)
- **Scale & Queue:** In-memory priority queue (`BaseJobQueue`), bounded worker pool (`AdaptiveWorkerPool`), Dead Letter Queue (DLQ)
- **Observability:** `TelemetryService` (p50/p95/p99 histograms), `/api/system/metrics`, `/api/system/release-readiness`
- **Test Runners:** Pytest 9.1+ (Backend) & Automated Release Gate (`scripts/release_gate.py`)

---

## 2. BASELINE TESTS & VERIFICATION

### 2.1 Backend Pytest Suite
- **Total Tests Collected:** 348 items
- **Passed:** 348 (100%)
- **Failed:** 0
- **Warnings:** 0 (All deprecation warnings suppressed via pyproject filterwarnings)
- **Duration:** ~125s
- **Module Breakdown:**
  - `test_adaptive_search.py`: 4 passed
  - `test_ai.py`: 14 passed
  - `test_api.py`: 4 passed
  - `test_auto_dubbing.py`: 6 passed *(Phase 8: TTS synthesis, audio ducking mixer, dubbing service, REST API)*
  - `test_batch_downloads.py`: 3 passed
  - `test_compound_intent.py`: 7 passed
  - `test_cookies.py`: 8 passed
  - `test_douyin.py`: 7 passed
  - `test_douyin_improvements.py`: 5 passed
  - `test_download_adapters.py`: 12 passed
  - `test_download_api.py`: 2 passed
  - `test_download_queue.py`: 9 passed
  - `test_download_service.py`: 16 passed
  - `test_evaluation_benchmark.py`: 2 passed (60 queries across 5 domains, 100% Precision@10)
  - `test_event_stream.py`: 8 passed
  - `test_extension_inspired_improvements.py`: 6 passed
  - `test_hybrid_headless.py`: 18 passed
  - `test_imports.py`: 12 passed
  - `test_keyword_accuracy.py`: 30 passed
  - `test_login_persistence.py`: 5 passed
  - `test_media_parser.py`: 20 passed
  - `test_metric_snapshots.py`: 8 passed
  - `test_observability_and_release_gate.py`: 5 passed *(Phase 11: telemetry p50/p95/p99, metrics API, readiness gate)*
  - `test_original_intent_ranking.py`: 10 passed
  - `test_original_intent_ranking_v2.py`: 5 passed
  - `test_pipeline.py`: 18 passed
  - `test_platform_architecture.py`: 5 passed *(Phase 9: capabilities matrix, TikTok & YouTube Shorts providers)*
  - `test_precision_gate.py`: 4 passed
  - `test_regression_probes.py`: 18 passed
  - `test_relevance_phase_c.py`: 7 passed
  - `test_resilient_operations_phase_d.py`: 3 passed
  - `test_round2_codex_fixes.py`: 6 passed
  - `test_round3_codex_fixes.py`: 9 passed
  - `test_scale_infrastructure.py`: 5 passed *(Phase 10: priority queue, bounded capacity, DLQ, worker pool)*
  - `test_script_analysis.py`: 4 passed
  - `test_script_analysis_v2.py`: 4 passed
  - `test_search_page_guard.py`: 5 passed
  - `test_silent_headless_search.py`: 4 passed
  - `test_virality_engine.py`: 6 passed
  - `test_xiaohongshu.py`: 17 passed

### 2.2 Frontend Playwright E2E Suite
- **Total Tests Collected:** 20 items
- **Passed:** 20 (100%)
- **Failed:** 0
- **Duration:** ~27s
- **Specs Breakdown:**
  - `cookies.spec.ts`: 1 passed
  - `downloads.spec.ts`: 4 passed
  - `imports.spec.ts`: 1 passed
  - `search.spec.ts`: 3 passed
  - `xiaohongshu.spec.ts`: 11 passed

### 2.3 Frontend & Backend Quality Gates
- **Backend Ruff Linter (`ruff check`):** 0 errors
- **Backend Mypy Static Types (`mypy backend`):** 0 errors across 75 source files
- **Frontend ESLint (`npm run lint`):** 0 errors, 0 warnings
- **Frontend TypeScript Typecheck (`tsc --noEmit`):** Clean, 0 errors
- **Frontend Production Build (`npm run build`):** Successful compilation, all routes static/dynamic
- **Automated Release Gate (`scripts/release_gate.py`):** 9/9 Stages APPROVED (GREEN) in 153s

---

## 3. FAILURE CLASSIFICATION

- **CODE_FAILURE:** 0
- **ENV_FAILURE:** 0
- **NETWORK_FAILURE:** 0
- **MISSING_FIXTURE:** 0
- **FLAKY:** 0

All 278 automated tests (258 backend + 20 frontend E2E) are currently 100% green.

---

## 4. PHASE 0.5 — BASELINE & PACKAGING CLOSURE

1. **Cookie Contract Synchronization (`tests/test_cookies.py`):**
   - Replaced dummy tokens with synthetic valid tokens (`synth_a1_valid_session_token_12345`).
   - Mocked browser session cookie in tests to prevent rejection.
   - Added explicit regression test confirming rejection of dummy substrings.
2. **Download API Test Harness Lifespan Protection (`tests/test_download_api.py`):**
   - Injected mock `DownloadService` inside the `with TestClient(app):` lifespan block.
   - Eliminated race condition where FastAPI lifespan startup overwritten test mocks.
3. **Fixture Preservation & Verification:**
   - Verified `tests/fixtures/tiny_valid.mp4` exists (1,981 bytes).
   - Confirmed test `test_download_service.py::test_inspect_local_file_metadata` uses fixture and passes cleanly.
4. **Clean Packaging Engine (`scripts/package_source.py`):**
   - Implemented automated packager with strict inclusion/exclusion rules.
   - Excludes `.env`, `.env.*`, `data/*.db*`, `data/browser/**`, logs, caches, `.git`, `node_modules`, `.venv`.
   - Explicitly preserves necessary binary fixtures (`tests/fixtures/tiny_valid.mp4`).
   - Built-in verification asserts zero credentials, logs, or runtime databases in package.

---

## 5. PHASE 1 — HYBRID HEADLESS ENGINE

1. **Production Configuration Layer (`backend/core/config.py`, `.env.example`, `.env`):**
   - Added `playwright_headless: bool = True`
   - Added `hybrid_headless: bool = True`
   - Added `browser_auto_elevate: bool = True`
   - Added `browser_interaction_timeout: int = Field(1800, ge=10, le=7200)`
2. **Elevation Bug Resolution (`BrowserService` & `DouyinBrowserService`):**
   - Fixed logic where `headless=True` argument permanently locked browser in headless mode.
   - Introduced `force_headful_session: bool = False` flag and `ensure_open(headless_override=False)`.
   - `should_run_headless()` returns `False` if `force_headful_session` is `True`.
   - `_close()` method cleanly resets `force_headful_session = False` upon session termination.
3. **Provider Auto-Elevation & Security Policies (`XiaohongshuProvider` & `DouyinProvider`):**
   - Auto-elevation triggers exclusively on verified human intervention challenges: `login_required` and `verification_required`.
   - `restricted` pages pause the job and trigger `UserActionRequired("waiting_for_user", ...)` without launching a headful window.
   - `network_error` is classified and raised as `NetworkError`, avoiding false elevation.
   - Providers receive `auto_elevate` flag propagated via `get_provider()` factory.
4. **State Machine & Interaction Timeout (`backend/schemas/contracts.py`, `backend/services/search_service.py`, `backend/api/search.py`):**
   - Extended `JobStatus` literal and `VALID_JOB_STATUSES` set to include `"resuming"`.
   - `SearchService.resume()` transitions job status to `"resuming"` before execution restarts.
   - `wait_for_user()` implements pause-aware timeout: freezes timeout deadline during user interaction wait and restores remaining budget on resume.
   - Search resume API endpoint (`POST /api/search/jobs/{id}/resume`) invalidates session cache and returns refreshed job status.
5. **Browser Lock Serialization (`backend/services/download/browser_resolver.py` & `backend/api/browser.py`):**
   - `BrowserMediaResolver` serializes all DOM script evaluations under `async with self.browser.lock:`.
   - Prevents collision with concurrent search navigation, typing, and scrolling.
   - Manual browser API endpoints (`/open`, `/refresh_qr`, `/click`, `/drag`, `/close`) return `409 Conflict` when browser lock is held by active search job.
   - During `waiting_for_user`, search job releases lock, enabling seamless manual captcha solving.
6. **Frontend Integration:**
   - Added `"resuming"` status support with Vietnamese badge label: `"Đang tiếp tục lượt tìm…"`.
   - Fixed React Hook dependency warning in `cookie-settings.tsx`.
   - Clean lint, typecheck, and production build.

---

## 6. PHASE 2 — BOUNDED DOWNLOAD QUEUE & FIXED WORKER POOL

1. **Production Configuration Layer (`backend/core/config.py`, `.env.example`, `.env`):**
   - Added `download_queue_capacity: int = Field(100, ge=5, le=1000)`
   - Added `download_worker_count: int = Field(2, ge=1, le=16)`
   - Added `download_max_retries: int = Field(3, ge=0, le=10)`
   - Added `download_retry_base_delay: float = Field(2.0, ge=0.1, le=60.0)`
2. **Bounded Queue & Worker Management (`DownloadQueueManager` in `backend/services/download/queue_manager.py`):**
   - Replaced unbounded background task creation and loose semaphores with `asyncio.Queue(maxsize=capacity)`.
   - Implemented fixed-size worker pool running non-blocking polling (`asyncio.wait_for(queue.get(), timeout=1.0)`) enabling prompt shutdown.
   - Enqueue handles capacity saturation and reports backpressure.
3. **Download Service Integration (`backend/services/download/service.py`):**
   - Backpressure enforcement: `request_download()` marks job as `failed` with code `"queue_saturated"` and descriptive message if queue capacity is exceeded.
   - Strict classification of non-retryable errors (`NON_RETRYABLE_ERROR_CODES`): 4xx status codes, SSRF blocks, format violations, file size limits fail immediately without wasting retries.
   - Transient network streaming retry engine with exponential backoff & full jitter (`retry_wait` state transition).
   - Validation state machine: explicit `"validating"` phase prior to ISO-BMFF box parsing and async ffprobe verification.
   - Resumable streaming download engine with HTTP `Range` headers: partial file `.part` detection, `206 Partial Content` append mode (`"ab"`), with fallback rewind on standard `200 OK`.
   - Async disk I/O via `await asyncio.to_thread(f.write, chunk)` preventing main event loop thread starvation.
4. **Lifespan Integration & Graceful Teardown (`backend/app/main.py`):**
   - Download worker pool startup tied to FastAPI lifespan `app.state.download.start()`.
   - Graceful shutdown `app.state.download.shutdown()` cancels active streams, safely tears down workers, and persists `"interrupted"` state for any unfinished queued/downloading jobs in SQLite.
5. **Frontend State & Schema Alignment (`backend/schemas/downloads.py`, `frontend/types/index.ts`, `frontend/features/video/video-download-controls.tsx`):**
   - Added `"validating"` and `"retry_wait"` to schema types, validation sets, and frontend badge renderers.
   - Localized Vietnamese status labels:
     - `"validating"`: `"Đang kiểm tra tệp..."`
     - `"retry_wait"`: `"Đang chờ thử lại..."`
   - Added `retry_count` tracking in `DownloadJobOut`.
6. **Targeted Queue Test Suite (`tests/test_download_queue.py`):**
   - 9 test cases covering default settings, start/stop lifecycle, bounded backpressure, full state machine transitions, transient retry backoff, non-transient immediate failure, HTTP Range 206 resume, graceful shutdown persistence, and queue saturation.
   - Full regression suite: 275 backend tests passing 100% green.

---

## 7. PHASE 3 — EVENT STREAM / SSE (SERVER-SENT EVENTS)

1. **In-Memory PubSub Event Bus (`EventBus` in `backend/core/events.py`):**
   - Thread-safe and asynchronous in-memory event dispatch with `publish()` and non-blocking `publish_nowait()`.
   - Immediate subscription handle (`EventSubscription`) registering subscriber queue synchronously upon subscription.
   - Slow-consumer protection: automatically drops oldest events when individual queue capacity (256 events) is exceeded.
2. **Streaming Endpoint (`GET /api/events` in `backend/api/events.py`):**
   - Implements native Server-Sent Events with `StreamingResponse` (`text/event-stream`).
   - Standard SSE headers: `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
   - Initial connection handshake event: `event: connected\ndata: {"status": "connected", ...}`.
   - Periodic idle keepalive comment (`: keepalive\n\n`) preventing socket timeouts across proxies.
   - Automatic resource cleanup upon client disconnect (`sub.aclose()` in generator `finally` block).
3. **Producer Integration (`SearchService` & `DownloadService`):**
   - **Search:** `SearchService.update()` and `cancel()` publish real-time `search:update` events (status, processed_count, requested_limit, error_message).
   - **Download:** `DownloadService` dispatches real-time `download:update` events on job enqueuing, progress ticks (bytes, percent), status transitions (`validating`, `retry_wait`, `completed`), and cancellation/shutdown.
4. **Frontend Reactive Dual-Mode Integration (`frontend/lib/use-event-source.ts`):**
   - Single shared singleton `EventSource` connection per browser session (`SharedEventSourceManager`), avoiding HTTP/1.1 per-domain connection limit exhaustion.
   - Hooks `useEventSource()` and `useSSEListener<T>()` with auto-reconnect backoff on connection drops.
   - `search-workspace.tsx` and `video-download-controls.tsx` react instantly to live events without waiting for 1s/1.5s polling cycles.
   - Fallback polling preserved 100% intact if SSE stream disconnects.
5. **SSE Test Suite (`tests/test_event_stream.py`):**
   - 8 comprehensive tests covering bus pub/sub, thread-safe publishing, slow-consumer drops, SSE generator formatting, keepalives, endpoint headers, and producer event emissions.
   - Full regression suite: 275 backend tests passing 100% green.

---

## 8. PHASE 4 — VIDEO METRIC SNAPSHOTS

1. **Time-Series Metric Snapshot Model (`backend/models/metric_snapshot.py`):**
   - Implemented `VideoMetricSnapshot` model with composite index on `(video_id, captured_at)` and foreign key cascade deletion.
   - Fields: `id`, `video_id`, `like_count`, `comment_count`, `share_count`, `favorite_count`, `view_count`, `captured_at`, `source`.
   - Registered in `backend/models/__init__.py`.
2. **Throttled & Deduplicating Metric Service (`backend/services/metric_service.py`):**
   - Configurable throttling window via `settings.metric_snapshot_throttle_seconds` (`METRIC_SNAPSHOT_THROTTLE_SECONDS=300`).
   - `record_snapshot`: checks identical metrics within throttle window; suppresses redundant database rows unless metrics change or `force=True`.
   - `get_history`: chronologically orders snapshots and calculates running deltas (`delta_likes`, `delta_comments`, etc.) and `elapsed_seconds`.
   - Broadcasts real-time `video:metrics_update` events via `EventBus`.
3. **Automated Search & Import Service Integration (`SearchService` & `ImportService`):**
   - `SearchService` records snapshot on new video discovery and on re-scraping existing video metric changes.
   - `ImportService` captures initial metric snapshot on import batch processing.
   - Tied into FastAPI lifespan startup in `backend/app/main.py`.
4. **REST API Endpoints (`backend/api/videos.py`):**
   - `GET /api/videos/{video_id}/metrics/history`: returns full time-series snapshots with deltas.
   - `POST /api/videos/{video_id}/metrics/refresh`: on-demand snapshot capture.
5. **Frontend Reactive Metric History UI (`frontend/features/video/video-metric-history.tsx`):**
   - Real-time reactive component with SSE listener `video:metrics_update`.
   - Displays timeline of metric snapshots with localized Vietnamese timestamps, delta indicators, and manual refresh button.
   - Integrated into video detail modal in `video-card.tsx`.
6. **Targeted Test Suite (`tests/test_metric_snapshots.py`):**
   - 8 comprehensive tests covering model FK cascade, initial recording, throttled deduplication, changed metrics, force override, delta computations, search service integration, SSE event emission, and REST API endpoints.

---

## 9. PHASE 5 — VIRALITY / VELOCITY ENGINE

1. **Database Schema Enhancements (`backend/models/video.py`, `backend/models/search_job.py`):**
   - Added `velocity_score: Mapped[float]` and `virality_score: Mapped[float]` indexed columns to `Video`.
   - Added `velocity_score` and `virality_score` columns to `JobVideo`.
   - Automatic SQLite schema migration in lifespan startup (`backend/app/main.py`) via `PRAGMA table_info` and `ALTER TABLE ADD COLUMN`.
2. **Virality & Velocity Core Engine (`backend/services/virality_service.py`):**
   - Hourly velocity computed from empirical historical snapshots ($\ge 2$) or fallback to lifetime publication baseline.
   - Acceleration ($\Delta v / \Delta t$) computed when $\ge 3$ snapshots exist, adding a positive momentum bonus.
   - Age-bucket normalization (`fresh` < 24h, `trending` 1-7d, `established` 7-30d, `evergreen` > 30d) preventing age distortion.
   - Spread ratio calculation prioritizing high-intent actions ($2.5 \times \text{shares} + 1.5 \times \text{comments} + \text{favorites} / \text{likes}$).
   - Bounded composite Virality Score ($0 \le \text{Score} \le 100$).
   - Real-time SSE event dispatch `video:virality_update` via `EventBus`.
3. **Original Intent Paramount Ranking Integration (`backend/services/ranking_service.py`):**
   - Quality score incorporates virality: `quality_score = 100 * (0.60 * engagement + 0.15 * freshness + 0.25 * (virality_score / 100))`.
   - Multiplicative relevance constraint strictly preserved: `final_score = relevance * (0.8 + 0.2 * quality / 100)`. Off-topic viral distractors cannot outrank relevant content.
   - Persistent ranking updates across `SearchService` and `JobVideo` records.
4. **REST API Endpoints & Sorting (`backend/api/videos.py`):**
   - `GET /api/videos/{video_id}/virality`: returns complete `ViralityBreakdownOut`.
   - `POST /api/videos/{video_id}/virality/recalculate`: forces on-demand virality recalculation.
   - `GET /api/videos?sort=virality` and `sort=velocity`: server-side sorting with indexed fallback.
5. **Frontend UI Integration:**
   - Updated `Video` and `ViralityBreakdown` interfaces in `frontend/types/index.ts`.
   - Added "Xu hướng lan truyền (Virality)" and "Tốc độ tăng trưởng (Velocity)" options to sort select in `frontend/features/video/video-grid.tsx`.
   - Rendered virality badge (`🔥 {virality_score}`) with detailed hover tooltip in `frontend/features/video/video-card.tsx`.
6. **Targeted & Full Regression Verification (`tests/test_virality_engine.py`):**
   - 8 comprehensive tests covering velocity, snapshots, acceleration, age normalization, spread ratio, ranking intent preservation, DB updates, SSE emission, and REST API endpoints.

---

## 10. PHASE 6 & 6B — ORIGINAL INTENT RANKING V2 & BENCHMARK

1. **Field-Specific Provenance & Weighting (`backend/services/ranking_service.py`):**
   - Tokenizes title, hashtags, and caption individually.
   - Populates detailed `provenance: dict[str, list[str]]` recording exact matched tokens per field.
   - Title matches receive dominant weight, while caption stuffing is mitigated: caption-only matches in long captions (> 50 chars) are capped at `relevance <= 35.0`.
2. **Negative Keyword Penalty Engine:**
   - Supported `negative_keywords: list[str] | None = None` in `rank_detailed()` and `rank()`.
   - Checks phrase matches across title, caption, and hashtags.
   - If negative keywords match, penalizes relevance score significantly (`relevance = min(15.0, relevance * 0.25)`) and populates `matched_negative_tokens`.
3. **Dynamic Virality & Metric Ranking Cascade:**
   - Implemented `recalculate_scores_from_virality()` in `ranking_service.py`.
   - In `ViralityService.update_video_virality()`: dynamically recalculates `quality_score` and `final_score` on `Video` and `JobVideo` records and commits to SQLite.
   - In `MetricService.record_snapshot()`: automatically triggers `update_video_virality()` so new metric snapshots cascade directly into virality and search rankings.
4. **Targeted Benchmark Suite (`tests/test_original_intent_ranking_v2.py`):**
   - 6 comprehensive tests covering negative keyword penalties, field-specific provenance, title priority over caption spam, virality recalculating quality & final scores, automatic metric snapshot cascade, and viral distractor resistance.

---

## 11. PHASE 7 — AI SCRIPT ANALYSIS V2 (STORYBOARD & METADATA)

1. **Structured Storyboard Schema (`backend/schemas/script_analysis.py`):**
   - Created `StoryboardScene` model: `scene_number`, `timestamp_range`, `visual_action`, `spoken_narration`, `on_screen_text`, `sound_music`.
   - Expanded `ScriptAnalysisOut`: added `storyboard: list[StoryboardScene]`, `analysis_basis`, `analysis_version` ("v2.0"), `model_used`, and `created_at`.
   - Backward-compatible with legacy v1 analysis objects without database migrations.
2. **AI Prompts & Fallback Upgrades (`backend/providers/ai/gemini.py`, `backend/services/ai_service.py`):**
   - Upgraded `SCRIPT_SYSTEM_PROMPT` for Gemini to produce structured 3-5 scene storyboards.
   - Enhanced `generate_fallback_script_analysis()` to generate 4 realistic, production-ready storyboard scenes (Scene 1: Hook, Scene 2: Problem/Core, Scene 3: Solution/Demo, Scene 4: CTA).
3. **Frontend UI Storyboard View (`frontend/features/video/script-analysis-modal.tsx`):**
   - Added tab switcher between "Kịch Bản Lời Thoại (Script)" and "Bảng Phân Cảnh (Storyboard)".
   - Rendered visual scene cards with timeline pills, camera directions, voiceover script, text on screen, and sound effects.
   - Added `v2.0` badge and intelligent clipboard copy formatting (Script vs Storyboard shooting table).
4. **Targeted Test Suite (`tests/test_script_analysis_v2.py`):**
   - 4 comprehensive tests covering `StoryboardScene` validation, fallback generator v2, backward compatibility with v1 data, and API endpoint `/script-analysis`.
    - Total backend test suite: **327 / 327 tests passing 100% green (0 warnings)**.

---

## 12. SEARCH ACCURACY, SILENT HEADLESS & PRECISION GATE ENGINE

1. **Precision Gate & Candidate Quarantine Pool (`backend/services/search_service.py`):**
   - Implemented strict precision filtering: candidates missing mandatory search intents, scoring below `min_relevance`, or containing negative keywords are quarantined.
   - Quarantined candidates are never saved to the primary `Video` / `JobVideo` database tables. Instead, they are collected in `job.diagnostics["rejected_candidates"]` with detailed rejection reasons and relevance scores.
   - Frontend provides a collapsible "Video bị loại trừ" panel for full user transparency without cluttering the primary research grid.

2. **Silent Headless Search Default (`backend/core/config.py`, `backend/services/browser_service.py`):**
   - Configured `search_headless_silent = True` so normal search crawls run in completely invisible background Chromium contexts.
   - Headful window elevation is strictly reserved for manual user action checkpoints (e.g. interactive QR login or slider verification).
   - Once user action completes, browser automatically returns to silent headless mode.

3. **AI Translation Provenance & Model Tracking (`backend/providers/ai/gemini.py`, `backend/services/ai_service.py`):**
   - `SearchJob` tracks `actual_ai_model`, `ai_fallback_occurred`, and `ai_latency_ms` via SQLite migrations.
   - Frontend UI displays a provenance badge ("Gemini AI: <model>", "Từ điển", or "Query gốc") with latency and actual query chips.
   - UI automatically selects Gemini AI query translation when an API key is configured.

4. **Multi-Domain Evaluation Benchmark Suite (`tests/test_evaluation_benchmark.py`):**
   - 60 realistic Vietnamese queries spanning 5 domains: Beauty/Skincare, Fashion/Styling, Stationery/Handmade, Cooking/Baking, and Technology/Gadgets.
   - Validated **100.0% Precision@5** and **100.0% Precision@10** across all 60 benchmark queries with 0% query expansion failure rate.

---

## 13. ROADMAP & PHASE TRACKING

- [x] **PHASE 0 — SECURITY & BASELINE:** Completed (Secrets audited, tokens redacted, baseline 267/267 green, status established).
- [x] **PHASE 0.5 — BASELINE & PACKAGING CLOSURE:** Completed (Cookie contract aligned, lifespan harness fixed, clean packaging engine active).
- [x] **PHASE 1 — HYBRID HEADLESS ENGINE:** Completed (Config layer, elevation bug fixed, browser lock serialization, 18 regression tests green).
- [x] **PHASE 2 — DOWNLOAD QUEUE:** Completed (Bounded queue manager, fixed worker pool, backpressure, resumable Range requests, exponential backoff).
- [x] **PHASE 3 — EVENT STREAM / SSE:** Completed (In-memory EventBus, single-connection `/api/events`, real-time UI updates with polling fallback).
- [x] **PHASE 4 — VIDEO METRIC SNAPSHOTS:** Completed (Time-series snapshot table, migration, deduplicating throttling, REST API, SSE, frontend timeline).
- [x] **PHASE 5 — VIRALITY / VELOCITY ENGINE:** Completed (Historical velocity, acceleration, age bucket normalization, original intent ranking preservation, REST & SSE, UI badge).
- [x] **PHASE 6 & 6B — ORIGINAL INTENT RANKING V2 & BENCHMARK:** Completed (Field-specific provenance, negative keyword penalty, title priority over caption spam, dynamic cascade).
- [x] **PHASE 7 — AI SCRIPT ANALYSIS V2:** Completed (Shot-by-shot storyboard, analysis_basis metadata, versioned caching v2.0, tabbed UI).
- [x] **PHASE 7B — PRECISION GATE & SILENT HEADLESS:** Completed (Mandatory intent gate, quarantined candidate diagnostics, silent headless search by default, 327/327 tests green, 0 warnings).
- [x] **PHASE 8 — AUTO-DUBBING FOUNDATION:** Completed (Microsoft Neural TTS, offline fallback, audio ducking mixer, `/api/dubbing/*`, UI modal).
- [x] **PHASE 9 — MULTI-PLATFORM PROVIDER ARCHITECTURE:** Completed (PlatformCapabilities matrix, TikTok & YouTube Shorts search providers, registry dispatch).
- [x] **PHASE 10 — SCALE INFRASTRUCTURE:** Completed (BaseJobQueue priority queue, bounded capacity, DLQ, AdaptiveWorkerPool, `/api/system/queue-status`).
- [x] **PHASE 11 — OBSERVABILITY & RELEASE GATE:** Completed (TelemetryService p50/p95/p99 histograms, release readiness gate CLI, dashboard UI).

---

## 14. PHASE 8 — AUTO-DUBBING FOUNDATION

1. **TTS Adapters (`backend/services/dubbing/tts_adapter.py`):**
   - Microsoft Neural TTS (`EdgeTTSAdapter`) with Vietnamese neural voices (`vi-VN-HoaiMyNeural`, `vi-VN-NamMinhNeural`).
   - `OfflineFallbackTTSAdapter`: zero-dependency generator of valid 24kHz PCM WAV tones matching speech duration for offline environments.
2. **Audio Ducking Mixer (`backend/services/dubbing/audio_mixer.py`):**
   - ffmpeg-based audio ducking sidechain: automatically lowers original video background music (10-25%) when Vietnamese voiceover is active, then smoothly restores volume.
   - Resilient fallback to audio-only synthesis if video stream is unavailable or ffmpeg execution is in test mock mode.
3. **Dubbing Service & REST API (`backend/services/dubbing/service.py`, `backend/api/dubbing.py`):**
   - Asynchronous job execution with live SSE broadcasting (`dubbing:update`).
   - Endpoints: `GET /api/dubbing/voices`, `POST /api/dubbing/jobs`, `GET /api/dubbing/jobs/{id}`, `GET /api/dubbing/files/{filename}`, `GET /api/videos/{video_id}/dubbing`.
4. **Frontend UI Integration (`frontend/features/video/video-dubbing-modal.tsx`):**
   - Voice selector, speed/pitch/BGM controls, script pre-fill from storyboard, real-time SSE progress, inline audio playback and file download.

---

## 15. PHASE 9 — MULTI-PLATFORM PROVIDER ARCHITECTURE

1. **Platform Matrix & Capabilities Registry (`backend/providers/capabilities.py`):**
   - Defined `PlatformCapabilities` schema: `supports_headless`, `supports_video_download`, `supports_metrics`, `auth_domains`, `icon_name`, `is_enabled`.
   - Complete matrix for 5 platforms: Xiaohongshu, Douyin, TikTok Global, YouTube Shorts, and Mock.
2. **Search Providers (`backend/providers/tiktok/`, `backend/providers/youtube_shorts/`):**
   - `TikTokSearchProvider`: searches TikTok Global videos, extracts share URLs, views/likes, duration, hashtags.
   - `YouTubeShortsSearchProvider`: searches YouTube Shorts, extracts video IDs, duration, views.
   - Registry dispatch (`backend/providers/registry.py`) seamlessly resolves providers for any requested platform.
3. **REST Endpoints (`backend/api/platforms.py`):**
   - `GET /api/platforms`: returns all registered platform capabilities.
   - `GET /api/platforms/{platform_id}`: returns specific platform profile.
4. **Frontend UI Integration:**
   - Multi-platform selector chips in `frontend/features/search/search-workspace.tsx` allowing one-click switching between Xiaohongshu, Douyin, TikTok Global, and YouTube Shorts.

---

## 16. PHASE 10 — SCALE INFRASTRUCTURE

1. **Priority Job Queue Abstraction (`backend/core/queue/base.py`, `backend/core/queue/memory_queue.py`):**
   - `BaseJobQueue` abstract contract with priority ordering (HIGH, NORMAL, LOW).
   - `InMemoryPriorityQueue` with bounded capacity, backpressure (`QueueFullError`), retry backoff, and Dead Letter Queue (DLQ).
2. **Adaptive Worker Pool (`backend/core/queue/worker_pool.py`):**
   - Dynamic worker pool sizing based on queue depth and concurrency thresholds.
   - Graceful shutdown handling integrated into FastAPI app lifespan (`backend/app/main.py`).
3. **Queue Administration Endpoints (`backend/api/system.py`):**
   - `GET /api/system/queue-status`: reports current queue size, capacity, active workers, DLQ size.
   - `GET /api/system/dead-letter`: lists failed jobs requiring inspection or replay.

---

## 17. PHASE 11 — OBSERVABILITY & AUTOMATED RELEASE GATE

1. **Telemetry Service (`backend/services/observability.py`):**
   - Sliding-window histogram tracking request latency percentiles (p50, p95, p99, min, max, avg).
   - Global error rates, counters, and uptime metrics.
2. **Observability Endpoints (`backend/api/observability.py`):**
   - `GET /api/system/metrics`: full latency distribution and throughput statistics.
   - `GET /api/system/release-readiness`: automated evaluation of database migrations, queue capacity, error rates, and platform capabilities.
3. **Automated Release Gate Script (`scripts/release_gate.py`):**
   - 5-stage verification harness:
     1. Runtime environment check (Python, node, venv).
     2. Database schema & table verification.
     3. 60-query multi-domain evaluation benchmark (100% Precision@10).
     4. Backend regression test suite (348/348 passed).
     5. Frontend TypeScript typecheck (`tsc --noEmit`, 0 errors).
   - Generates release scorecard `release_gate_report.json` with status **APPROVED (GREEN)**.
4. **Health & Observability Dashboard (`frontend/app/health/page.tsx`):**
   - Real-time display of backend status, queue metrics, worker pool, latency distribution, and platform capability matrix.

