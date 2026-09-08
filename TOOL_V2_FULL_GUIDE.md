# 🚀 HƯỚNG DẪN TOÀN DIỆN & TÀI LIỆU KỸ THUẬT AUTO DUBBING BOT - PIPELINE V2

Tài liệu này tổng hợp toàn bộ kiến trúc, hướng dẫn vận hành, cấu hình và tính năng của **Pipeline v2** trong dự án **Auto Video Dubbing Bot**.

---

## 📌 1. TỔNG QUAN KIẾN TRÚC PIPELINE V2

Pipeline v2 là hệ thống xử lý video đa phương tiện hướng trạng thái (State-driven DAG), được thiết kế để vận hành 24/7 ổn định trên card đồ họa phổ thông (NVIDIA RTX 4050 6GB VRAM) mà không xảy ra hiện tượng rò rỉ bộ nhớ (VRAM Leak) hay sập ứng dụng (OOM).

---

## 🌟 2. CÁC TÍNH NĂNG ĐỘT PHÁ CỦA V2

### 1. Cách ly tiến trình GPU (GPU Process Isolation & Lock)
- Mỗi model AI nặng (BS-RoFormer/Demucs, Qwen3-ASR/Whisper,
  PP-OCRv6/EasyOCR, RVC) được thực thi trong một tiến trình con (subprocess) độc lập.
- Sau khi hoàn thành stage, tiến trình con tự hủy -> **Hệ điều hành Windows thu hồi 100% VRAM về 0MB**.
- GPULock sử dụng file-lock nguyên tử đảm bảo chỉ 1 tác vụ AI chiếm dụng GPU tại một thời điểm.

### 2. Điểm phục hồi nguyên tử (Atomic Checkpoints & Resume)
- Trạng thái từng stage được ghi vào job_manifest.json theo cơ chế nguyên tử (atomic_replace_file + os.fsync).
- Nếu ứng dụng bị ngắt đột ngột (rớt mạng, cúp điện), hệ thống sẽ **tự động Resume tiếp tục đúng stage đang dang dở** khi khởi động lại mà không cần chạy lại từ đầu.

### 3. Bộ trộn âm thanh Studio (FFmpeg Studio Mixer Engine)
- **Sidechain Ducking:** Nhạc nền tự động hạ âm lượng khi nhân vật nói và nâng âm lượng lên khi ngắt câu.
- **Chuẩn hóa EBU R128:** Âm lượng tổng thể đạt chuẩn phát sóng quốc tế **-15 LUFS**.
- **True Peak Limiter:** Giới hạn đỉnh âm lượng ở **-1 dBTP**, loại bỏ hoàn toàn hiện tượng rè loa trên điện thoại.

### 4. Thuật toán Căn chỉnh Nhịp đọc (Timing Solver 0.92x – 1.40x)
- Tính toán ngân sách từ ngữ trước khi đọc thoại.
- Giới hạn tốc độ đọc an toàn từ **0.92x đến 1.40x**, giúp giọng lồng tiếng tự nhiên, không bị méo tiếng hay đọc dồn dập.

### 5. Khóa Model Giọng đọc Chí Mai RVC 55.2MB
- Tự động nhận diện và khóa chặt model MyVoiceModel_v2/mi-giong_cua_toi_v2.pth.
- Cơ chế thử nghiệm RVC 3 tầng (rmvpe -> pm -> harvest), đảm bảo các từ cảm thán và câu thoại ngắn không bị rơi về giọng mặc định.

### 6. Khóa Dải Phụ Đề Chữ Hán (Strict Chinese Subtitle Locking)
- Chỉ nhận diện và che phụ đề tiếng Trung, loại bỏ 100% các dòng phụ đề tiếng Anh trong video song ngữ.
- Tự động ghim trục Y cố định theo đường chữ Trung chuẩn, triệt tiêu hiện tượng phụ đề bị nhảy lên xuống.

### 7. Cổng Kiểm định Chất lượng Tự động (Automated QC Gate)
- Tự động đo kiểm sau render: kiểm tra độ lệch âm thanh/hình ảnh, phát hiện khoảng lặng chết, kiểm tra vùng an toàn phụ đề.
- 3 chế độ linh hoạt: report_only (chỉ báo cáo), warn (cảnh báo), block (chặn xuất nếu lỗi).

---

## ⚙️ 3. BẢNG CẤU HÌNH BIẾN MÔI TRƯỜNG (backend/.env)

| Tên biến | Giá trị khuyên dùng | Ý nghĩa |
| :--- | :--- | :--- |
| PIPELINE_MODE | v2 | Kích hoạt Pipeline v2 (v2 hoặc legacy) |
| BOT_TOKEN | [Token Telegram của bạn] | Mã Token bot Telegram |
| GEMINI_API_KEY | [Key Gemini của bạn] | API Key Google Gemini (Vision & Translation) |
| ATEMPO_MIN | 0.92 | Tốc độ đọc chậm nhất |
| ATEMPO_MAX | 1.40 | Tốc độ đọc nhanh nhất |
| TARGET_LUFS | -15.0 | Chuẩn âm lượng tổng thể EBU R128 |
| TRUE_PEAK_MAX_DBTP | -1.0 | Ngưỡng chặn rè âm True Peak |
| QC_GATE_POLICY | block | Chặn giao video khi QC phát hiện lỗi nghiêm trọng |
| ENABLE_RVC | true | Bật tính năng lồng tiếng RVC AI |

---

## 🚦 4. HƯỚNG DẪN KHỞI CHẠY & VẬN HÀNH

### 1. Kiểm tra Sức khỏe Hệ thống (Preflight Check)
Mở PowerShell tại thư mục backend/ và chạy:
```powershell
.\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface all
```
Yêu cầu production hiện tại: `ready=True pass=32 warning=0 error=0` cho giao diện Telegram.

### 2. Khởi động Bot Telegram
- **Cách 1 (Giao diện chuẩn):** Chạy file start_bot.bat ở thư mục gốc.
- **Cách 2 (Chạy ngầm không hiện cửa sổ):** Nhấp đúp vào KhoiDongAn.vbs.

### 3. Chạy Hàng loạt Thư mục Cục bộ (Batch Processing)
Chạy file run_batch_edit.bat để render toàn bộ video có trong thư mục đầu vào.

### 4. Khởi chạy Bộ Kiểm thử (Run All Tests)
```powershell
.\backend\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```
Kết quả hiện tại: 115/115 tests đạt chuẩn.

---

## 🔄 5. HƯỚNG DẪN QUAY VỀ BẢN V1 (ROLLBACK) KHI CẦN

Nếu cần chuyển nhanh về bản V1 (Legacy):
1. Dùng nhánh GitHub `tool-v1` cho mã nguồn V1 độc lập.
2. Dùng nhánh `tool-v2` cho Pipeline V2 production.
3. Chỉ dùng `PIPELINE_MODE=legacy` để rollback tạm thời trong checkout V2.

---

## 📦 6. PHẠM VI DỮ LIỆU TRÊN GITHUB

- Source backend/frontend, tests, tài liệu và script vận hành được track đầy đủ.
- Model RVC `.pth`/`.index` được lưu bằng Git LFS.
- Qwen3-ASR, ForcedAligner, PP-OCRv6 và BS-RoFormer được tải vào `models/`
  khi cài/chạy model runtime; cache này không commit vì có kích thước khoảng 4.2 GB.
- `backend/venv`, `backend/model_venv`, `workspace`, video render, log và `.env`
  không được đẩy lên GitHub. Chúng có tổng dung lượng hơn 21 GB và `.env` chứa token/API key.
