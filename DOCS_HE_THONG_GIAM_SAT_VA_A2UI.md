# TOÀN BỘ TÀI LIỆU & MÃ NGUỒN: HỆ THỐNG GIÁM SÁT RENDER & A2UI STUDIO (TOOL V1)

> **Dành cho Người Dùng & AI Assistant (Codex, Claude, ChatGPT, Cursor)**  
> Bản cập nhật: 2026-09-07  
> Thư mục dự án: `C:\tool v1`  
> Môi trường Python: `C:\tool v1\backend\venv\Scripts\python.exe` (Python 3.10)  
> Cổng dịch vụ: `http://127.0.0.1:8088`

---

## 1. TỔNG QUAN KIẾN TRÚC HỆ THỐNG

Hệ thống bao gồm 2 phân hệ hoạt động song song trên cùng một máy chủ FastAPI (`backend/main.py`), đảm bảo tính độc lập tuyệt đối với lõi Tool V1:

```
                  ┌────────────────────────────────────────┐
                  │          FastAPI Web Server            │
                  │         http://127.0.0.1:8088          │
                  └──────┬──────────────────────────┬──────┘
                         │                          │
           ┌─────────────┴────────────┐             │
           ▼                          ▼             ▼
   [Phân Hệ 1: Giám Sát]       [Lõi Render V1]   [Phân Hệ 2: A2UI Studio]
   - GET /                    - batch_processor   - GET /a2ui
   - GET /api/status          - video_utils       - GET /api/a2ui/catalog
   - GET /api/phoi            - job_tracker       - GET /api/a2ui/scenario/{name}
   - GET /api/banve           - telegram_bot      - POST /api/a2ui/generate
   - POST /api/run-batch      (GPU NVENC CUDA)   - POST /api/a2ui/action (2-Way RPC)
```

---

## 2. DANH SÁCH FILE VÀ CẤU TRÚC CODEBASE

### A. Phân hệ Giám Sát Render & Video Phôi
| Đường Dẫn File | Vai Trò & Chức Năng |
| :--- | :--- |
| `C:\tool v1\backend\main.py` | Máy chủ FastAPI chính, tích hợp REST API, SSE log stream, phục vụ HTML dashboard & A2UI. |
| `C:\tool v1\backend\templates\dashboard.html` | Giao diện Dashboard Dark Mode (Bootstrap 5, Glassmorphism, thanh tiến độ 6 bước, video preview modal, log console tự cuộn). |
| `C:\tool v1\backend\job_tracker.py` | Mô-đun theo dõi tiến độ đa luồng thread-safe, ánh xạ các bước xử lý (Tách âm, Demucs, Whisper, OCR, Gemini, RVC, NVENC). |
| `C:\tool v1\tests\test_dashboard_api.py` | Bộ unit test kiểm thử toàn bộ API giám sát. |
| `C:\Users\admin\OneDrive\Desktop\Giao_Dien_Quan_Sat_Tool.bat` | Script Batch khởi động nền an toàn không pop-up, chống lỗi ký tự Windows, tự mở trình duyệt. |
| `C:\Users\admin\OneDrive\Desktop\Bang_Dieu_Khien_Tool_V1.url` | Lối tắt URL truy cập thẳng `http://127.0.0.1:8088/`. |

### B. Phân hệ Giao Diện Động AI Agent (A2UI v1.0)
| Đường Dẫn File | Vai Trò & Chức Năng |
| :--- | :--- |
| `C:\tool v1\backend\a2ui\protocol.py` | Triển khai chuẩn A2UI v1.0 (Google & CopilotKit standard): `CreateSurface`, `UpdateComponents`, `UpdateDataModel`, `CallAgentFunction`, `AgentFunctionResponse`, data binding `$bind`. |
| `C:\tool v1\backend\a2ui\catalog.py` | Danh mục Media Component Catalog bảo mật: `Card`, `Text`, `Button`, `ProgressBar`, `VoiceSelectorCard`, `SubtitleReviewCard`, `AudioMixerCard`, `VideoInspectorCard`, `ButtonGroup`. |
| `C:\tool v1\backend\a2ui\mock_agent.py` | 3 Kịch bản AI Agent mẫu: Lồng tiếng 2 nhân vật, Kiểm duyệt phụ đề nhanh 1-chạm, Đánh giá phôi & Audio Mixer. |
| `C:\tool v1\backend\a2ui\agent_generator.py` | Hàm `generate_a2ui_for_video(video_path)` trích xuất thông số video phôi và tạo gói A2UI động. |
| `C:\tool v1\backend\templates\a2ui_studio.html` | Trình biên dịch & hiển thị A2UI Client Native Renderer + Thanh soi giao thức Stream & 2-Way RPC Inspector. |
| `C:\tool v1\tests\test_a2ui_protocol.py` | Bộ test tự động kiểm thử giao thức A2UI, catalog validation, kịch bản mẫu và API endpoints. |

---

## 3. CHI TIẾT API CONTRACTS & ĐẶC TẢ GIAO THỨC

### 3.1. API Giám Sát (Dashboard Endpoints)
- **`GET /api/status`**:
  ```json
  {
    "is_running": true,
    "current_video": "1788783054_050209dd.mp4",
    "step_name": "RVC Dub (85%)",
    "step_index": 5,
    "progress_percent": 85,
    "queue_count": 0,
    "total_queue": 1,
    "eta_seconds": 12,
    "elapsed_seconds": 35
  }
  ```
