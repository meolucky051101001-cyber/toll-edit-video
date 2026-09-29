# Mục lục tài liệu

Tài liệu được nhóm theo thứ tự đọc: chọn phiên bản → cài/chạy → nghiệm thu → tra cứu kỹ thuật.

## 1. Chọn công cụ

- [README tổng quan](../README.md) — tính năng, nhánh, cổng service, cấu trúc repository.
- [README_FIRST](../README_FIRST.md) — checklist cài nhanh.
- [VERSIONS](../VERSIONS.md) — bản đồ nhánh/alias và runtime.

## 2. Cài đặt và vận hành Tool V2

- [TOOL_V2_FULL_GUIDE](../TOOL_V2_FULL_GUIDE.md) — cài môi trường, cấu hình, chạy bot/batch/test.
- [Pipeline V2 rollout](pipeline_v2_rollout.md) — preflight, pipeline mode, QC gate, resume, rollback và kiểm tra production.
- [Code handoff 2.12](../CODE_HANDOFF_2_12.md) — phạm vi bàn giao, bằng chứng test tại thời điểm ghi và giới hạn.
- [Third-party downloader notes](douyin-downloader-third-party.md) — thông tin thư viện/nguồn bên thứ ba.

## 3. Nghiệm thu và bảo toàn baseline

- [Phase 4 acceptance](../PHASE4_ACCEPTANCE.md) — tiêu chí QC/cover và bằng chứng kiểm thử.
- [Phase 5 acceptance](../PHASE5_ACCEPTANCE.md) — adaptive OCR và giới hạn benchmark.
- [V1 baseline](baseline/pipeline_v1.md) — mô tả pipeline đối chiếu V1.
- [Baseline video matrix](baseline/test_video_matrix.json) — fixture/matrix kiểm tra.

## 4. Ghi chú kỹ thuật theo chủ đề

- [Kiến trúc backend](backend_architecture.md) — ranh giới module, đường dẫn, profile CPU/GPU, dependencies phát triển và CI.
- [Pipeline V2 phases 1–2](pipeline_v2_phases_1_2.md)
- [Pipeline V2 review fixes](pipeline_v2_review_fixes.md)
- [Pipeline speed V2.3.6](pipeline_speed_v2_3_6.md)
- [Subtitle layout V2.3.5](subtitle_layout_v2_3_5.md)
- [Subtitle sticker V2.3.4](subtitle_sticker_v2_3_4.md)
- [OCR subtitle detection V2.3.3](ocr_subtitle_detection_v2_3_3.md)
- [Model speed V2.4.0](model_speed_v2_4_0.md)

## 5. Tool V1 và sửa chữa

Tài liệu bàn giao V1 nằm trên nhánh V1 vì nhánh đó có lịch sử/source riêng:

- [Dashboard Monitor Handover](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-v1/DASHBOARD_MONITOR_HANDOVER.md)
- [Hướng dẫn tiện ích sửa V1](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-v1/fixes_for_v1/README_HUONG_DAN.md)

## Ghi chú

Các acceptance/handover ghi lại trạng thái tại thời điểm lập tài liệu. Hãy chạy test/preflight trên commit và máy thực tế trước khi tuyên bố sẵn sàng production. Tài liệu kỹ thuật không thay thế quyền sử dụng media hoặc điều khoản của nền tảng.
