# Bắt đầu từ đây

Đây là lối vào nhanh; bản đồ tính năng đầy đủ nằm trong [README.md](README.md).

## Chọn phiên bản

- **Tool V1:** checkout nhánh [tool-v1](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1), xem [handover dashboard V1](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-v1/DASHBOARD_MONITOR_HANDOVER.md).
- **Tool V2:** checkout nhánh [tool-v2](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v2), xem [hướng dẫn đầy đủ](TOOL_V2_FULL_GUIDE.md) và [rollout guide](docs/pipeline_v2_rollout.md).
- **AI Video Research Tool:** dùng README và launcher riêng trên nhánh [tool-tim-kiem-video](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-tim-kiem-video).

## Checklist Tool V2

1. Cài Python 3.10, Node.js LTS, FFmpeg/ffprobe; chuẩn bị driver NVIDIA nếu dùng CUDA/NVENC.
2. Cài đúng CUDA-enabled PyTorch trước các package backend, rồi tạo backend/venv và backend/model_venv theo requirements.
3. Tạo backend/.env từ backend/.env.example; điền secret và sửa path theo máy. Không commit .env.
4. Chạy preflight bằng đúng venv:

~~~powershell
cd backend
.\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface all
cd ..
~~~

5. Chỉ xử lý thật khi preflight sẵn sàng; thử một video ngắn có quyền sử dụng và xem QC/report.
6. Dùng start_bot.bat cho Telegram + Electron UI. Dashboard Monitor web cần chạy riêng bằng:

~~~powershell
.\backend\venv\Scripts\python.exe .\backend\dashboard_monitor.py
~~~

Dashboard Monitor V2: http://127.0.0.1:8089. Chi tiết batch, resume, QC, rollback và cấu hình model nằm trong [rollout guide](docs/pipeline_v2_rollout.md).

## Tài liệu cốt lõi

- [README.md](README.md): tính năng, service, cấu trúc và hướng dẫn tổng quan.
- [VERSIONS.md](VERSIONS.md): nhánh và cổng service.
- [docs/README.md](docs/README.md): mục lục tài liệu.
- [TOOL_V2_FULL_GUIDE.md](TOOL_V2_FULL_GUIDE.md): hướng dẫn kỹ thuật chi tiết.
- [CODE_HANDOFF_2_12.md](CODE_HANDOFF_2_12.md): ghi chú bàn giao; các test trong đó là kết quả tại thời điểm ghi, không phải CI hiện tại.

Trước khi cập nhật hoặc khởi động lại bot, xác nhận đúng nhánh, queue trống và không có model/FFmpeg đang chạy. Không xóa workspace/model/media để “dọn repo”; chúng là dữ liệu runtime và phải xử lý riêng.