- **`GET /api/phoi`**:
  Danh sách video trong thư mục nguồn `D:\video phôi`.
  ```json
  {
    "directory": "D:\\video phôi",
    "total_files": 3,
    "total_size_mb": 68.4,
    "files": [
      {"name": "vid1.mp4", "size_mb": 22.4, "created_at": "2026-09-07 19:00:00"}
    ]
  }
  ```
- **`GET /api/banve`**:
  Danh sách video thành phẩm đã render xong tại `D:\banve`.
  ```json
  {
    "directory": "D:\\banve",
    "total_files": 10,
    "total_size_mb": 351.07,
    "files": [
      {"name": "Dubbed_vid1.mp4", "size_mb": 16.22, "created_at": "2026-09-07 19:13:53"}
    ]
  }
  ```
- **`POST /api/run-batch`**:
  Kích hoạt quy trình render ngầm cho tất cả video trong `D:\video phôi`.

---

### 3.2. API & Giao Thức A2UI (Agent-to-User Interface v1.0)
Giao thức tuân thủ chuẩn JSON Streaming tuần tự gồm 3 loại bản tin cơ sở:

#### Bản tin 1: Khởi tạo Surface (`createSurface`)
```json
{
  "version": "v1.0",
  "createSurface": {
    "surfaceId": "multivoice-surface",
    "catalogId": "https://a2ui.org/catalogs/autodub-media-v1.json",
    "metadata": {}
  }
}
```

#### Bản tin 2: Cập nhật Cây Thành Phần UI (`updateComponents`)
Thành phần sử dụng cấu trúc phẳng (Flat Array) với tham chiếu ID con (`children`):
```json
{
  "version": "v1.0",
  "updateComponents": {
    "surfaceId": "multivoice-surface",
    "components": [
      {
        "id": "card-root",
        "component": "Card",
        "title": "AI Phát Hiện 2 Nhân Vật",
        "variant": "highlight",
        "children": ["voice-male", "voice-female", "btn-apply"]
      },
      {
        "id": "voice-male",
        "component": "VoiceSelectorCard",
        "selectedVoice": { "$bind": "/voice/male_id" },
        "options": [
          {"id": "rvc-chimai", "name": "Giọng Chí Mai", "type": "rvc", "recommended": true},
          {"id": "edge-nam", "name": "Nam Thần", "type": "edge", "recommended": false}
        ]
      },
      {
        "id": "btn-apply",
        "component": "Button",
        "label": "Áp Dụng Lồng Tiếng Kép 🚀",
        "variant": "primary",
        "action": {
          "callAgentFunction": {
            "name": "apply_dual_voices",
            "parameters": {
              "male_voice": { "$bind": "/voice/male_id" },
              "female_voice": { "$bind": "/voice/female_id" }
            }
          }
        }
      }
    ]
  }
}
```

#### Bản tin 3: Đồng bộ Mô Hình Dữ Liệu (`updateDataModel`)
```json
{
  "version": "v1.0",
  "updateDataModel": {
    "surfaceId": "multivoice-surface",
    "path": "/voice",
    "value": {
      "male_id": "rvc-chimai",
      "female_id": "edge-hoaimy"
    }
  }
}
```

#### Tương tác 2 chiều (2-Way RPC): `POST /api/a2ui/action`
- **Request từ Client Renderer khi người dùng bấm nút:**
  ```json
  {
    "name": "apply_dual_voices",
    "parameters": {
      "male_voice": "rvc-chimai",
      "female_voice": "edge-hoaimy"
    },
    "functionCallId": "call-1788798860290"
  }
  ```
- **Response từ Backend Agent:**
  ```json
  {
    "agentFunctionResponse": {
      "functionCallId": "call-1788798860290",
      "result": {
        "status": "success",
        "message": "Agent đã tiếp nhận và thực thi hàm 'apply_dual_voices' thành công!",
        "executed_at": "2026-09-07 23:34:20"
      }
    }
  }
  ```

---

## 4. HƯỚNG DẪN DÀNH CHO CODEX / DEVELOPER TIẾP QUẢN

### Khởi động thủ công:
```powershell
# Chạy Dashboard & A2UI Server
& "C:\tool v1\backend\venv\Scripts\python.exe" "C:\tool v1\backend\main.py"

# Chạy bot Telegram (nếu cần nhận lệnh qua Telegram)
& "C:\tool v1\backend\venv\Scripts\python.exe" "C:\tool v1\backend\telegram_bot.py"
```

### Chạy kiểm thử tự động (Unit Tests):
```powershell
# Test riêng phân hệ A2UI
& "C:\tool v1\backend\venv\Scripts\python.exe" -m unittest tests/test_a2ui_protocol.py

# Test toàn bộ dự án Tool V1 (135 tests)
& "C:\tool v1\backend\venv\Scripts\python.exe" -m unittest discover -s tests
```

### Cách mở rộng Component mới trong A2UI Catalog:
1. Thêm định nghĩa Schema vào `C:\tool v1\backend\a2ui\catalog.py` trong biến `MEDIA_COMPONENT_CATALOG["components"]`.
2. Khai báo hàm render tương ứng trong `C:\tool v1\backend\templates\a2ui_studio.html` tại hàm `renderComponent(comp, compMap)`.
3. Khai báo handler xử lý action trong `C:\tool v1\backend\main.py` tại endpoint `/api/a2ui/action`.
