# V2 Desktop Frontend

Giao diện desktop cục bộ được xây bằng React, Vite và Electron. Đây không phải frontend Next.js.

## Chức năng

- Chọn thư mục hoặc video qua Electron preload bridge.
- Xem video, điều khiển phát/tua và biên tập phụ đề trên giao diện.
- Chọn nguồn/giọng đọc ở các luồng giao diện hiện có.
- Xem log từ API cục bộ.
- Electron bật context isolation, sandbox và Node integration tắt; quyền chọn file được giới hạn qua preload.

## Phát triển

Yêu cầu Node.js LTS tương thích với phiên bản Vite trong package lock.

~~~powershell
cd frontend
npm ci
npm run dev
~~~

Lệnh dev mở Vite và Electron. Trong chế độ dev, Electron kết nối backend cục bộ theo cấu hình trong electron-main.cjs; API desktop mặc định dùng 127.0.0.1:8000. Dashboard Monitor V2 ở cổng 8089 là service riêng.

~~~powershell
npm run lint
npm run build
~~~

Chạy các lệnh tại đúng thư mục frontend. Không commit node_modules, dist, log, video hoặc file cấu hình chứa secret.
