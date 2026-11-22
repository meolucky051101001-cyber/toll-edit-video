# Tool V1 — ghi chú vận hành cho agent

## Runtime (từ 2026-11-22)
- `backend\venv` và `backend\model_venv` dùng Python 3.12.10 standalone tại `%LOCALAPPDATA%\Programs\Python\Python312` (không còn dùng Python trong cache Codex). `backend\model_venv_separator` và `backend\venv_py310` dùng Python 3.10. `%LOCALAPPDATA%\py.ini` giữ `py` mặc định là 3.10.
- Backup `.env`/`pyvenv.cfg` trước khi đổi: `D:\workspace\_env_backups_20261122\`.

## Dịch vụ (Scheduled Tasks)
- `AutoDubV1-telegram`, `AutoDubV1-scriptbot`, `AutoDubV1-dashboard` (supervisor `background_service.py`), `AutoDub-control` (8090), `AutoDubV1-ops-watchdog`, `AutoDubV1-telebot-downloader`, `AutoDubV1-cloud-node` (8089, thuộc phạm vi Cloud — không restart khi làm việc local).
- Supervisor nạp `.env` lúc khởi động và truyền xuống bot con → đổi `.env` phải restart cả supervisor:
  `backend\venv\Scripts\python.exe -B backend\v1_shutdown.py --scope processing --mode restart` (+ `--scope downloader`), rồi `Start-ScheduledTask` từng task. `--scope all --mode restart --include-downloader` cho toàn bộ (không gồm cloud node). Luôn `--preview` trước.
- Chỉ deploy/restart khi rảnh: `D:\workspace\control\v1.json` có `busy=false`, `queue=0`; `D:\workspace\bot_system\telegram_queue.json` rỗng. Không pull code khi service đang chạy job (lệch phiên bản module → ImportError/TypeError).

## Kiểm tra
- Sức khỏe: `backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py --timeout 30` (kỳ vọng PASS, preflight 27/27).
- CI chỉ chạy danh sách test trong `.github/workflows/python-quality.yml`. Suite đầy đủ local (2026-11-22) có 13 test fail sẵn trên `main` (cloud cache/telemetry, port 8090 bận, một số test V1 cũ ngoài CI) — so sánh với baseline trước khi kết luận regression. Test trong git worktree thiếu venv/model nên có thêm fail giả.
- Lint: `python -m ruff check .`

## Quan sát hiệu năng
- Thời gian từng stage: `D:\workspace\bot_system\manifests\*.manifest.json` (`stages.*.duration_s`, `metrics`); log `app.log` (`v1.performance`), `D:\workspace\bot_system\service_logs\`, `ops_health.json`.
- RAM là nút thắt chính: cùng video 54s, OCR mất 196s khi RAM trống ~0.4–1.8 GB nhưng 58s khi trống ~3 GB. Keep-warm ASR/tách âm/OCR tự tắt khi RAM < 2000/2000/1500 MB.

## Biến cấu hình mới (2026-11-22)
- `V1_SPEECH_MAX_FOLLOW_DELAY_S` (mặc định 0.5, `0` = tắt): khi một câu TTS không vừa khung đọc, cho câu kế tiếp lùi tối đa chừng này giây (nén ≤ 1.5x) thay vì fail job.
- `V1_FROZEN_CONFIG_RETENTION_DAYS` (mặc định 14, `0` = giữ hết): mỗi lần đóng băng cấu hình job sẽ xóa snapshot cũ hơn số ngày này khỏi `D:\workspace\control\job_frozen_configs.json`.
