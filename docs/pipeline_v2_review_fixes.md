# Pipeline v2.3.2 — sửa lỗi sau review

Nguồn dùng để sửa là bản đầy đủ tại `C:\tool v2`, nhánh `refactor/pipeline-v2`.
Snapshot `_review_src` cũ thiếu frontend và `gender_detector.py`; bản cài đầy đủ
đã có những file này, lưu `gender` trong JSON và có test cho chế độ một giọng.
Không đổi mặc định của bản cài: `PIPELINE_MODE=v2`, `QC_GATE_POLICY=block`,
`ENABLE_AUTO_GENDER=false`.

## Thay đổi

- Mỗi manifest mới có `cache_generation`. Batch dịch/TTS/RVC chỉ được dùng lại
  trong cùng lượt xử lý. Đổi model, provider, nguồn video hoặc cấu hình tạo
  manifest mới; batch cũ không thể được nhận lại sau khi manifest bị vô hiệu hóa.
  Resume một job chưa xong giữ generation cũ. Tắt stage cache rồi chạy lại job
  hoàn tất cũng tạo generation mới. Version 2.3.2 vô hiệu hóa cache phiên bản cũ.
- Fingerprint cấu hình chứa `LLM_PROVIDER`, trạng thái có/không có credential
  dịch và style phụ đề; không chứa giá trị secret. RVC batch có cả hash index.
- Qwen gán mỗi token cho một phía của trung điểm vùng overlap; giữ nguyên
  timestamp toàn token thay vì cắt đầu token ở biên.
- API, Telegram, batch và preflight dùng chung cách đọc `.env`: biến môi trường
  tiến trình ưu tiên, sau đó mới lấy mặc định từ file; hỗ trợ UTF-8 BOM và quote.
- Font, màu và độ đậm đi từ API đến ASS và được lưu để resume. Giữ style chữ
  đen trên nền sáng cho bot/batch không truyền tuỳ chỉnh; chữ sáng có viền đen.
- FPT giữ provider và voice đã chọn khi bật tự phân loại giọng, thay vì đi nhầm
  sang CapCut ở segment nam. Chế độ RVC một giọng hiện có được giữ nguyên.

## Kiểm tra

Bộ test gồm kiểm tra bước ASR thật với inference giả lập, giữ gender qua lưu/đọc
và timing, cache khi đổi model/provider/video, resume, font ASS và đường truyền
tham số API, FPT, Qwen overlap. Test không gọi dịch vụ trả phí.

Chạy bằng venv đã cài:

```powershell
.\backend\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Đã kiểm tra 138 test backend đạt, gồm test FFmpeg; frontend lint/build đạt.
Preflight bản cài báo 36 pass, 0 warning, 0 error. Video 12 giây đã qua pipeline
cục bộ BS-RoFormer native FP16, Qwen3-ASR + forced alignment, OCR, ASS và NVENC.
Đây là kiểm tra xử lý/render với tiếng gốc,
không phải bằng chứng dịch và lồng tiếng cloud đã đạt.

Lượt E2E Gemini/Edge cần xác nhận gửi phụ đề/khung hình sang Gemini và văn bản
sang Edge TTS. Automatic approval review đã chặn lượt cloud khi chưa có xác nhận.
Sau khi áp dụng mã, chỉ khởi động lại bot/API khi hàng đợi đã rảnh để tránh trộn
module cũ trong tiến trình đang chạy với worker mới trên đĩa.
