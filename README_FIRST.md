# AutoDub Pipeline v2 — Bàn giao production

Thư mục này chứa cây mã nguồn đầy đủ của nhánh `tool-v2` cùng toàn
bộ thay đổi ổn định hóa Pipeline v2 trong vòng review cuối.

## Nội dung

- `backend/`: Downloader, BS-RoFormer/Demucs, Qwen3-ASR/Whisper,
  PP-OCRv6/EasyOCR, Gemini Translation,
  Timing Solver, TTS, RVC, FFmpeg Mixer, QC Gate, API và Telegram bot.
- `frontend/`: Electron/React UI, preload bridge, dependencies và production build.
- `MyVoiceModel_v2/`: model và index RVC hiện có.
- `tests/`: unit/regression tests Pipeline v2.
- `docs/`: kiến trúc và hướng dẫn rollout.
- `.git/`: lịch sử Git của repository.
- `AutoDub_Pipeline_v2_review.patch`: patch hợp nhất có thể áp dụng lại lên bản
  gốc của nhánh `refactor/pipeline-v2`.

## Kết quả kiểm định hiện tại

- 115/115 unit và regression tests đạt.
- Preflight Telegram: 32/32 đạt, `ready=True`.
- E2E đủ các stage đạt QC 12/12, không cảnh báo.
- CUDA RTX 4050, FFmpeg/ffprobe và NVENC đã được xác minh.
- GitHub production branch: `tool-v2`.

## Trạng thái máy tại thời điểm bàn giao

Máy production hiện dùng `backend/venv` cho bot/pipeline và
`backend/model_venv` cho model mạnh. Các thư mục môi trường, cache model,
workspace, log và `.env` được giữ ngoài Git vì có thể sinh lại hoặc chứa dữ
liệu riêng tư. Model RVC cần thiết được quản lý bằng Git LFS.

Không chạy production trước khi tạo `backend/venv`, cài dependencies, sao chép
`backend/.env.example` thành `backend/.env`, điền secrets và nhận kết quả
`ready=True` từ:

```powershell
cd backend
.\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface all
```

Production dùng `QC_GATE_POLICY=block`. Xem checklist đầy đủ tại
`docs/pipeline_v2_rollout.md` và sơ đồ phiên bản tại `VERSIONS.md`.
