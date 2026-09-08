# AutoDub Tool V1 và Tool V2

Repository giữ hai phiên bản độc lập bằng branch để lịch sử và runtime không
ghi đè lẫn nhau:

| Phiên bản | Branch | Mục đích |
| --- | --- | --- |
| Tool V1 | [`tool-v1`](https://github.com/meobeo240107/auto-dubbing-bot/tree/tool-v1) | Pipeline V1 ổn định, bot/token riêng |
| Tool V2 | [`tool-v2`](https://github.com/meobeo240107/auto-dubbing-bot/tree/tool-v2) | Pipeline V2 production, model mạnh và QC block |

## Có trên branch `tool-v2`

- Toàn bộ source `backend/` và `frontend/`.
- Pipeline v2, downloader, model workers, Telegram bot, batch processor.
- Tests, tài liệu, script setup token và cấu hình mẫu không chứa secret.
- Model/index RVC được quản lý bởi Git LFS.

## Cố ý không đưa lên GitHub

- `backend/.env`: token Telegram và API key.
- `backend/venv`, `backend/model_venv`: môi trường có thể tái tạo từ requirements.
- `models/`: cache Qwen3-ASR, ForcedAligner, PP-OCRv6 và BS-RoFormer.
- `workspace/`, video đầu vào/đầu ra, log và cache tạm.

Các mục trên không phải mã nguồn. Chúng được loại trừ để tránh lộ bí mật/dữ
liệu người dùng và tránh biến Git repository thành bản sao runtime hơn 21 GB.

## Tái dựng V2 trên máy khác

```powershell
git clone --branch tool-v2 https://github.com/meobeo240107/auto-dubbing-bot.git
cd auto-dubbing-bot
git lfs pull
py -3.10 -m venv backend\venv
.\backend\venv\Scripts\python.exe -m pip install -r requirements.txt
py -3.10 -m venv backend\model_venv
.\backend\model_venv\Scripts\python.exe -m pip install -r backend\requirements-models.txt
Copy-Item backend\.env.example backend\.env
```

Sau đó điền secret cục bộ vào `backend/.env`, cài PyTorch/TorchAudio phù hợp
CUDA của máy và chạy preflight. Model cache sẽ được tải lại khi model runtime
được khởi tạo; không cần sao chép cache từ máy production.
