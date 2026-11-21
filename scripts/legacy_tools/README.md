# Legacy & Standalone Utility Scripts

Thư mục này lưu trữ các script tiện ích chạy tay một lần (benchmark, re-render thử nghiệm, công cụ nội bộ cũ) được tách ra khỏi `backend/` gốc để giữ sạch không gian mô-đun sản xuất của Tool V1.

Khi cần chạy thủ công một script trong thư mục này, đặt `PYTHONPATH` trỏ vào `backend`:

```powershell
$env:PYTHONPATH = "C:\tool v1;C:\tool v1\backend"
& "C:\tool v1\backend\venv\Scripts\python.exe" "scripts\legacy_tools\<ten_script>.py"
```

## Chuyển vào ngày 2026-11-21

Ba script không được module, test, CI hay file `.bat` nào tham chiếu:

- `diagnose_canary.py`, `diagnose_covers.py`: script chạy một lần, đọc cứng đường dẫn `D:\workspace_v2\canary_batch_runs\...`.
- `audit_video_timeline.py` (trước đây ở `backend/scripts/`): công cụ kiểm tra timeline thủ công.
