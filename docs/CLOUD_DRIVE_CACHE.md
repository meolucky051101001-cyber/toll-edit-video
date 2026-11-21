# Google Drive cache cho Cloud GPU

Cache môi trường và model trên Google Drive **tắt mặc định**. Để bật cho các phiên Cloud mới:

```bash
export AUTODUB_CLOUD_CACHE_BACKEND=gdrive
```

Có thể đổi thư mục cache bằng `AUTODUB_CLOUD_CACHE_DRIVE_ROOT` (mặc định
`/content/drive/MyDrive/AutoDubCloudCache`) và tắt việc lưu cache bằng
`AUTODUB_CLOUD_CACHE_SAVE=0`. Ước tính cần khoảng **6–10 GB** dung lượng Drive; đây
là ước tính và phụ thuộc runtime cùng các model đang dùng.

Khi cache chưa có hoặc không hợp lệ, job chỉ lưu archive sau khi pipeline hoàn tất
thành công. Archive chứa isolated venv, Python do uv quản lý và các thư mục model
đã được điền; nếu pipeline thất bại thì cache không được lưu.

Trước khi chạy job, hãy cấp quyền Drive một lần trong WSL ở chế độ tương tác:

```bash
colab new -s drivetest && colab drivemount -s drivetest
```

Nếu mount Drive thất bại, job vẫn tiếp tục mà không dùng cache. Để rollback, tắt
backend bằng cách bỏ biến môi trường hoặc đặt lại:

```bash
unset AUTODUB_CLOUD_CACHE_BACKEND
```
