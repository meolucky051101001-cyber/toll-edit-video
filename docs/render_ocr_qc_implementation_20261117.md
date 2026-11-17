# Kết quả sửa render, OCR và QC — 17/11/2026

Hàng đợi đã được dừng theo yêu cầu: utility `backend.v1_shutdown` xác minh và dừng hai tiến trình Telegram V1 (PID 8732, 36664), trả `exit_code=0`, không còn survivor. Preview worker sau đó không chọn tiến trình nào. Cờ tạm ngưng xử lý/ngăn tự khởi động lại đã được đặt với xác nhận của người dùng. Chưa bật lại dịch vụ, chưa đưa hàng đợi chạy tiếp. Bản ghi này mô tả thời điểm triển khai và benchmark trước commit/PR; trạng thái phát hành mới nhất nằm trên PR. Không đọc `.env`, cookie hoặc token thật.

## Thay đổi đã triển khai

- QC là bước 9/9 trên orchestrator, manifest, tracker, Telegram và dashboard một/hai worker. Timer render dừng khi QC bắt đầu. Tiến độ decode/OCR xen kẽ được tổng hợp tăng dần; thông báo thông thường tối đa một lần mỗi 2 giây để tránh thêm chi phí HTTP Telegram cho từng ROI. Chỉ báo 100% sau khi xác minh xuất bản/phân phối thành công.
- Manifest 8 bước chưa xuất bản bổ sung QC pending, giữ các artifact đã xác minh; job cũ đã hoàn tất vẫn được nhận diện là hoàn tất. QC lỗi/thử lại tái sử dụng render, ASR và TTS. Hủy tác vụ truyền qua cầu nối thread/asyncio và không xuất bản.
- QC gom mẫu biên và giữa câu thành lịch tăng dần, decode tiến theo PTS thực, một khung phục vụ nhiều ROI. Không seek; giữ tối đa hai ảnh đầy đủ và batch tối đa 8 crop. PTS lặp/lùi/NaN, giải mã bị cắt hoặc OCR thiếu kết quả không thành PASS. Preroll âm nhỏ ở khung đầu OpenCV/B-frame được quy về mốc hiển thị 0; các khung sau vẫn phải tăng nghiêm ngặt.
- OCR có ledger riêng cho từng lần xử lý video, dùng chung coarse/rescue/source/refinement. Khóa xét nguồn, model/backend/runtime, dtype, shape và hash; vẫn so byte trước khi reuse. Kết quả được deepcopy, còn thời gian/geometry/màu thuộc từng occurrence. QC đầu ra dùng cache batch riêng. Payload cache mặc định tối đa 96 MiB/512 mục, metadata occurrence tối đa 4.096 mục; đây không phải giới hạn tuyệt đối RAM tiến trình. Cache bị xóa khi kết thúc hoặc lỗi.
- Ledger dùng raw byte có giới hạn: thử nén zlib đã bị loại vì ảnh nền chuyển động làm tăng đáng kể thời gian. Metric tách hash/lookup, lưu cache, PNG, runtime, postprocess, số input yêu cầu/đã thử/đã thành công/reuse. `visual_ocr_calls` tính cả refinement.
- Blur có fast path dùng lại crop cùng kích thước khi lifetime không chồng nhau và thứ tự overlay an toàn. Mỗi crop lấy pixel nguồn chưa blur. Chỉ dùng fast path nếu giảm ít nhất 25% số nhánh; các trường hợp khác giữ graph tĩnh. Renderer v6 nhận được geometry ASS v5 và thay signature render, giữ cache OCR/ASR/TTS. AutoDub/Studio ghi metric graph riêng và giữ đúng hệ tọa độ.

## Số đo và giới hạn

| Kiểm tra | Trước | Sau | Phạm vi |
| --- | ---: | ---: | --- |
| Trích 195 mẫu QC trên video thành phẩm 23,766667 giây, 713 khung | 53,0042 giây, 195 seek | 3,5663 giây, 0 seek, đủ 195 mẫu | Một cặp chạy CPU; giảm 93,27%; chưa gồm inference OCR |
| Overhead cache với 4 input 960×1706 khác nhau, model giả trả ngay | Nén thử: 0,609 giây chỉ để lưu | Raw byte: trung vị 0,0228 giây toàn lookup/lưu | 3 lần raw, payload 19.653.128 byte |
| Blur tổng hợp 320×180, 10 giây | 283 nhánh, 3,094 giây | 17 slot, 0,157 giây | CPU một thread; output YUV giống từng byte |
| Geometry baseline trên source tổng hợp 320×568, 10 giây | 101 nhánh, 2,547 giây | 91 slot, 2,765 giây | Chậm hơn nên production chọn graph tĩnh |

Clip baseline 65,833 giây có 289 rectangle và 238 cặp kích thước khác nhau. Fast path an toàn không đủ giảm nhánh, nên clip này vẫn dùng graph tĩnh. **Mục tiêu 289 → 1 nhánh và giảm render baseline ít nhất 60% chưa đạt.** Thay kích thước crop động sai pixel; phương án loại frame không hoạt động gây framesync buffering (333 MB ngay ở 640×360 cho mask xuất hiện muộn), nên đều không phát hành.

