---
trigger: always_on
description: Quy định an toàn tuyệt đối khi dọn dẹp ổ đĩa và quản lý file của người dùng
---

# 🛑 QUY ĐỊNH AN TOÀN TUYỆT ĐỐI VỀ DỌN DẸP Ổ ĐĨA & TẬP TIN (DISK CLEANUP SAFETY RULE)

1. **CHỈ DỌN RÁC KHI ĐƯỢC YÊU CẦU & HỎI Ý KIẾN TRỰC TIẾP:**
   - Trợ lý AI TUYỆT ĐỐI KHÔNG được tự ý dọn dẹp, xóa bỏ, di chuyển hay dọn dẹp ổ đĩa/thư mục trừ khi NGƯỜI DÙNG CHỦ ĐỘNG YÊU CẦU hoặc HỎI.
   - Khi người dùng yêu cầu dọn rác, AI phải liệt kê rõ ràng danh sách các file/thư mục tạm thời dự kiến xóa và hỏi xác nhận từ người dùng.

2. **NGHIÊM CẤM ĐỘNG VÀO CÁC FILE KHÁC TRÊN MÁY:**
   - TUYỆT ĐỐI NGHIÊM CẤM xóa, sửa đổi, di chuyển hoặc can thiệp vào:
     + Toàn bộ file cá nhân, video gốc, ảnh, tài liệu trên ổ `D:\`, `C:\`, Desktop, Downloads, v.v.
     + Các thư mục dữ liệu của người dùng như `D:\banve`, `D:\video_input`, `D:\*`.
     + Các file mã nguồn, dữ liệu dự án quan trọng ngoài các file rác được chỉ định.
   - Chỉ được phép thao tác dọn dẹp trên các file tạm rác đã được người dùng đồng ý duyệt.
