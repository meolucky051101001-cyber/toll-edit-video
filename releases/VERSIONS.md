# Bản đồ phiên bản và nhánh Git

Repository lưu trữ các nhánh theo phân định rõ ràng:

---

## 1. Trạng thái các nhánh trên GitHub

| Nhánh | Trạng thái | Vai trò chính |
| --- | --- | --- |
| **`tool-v1`** | **CHÍNH THỨC (Production)** | Toàn bộ mã nguồn sản xuất của Tool V1: 8 bước xử lý tự động, GPU BS-RoFormer, Whisper Turbo, Gemini AI failover, Telegram Bot, Dashboard V1 (port 8088) và Tool Control (port 8090). |
| **`main`** | **CHÍNH THỨC (Production Default)** | Nhánh mặc định của kho lưu trữ, đồng bộ và phản ánh trực tiếp trạng thái của Tool V1. |
| **`tool-v2`** | **ĐÃ DỪNG (Deprecated / Retired)** | Nhánh lưu trữ lịch sử của Tool V2. Hệ thống đã dừng sử dụng hoàn toàn và được gỡ bỏ khỏi môi trường vận hành thực tế. |
| **`tool-tim-kiem-video`**<br>(alias: `ai-video-research-tool`) | **Độc lập** | Ứng dụng nghiên cứu và thu thập video AI độc lập (crawler Douyin/Xiaohongshu), không chạy chung pipeline với Tool V1. |

---

## 2. Dịch vụ và Cổng mạng của Tool V1

- **Dashboard V1 Web:** `http://127.0.0.1:8088/`
- **Tool Control Server:** `http://127.0.0.1:8090/`
- **Telegram Bot V1:** Vận hành qua script `KhoiDongAn.vbs` hoặc `start_bot.bat`.
