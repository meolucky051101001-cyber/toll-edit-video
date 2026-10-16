# Mục lục tài liệu kỹ thuật Tool V1

Tài liệu được phân loại theo mục đích tra cứu: hướng dẫn vận hành → kỹ thuật & mô hình → lưu trữ lịch sử.

---

## 1. Hướng dẫn & Vận hành Tool V1 (Production)

- [README tổng quan](../README.md) — Kiến trúc tổng thể, 8 bước xử lý tự động, cấu hình model và dịch vụ.
- [README_FIRST](../README_FIRST.md) — Lối vào nhanh, checklist khởi động và kiểm tra sức khỏe.
- [Bản đồ phiên bản](../VERSIONS.md) — Phân định nhánh GitHub và cổng dịch vụ.
- [Bàn giao Dashboard Monitor](DASHBOARD_MONITOR_HANDOVER.md) — Cơ chế giám sát tiến độ và giao diện A2UI Studio.
- [Tài liệu giám sát & A2UI](DOCS_HE_THONG_GIAM_SAT_VA_A2UI.md) — Hướng dẫn chi tiết hệ thống giám sát.
- [Đánh giá & nghiệm thu Tool V1](REVIEW_TOOL_V1_2026-10-15.md) — Kết quả kiểm định độ ổn định, bộ giới hạn tốc độ 1.45x và sửa lỗi phụ đề.

---

## 2. Ghi chú kỹ thuật & Mô hình

- [Kiến trúc Backend](backend_architecture.md) — Ranh giới module, quản lý tài nguyên GPU và tiến trình.
- [Refactor âm thanh V1](v1_audio_refactor_2026-09-20.md) — Cấu trúc bộ trộn âm thanh và chống đè tiếng.
- [Sửa lỗi phụ đề V1](subtitle_v1_fix.md) — Chuẩn hóa layout ASS và thuật toán che phụ đề cũ.
- [Hướng dẫn ChatGPT Review](CHATGPT_REVIEW_GUIDE.md) — Quy chuẩn review mã nguồn.
- [Cấu hình Gemini AI](GEMINI.md) — Cơ chế failover đa key và template prompt.

---

## 3. Tài liệu lưu trữ lịch sử (Archive)

Các tài liệu dưới đây thuộc giai đoạn thử nghiệm trước đây, được lưu trữ phục vụ tham khảo lịch sử:
- [Hướng dẫn Pipeline V2 (Deprecated)](archive/TOOL_V2_FULL_GUIDE.md)
- [Code Handoff 2.12](archive/CODE_HANDOFF_2_12.md)
- [Phase 4 Acceptance](archive/PHASE4_ACCEPTANCE.md)
- [Phase 5 Acceptance](archive/PHASE5_ACCEPTANCE.md)