Video baseline cũ không còn tại thư mục xuất khi chạy benchmark cuối; không tuyên bố số đo QC trên clip 23,77 giây là đo lại baseline. Chưa chạy acceptance RTX 4050 đầy đủ OCR/render/QC cold/warm. Không cam kết giảm số inference với nền luôn chuyển động: ledger chỉ reuse input giống hệt. Chưa bật bộ theo dõi bỏ nhận dạng theo độ giống ảnh, chưa nới gap nguồn để giảm công việc. Các test A–B–A/A–empty–A ở ledger chứng minh reuse không nuốt thay đổi đã quan sát; không chứng minh bộ lấy mẫu hiện tại thấy mọi caption một khung.

Số đo máy đọc được: `qc_ocr_benchmark.json`, `blur_synthetic_benchmark.json`, `blur_real_geometry_benchmark.json`. Script QC/cache nằm tại `scratch/benchmark_qc_ocr.py`.

## Kiểm chứng

Các test liên quan bao gồm lossless Y/U/V cho blur tại 30/60 fps, VFR, biên một khung, gap, resize, vùng nhỏ/sát mép, chồng nhau; decoder QC thực với VFR; cache collision/dtype/policy/cách ly/eviction/hủy; pipeline coarse/rescue/source dùng chung ledger; manifest cũ, QC retry/cancel, tiến độ/timer hai worker.

Node: 20/20 test trong glob CI đã qua. Bộ Python 3.12 theo danh sách CPU CI cộng regression OCR/QC mới có JUnit xác nhận 527 test, 0 failure, 0 error, 0 skip (2 cảnh báo thư viện có sẵn); `pytest.main` trả mã 0, lưu tại `scratch/performance_checks_exit.json`. Wrapper môi trường vẫn báo mã thoát 1 sau khi pytest đã trả 0; nguyên nhân thoát interpreter/wrapper chưa được xác định, không coi toàn tiến trình CLI là mã 0. Nhóm xác nhận trực tiếp cuối có 90 test qua và CLI mã 0. Nhóm Python 3.10 OCR/visual/cache báo 90 test và 11 subtest qua; 12 file sản phẩm sửa được parse bằng ngữ pháp Python 3.10. Golden orchestrator 8/8 qua, nhóm tracker/manifest/Telegram/checkpoint 19/19 qua. `git diff --check` sạch, chỉ có thông báo chuẩn hóa CRLF.

Windows sandbox có thể chặn TCP socketpair lúc tạo asyncio trước cả `setUp`; lần kiểm tra rộng đầu tiên chạy ngoài sandbox thực tế có 522 test qua và một test kỳ vọng batch cũ lỗi, đã sửa và xác nhận riêng. Bộ kiểm tra cuối và golden chạy qua launcher polling riêng trong scratch; production và thư viện chuẩn không thay đổi. Launcher này chỉ hỗ trợ môi trường test bị hạn chế và không được coi là benchmark runtime. Các lần xét duyệt quyền tự động để chạy lại ngoài sandbox đã hết thời gian; không thao tác dịch vụ nào trong lượt test.

## Phạm vi phát hành

Thay đổi được đóng gói trong PR hiệu năng riêng, dựa trên main và bao gồm hai commit OCR batch/QC của PR #28 cùng các precursor narration/geometry cục bộ cần thiết; không bao gồm tích hợp Cloud từ runtime. Việc cập nhật runtime giữ các cờ tạm dừng và chỉ cho phép khởi động lại hàng đợi bằng lệnh Start rõ ràng. Cần acceptance GPU đầy đủ ở ranh giới job trước khi coi toàn bộ mục tiêu tối ưu render/OCR đã nghiệm thu. Phần QC tuần tự và hiển thị 9/9 đã có kiểm chứng cụ thể; giới hạn blur baseline được giữ rõ ở trên.


## Kiểm tra trên nhánh PR tách riêng

Nhánh PR dựa trên `main`, bao gồm hai commit OCR batch/QC của PR #28 cùng precursor narration/geometry cục bộ và các thay đổi hiệu năng ở trên; không kéo mã Cloud từ runtime. Workflow CPU bổ sung regression ledger, QC stage, progress và bootstrap pause. Bộ Python 3.12 trực tiếp chạy theo workflow mới trả exit 0: 552 passed, 3 skip quarantine có sẵn; Node 20/20. Python 3.10 local dùng OpenCV 4.8 (CI pin 4.10) phát hiện seek thời gian ở đuôi VFR có thể thất bại; đường phục hồi đọc tuần tự có giới hạn dựa trên PTS thật giữ đủ khung. QC vẫn chặn độc lập khi OCR confidence >=0,90 nằm trong >=50% vùng caption đã xác minh, kể cả khi spelling khác source; packaging ngoài bbox chỉ cảnh báo. Các chỉnh cuối: nhóm OCR/timing/QC có 108 test qua trên mỗi Python; Python 3.10 exit 0 (kèm 15 subtest), Python 3.12 wrapper trả exit 1 sau summary xanh như giới hạn môi trường đã ghi ở trên. Hai regression cuối riêng chạy Python 3.12 exit 0.
