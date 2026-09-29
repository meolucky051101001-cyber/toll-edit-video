# Bản đồ phiên bản và nhánh

Repository lưu nhiều công cụ theo các nhánh Git độc lập. Không checkout một nhánh rồi giả định nó chứa phiên bản của nhánh khác.

## Nhánh chuẩn

| Nhánh | Vai trò | Điểm vào / cổng tham khảo |
| --- | --- | --- |
| [main](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/main) | Nhánh tổng hợp và đích nhận thay đổi chung; có thể chậm hơn nhánh tính năng cho đến khi PR được nhập | README và tài liệu tổng quan |
| [tool-v1](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1) | Pipeline V1, dashboard monitor và công cụ tương thích V1 | Dashboard mặc định 8088 |
| [tool-v2](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v2) | Pipeline V2, Telegram, batch, Dashboard Monitor và API/editor | Dashboard 8089; API desktop 8000 |
| [tool-tim-kiem-video](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-tim-kiem-video) | Ứng dụng nghiên cứu/tìm kiếm độc lập | Theo README của nhánh |

## Alias và nhánh tương thích

- **refactor/pipeline-v2** hiện cùng nội dung với **tool-v2**. Dùng **tool-v2** làm nhánh chuẩn để tránh phát triển hai bản giống nhau.
- **ai-video-research-tool** là alias tương thích của **tool-tim-kiem-video**. Ưu tiên tên **tool-tim-kiem-video**.
- Alias có thể được giữ để tương thích liên kết cũ; không có nghĩa đó là một sản phẩm/phiên bản riêng.

## Chọn nhánh

~~~powershell
git fetch origin
git switch tool-v1
# hoặc
git switch tool-v2
~~~

Trước khi cập nhật máy đang chạy:

1. Xác nhận đúng thư mục cài đặt và nhánh.
2. Kiểm tra git status; sao lưu thay đổi local và .env riêng.
3. Xem PR/commit hiện tại của nhánh trên GitHub.
4. Chạy preflight và test phù hợp.
5. Chỉ restart khi queue trống và không có stage AI/FFmpeg đang hoạt động.

## Phân biệt service

### Tool V1

- FastAPI dashboard tại **127.0.0.1:8088** theo tài liệu bàn giao V1.
- Dùng launcher/script của riêng nhánh V1.
- Hàng đợi và tiến độ tương ứng với tracker/pipeline V1; không dùng cấu hình V2 như thể chúng tự động áp dụng.

### Tool V2

- **backend/dashboard_monitor.py**: Dashboard Monitor và các route Workflow, thường dùng cổng **8089**.
- **backend/main.py**: API xử lý/editor, cổng **8000**; Electron có thể khởi chạy API trong desktop workflow.
- **frontend/**: React + Vite + Electron; Vite dev server thường dùng **5173**.
- **backend/telegram_bot.py**: bot Telegram; không phải dashboard web.
- **run_batch_edit.bat**: batch processor cục bộ.

### AI Video Research Tool

Ứng dụng có hướng dẫn setup, database và lifecycle riêng. Dùng đúng README/start/stop script trên nhánh nghiên cứu. Cổng backend mặc định của ứng dụng này cũng có thể là **8000**, nên không chạy chồng với API Tool V2 nếu chưa cấu hình cổng khác.

## Dữ liệu không nên commit

Secret, cookie, profile trình duyệt, video đầu vào/đầu ra, workspace, log, cache model và hàng đợi runtime phải được giữ local. Model RVC có thể được quản lý qua Git LFS; các cache AI lớn khác thường được tạo/tải lại theo cấu hình. Đọc file .gitignore và README của nhánh trước khi thêm file.
