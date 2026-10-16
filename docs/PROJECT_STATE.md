# BÁO CÁO TRẠNG THÁI HỆ THỐNG & DỰ ÁN (PROJECT STATE & HANDOFF)
*Tạo ngày: 03/10/2026 - Dành cho Antigravity IDE & Antigravity 2.0*

---

## 1. TỔNG QUAN HỆ THỐNG
Dự án bao gồm 2 công cụ AutoDub độc lập chạy song song, được điều phối bởi 1 bộ điều khiển trung tâm (Central Controller) trên cổng 8090:

1. **Tool V1 (`C:\tool v1`):**
   - Chuyên xử lý video ngắn (Shorts / Reels / TikTok).
   - Backend chạy cổng: `8088` (`main.py`, `dashboard_monitor.py`, `telegram_bot.py`).
   - Sử dụng Whisper Large V3 / Gemini, RVC (giọng Chí Mai), CapCut TTS, Edge TTS.
   - Thư mục điều khiển: `C:\tool v1\workspace\bot_system\control\` và `C:\tool v1\workspace\control\`.

2. **Tool V2 (`C:\tool v2`):**
   - Chuyên xử lý video dài (Long-form YouTube / Podcasts / Giải thích).
   - Backend chạy cổng: `8089` (`dashboard_monitor.py`, `telegram_bot.py`, pipeline V2).
   - Nâng cấp đặc biệt:
     - Tích hợp **VieNeu-TTS (48kHz Neural Vietnamese TTS)** chạy 100% cục bộ trên GPU NVIDIA (CUDA).
     - Giao diện Dashboard V2 có chọn giọng riêng lẻ (`manualVoiceSelect`, `autoFemaleVoiceSelect`, `autoMaleVoiceSelect`).
     - Tích hợp logic cắt phụ đề thông minh, giữ nguyên giọng gốc / dịch đè âm thanh.
   - Thư mục điều khiển: `C:\tool v2\workspace\control\`.

3. **Bộ điều khiển trung tâm (Central Controller - `tool_control.py`):**
   - Cổng giao diện: `http://127.0.0.1:8090`.
   - File chạy: `C:\tool v1\backend\tool_control.py` (đồng bộ với `C:\tool v1\tool_control.py`).
   - Quy tắc an toàn: Độc quyền (Chỉ bật 1 Tool V1 hoặc Tool V2 tại một thời điểm, hoặc tắt cả 2 để giải phóng 100% RAM & VRAM GPU).
   - Quản lý danh mục 26 giọng đọc dùng chung, tích hợp video test giọng 14s.

---

- **12 giọng CapCut TTS (Chất lượng cao & Mặc định):**
  - `capcut-BV562_streaming`: Mai (Nữ Chuẩn - Mặc định)
  - `capcut-BV075_streaming`: Thanh Niên Tự Tin (Nam Trẻ)
  - `capcut-multi_male_felipe_uranus_bigtts`: Nam Trầm (Chuyên Gia)
  - `capcut-BV421_vivn_streaming`: Nhỏ Ngọt Ngào (Nữ Dễ Thương)
  - `capcut-BV074_streaming`: Cô Gái Hoạt Ngôn (Nữ Năng Động)
  - `capcut-multi_female_richgirl_uranus_bigtts`: Review Phim (Kịch Tính)
  - `capcut-vi_female_huong`: Hương (Nữ Phổ Thông)
  - `capcut-multi_female_banmai`: Ban Mai (Tươi Sáng)
  - `capcut-BV007_streaming`: Nam Phổ Thông
  - `capcut-BV008_streaming`: Nam Trầm Ấm
  - `capcut-BV074_streaming_dsp`: Giọng Bé (Dễ Thương)
  - `capcut-BV075_streaming_vibrato_dsp`: Việt Méo (Hài Hước)
- **14 giọng Vie-TTS / VieNeu (48kHz Studio):**
  - `vieneu-haidang`: Hải Đăng (Nam Bắc)
  - `vieneu-maianh`: Mai Anh (Nữ Bắc)
  - `vieneu-trucly`: Trúc Ly (Nữ Bắc)
  - `vieneu-thienminh`: Thiện Minh (Nam Kể chuyện)
  - `vieneu-thuydung`: Thùy Dung (Nữ Nam)
  - `vieneu-adambua`: Adam Bựa (Nam Hài hước)
  - `vieneu-thaison`: Thái Sơn (Nam Nam)
  - `vieneu-phamtuyen`: Phạm Tuyên (Nam Trầm ấm)
  - `vieneu-ngochuyen`: Ngọc Huyền (Nữ Tự nhiên)
  - `vieneu-ngoctran`: Ngọc Trân (Nữ Miền Trung)
  - `vieneu-quangson`: Quang Sơn (Nam Miền Trung)
  - `vieneu-myduyen`: Mỹ Duyên (Nữ Nam Đọc truyện)
  - `vieneu-quynhanh`: Quỳnh Anh (Nữ Bắc Đọc truyện)
  - `vieneu-thanhbinh`: Thanh Bình (Nam Kể chuyện)
- **2 giọng Microsoft Edge TTS:**
  - `microsoft-hoaimy`: Hoài My (Nữ)
  - `microsoft-namminh`: Nam Minh (Nam)
*(Model RVC đã được gỡ bỏ hoàn toàn; tự động ánh xạ cấu hình cũ sang CapCut Mai)*

---

## 3. CÁC CÔNG VIỆC VỪA HOÀN TẤT GẦN ĐÂY
1. **Phân tích 2 repo mã nguồn mở:** `Editor-AI-App` & `VieNeu-TTS`.
2. **Triển khai Giai đoạn 1 (VieNeu-TTS):** Cài đặt thư viện `vieneu`, xây dựng service `C:\tool v2\backend\ai\vieneu_tts_service.py`, nạp 14 giọng vào danh mục `voice_catalog.json`.
3. **Triển khai Giai đoạn 2 (Tích hợp Dashboard Tool V2):** Cập nhật `dashboard.html` để chọn giọng thủ công, giọng nữ/nam tự động theo từng nhóm optgroup.
4. **Triển khai Giai đoạn 3 (Khung Test Giọng Controller 8090):**
   - Sinh bộ âm thanh & video mẫu 14s cho tất cả 14 giọng VieNeu.
   - Thêm optgroup VieNeu và các nút Quick Chip trên trang 8090.
   - Hỗ trợ xem thử video, nghe audio riêng và bấm 1 nút lưu đồng bộ cả 2 tool.
   - Khắc phục lỗi socket `TIME_WAIT` và thiết lập `allow_reuse_address = True`.

---

## 4. CÁCH KIỂM TRA & VẬN HÀNH
- **Giao diện điều khiển chung:** `http://127.0.0.1:8090`
- **Dashboard Tool V1:** `http://127.0.0.1:8088`
- **Dashboard Tool V2:** `http://127.0.0.1:8089`
- File khởi động Tool V1: `C:\tool v1\Giao_Dien_Quan_Sat_Tool.bat`
- File dừng toàn bộ: `C:\tool v1\TatHoanToan.bat`
