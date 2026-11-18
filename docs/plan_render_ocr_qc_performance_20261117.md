# Kế hoạch tối ưu render, OCR và QC — 17/11/2026

Phạm vi là pipeline cục bộ tại `C:\tool v1`: giảm nhánh blur, đọc khung QC tuần tự, giảm OCR lặp và hiển thị QC riêng. Kết quả triển khai và giới hạn nghiệm thu được ghi tại `render_ocr_qc_implementation_20261117.md`. Runtime đã được dừng theo yêu cầu; GitHub tiếp tục tạm dừng.

## Cơ sở đo và nguyên tắc nghiệm thu

Baseline lấy từ job `telegram_4f13d5a185944f31a53786fb3d1e096c`, video 65,833 giây, 1080×1920, 30 fps, 1.975 khung và 18 câu thoại.

| Hạng mục | Baseline |
| --- | --- |
| OCR | 378,17 giây; 165 lượt, 1.308 input khung; log báo reuse 0 |
| Nhận dạng trong các batch OCR | Tổng 325,09 giây |
| Quét timeline nguồn | 1.278 observation; 1.089 khung refinement; 1.248 tracking box |
| Render NVENC | 602,20 giây, xấp xỉ 3,28 fps |
| Graph blur | 289 hình chữ nhật khác nhau, split 290 nhánh, 289 crop/blur và overlay |
| Số vùng blur hoạt động đồng thời | Tối đa 1 trên clip baseline |
| QC | 384,95 giây; 195 mẫu, khoảng 333,6 giây trước lượt OCR hậu kiểm và 50,22 giây nhận dạng |
| Keyframe video đầu ra | 8 keyframe, cách nhau 8,333 giây |

Mọi tối ưu phải giữ đúng thời gian và tọa độ nguồn, khoảng không có chữ, các vùng rời/chồng nhau và chất lượng che chữ. Đọc PTS thực từ decoder; không suy diễn mọi khung bằng FPS trung bình. QC vẫn đọc pixel đầu ra sau render. Các kết quả chưa xác minh, lỗi hoặc hủy không được chuyển thành PASS.

Hiện có thay đổi chưa commit ở `backend/ocr_utils.py`, `backend/visual_caption_timing.py`, `backend/v1_checkpoint.py` và `backend/v1_quality_gate.py`. Cần giữ và rà soát chúng trước khi ghép sửa: batching 8 ảnh, giảm seek gần, tái sử dụng khung QC cùng thời điểm và giữ nguyên duration render đã hoàn tất. Các phần này chưa giải quyết graph blur 289 nhánh hay việc QC vẫn seek tới từng thời điểm.

## 1. Tách QC thành bước 9/9 và bổ sung đo thời gian

Làm trước để các lần benchmark sau không cộng QC vào render.

- `backend/v1_orchestrator.py`: phát sự kiện có cấu trúc `stage=quality_gate`, `step=9`, `total_steps=9` trước QC; ghi start/complete/failure riêng. Bước 8 dừng timer khi render kết thúc. QC có tiến độ con trích khung và OCR hậu kiểm. Chỉ báo toàn job 100% sau QC cho phép xuất bản và phân phối thành công.
- `backend/v1_checkpoint.py`: thêm stage QC; migration manifest 8 bước đang xử lý thành QC pending, giữ nguyên render artifact và duration. Job cũ đã xuất bản vẫn được nhận diện là hoàn tất, không tự chạy lại. QC lỗi/thử lại dùng render đã xác thực; không tạo lại ASR/TTS chỉ vì thêm bước QC.
- `backend/job_tracker.py`: thêm trường tùy chọn `stage`, `total_steps`; tách duration bước 8 và 9, reset timer đúng khi đổi stage/job. Caller cũ và payload 8 bước tiếp tục đọc được.
- `backend/batch_processor.py`, `backend/telegram_bot.py`: truyền đủ stage/total_steps qua callback. `backend/telegram_progress.py`: ưu tiên trường có cấu trúc; parser dự phòng nhận QC trước từ khóa OCR để “OCR hậu kiểm QC” không quay về bước 4.
- `backend/templates/dashboard.html`: thêm QC cho cả chế độ một worker và hai worker; cập nhật reset, ánh xạ trạng thái, màu lỗi và thống kê thời gian. ETA dùng tiến độ của stage; khi chưa đủ dữ liệu thì hiển thị chưa xác định.

Nghiệm thu: bước 8 completed/bước 9 active; QC fail không hiện thành công; timer render không tăng khi QC chạy hoặc khi cập nhật đường dẫn artifact sau publish. Kiểm tra resume manifest cũ, retry QC, reset job mới và trạng thái độc lập giữa hai worker. Test dashboard mới đặt trong `tests/js/*.test.js`; test `.cjs` bảy bước cũ hiện không nằm trong glob Node CI.

## 2. Đọc khung QC tuần tự

Thay vòng `VideoCapture.set(...); read()` cho từng mẫu trong `backend/v1_quality_gate.py` bằng một lịch yêu cầu chung, tăng dần theo thời gian nguồn.

- Giữ tập mẫu biên và giữa câu hiện có; gom các yêu cầu rơi vào cùng khung nhưng giữ riêng ROI và metadata của từng yêu cầu.
- Decode tiến về phía trước một lần, dùng PTS/ordinal thật để chọn khung đang hiển thị tại thời điểm yêu cầu. Tái sử dụng một khung cho nhiều crop; không seek lùi chỉ vì danh sách mẫu giữa câu được nối sau danh sách mẫu biên.
- Bỏ qua chuyển đổi ảnh không cần thiết cho khung không lấy mẫu. Chỉ giữ khung hiện tại và hàng đợi crop có giới hạn; OCR hậu kiểm theo batch phù hợp VRAM.
- Cache QC tách khỏi OCR nguồn, gắn với fingerprint video đã render. Không dùng kết quả nhận dạng nguồn để tự chứng minh đầu ra đã che chữ.
- Ghi số khung decode, số seek, số thời điểm/khung duy nhất, số ROI, thời gian decode/crop/OCR và tổng QC.

Nghiệm thu: cùng tập mẫu và ROI, trạng thái PASS/WARN/FAIL tương đương baseline; mẫu không bị mất do sắp xếp/gom trùng. Test VFR, nhiều ROI cùng PTS, cuối video, lỗi decoder, hủy và retry. Trên clip baseline, số seek phải độc lập với 195 yêu cầu và đường decode chính là một lượt tiến tới mẫu cuối.

## 3. Giảm graph blur theo số vùng đồng thời

Giữ nguyên hình học từ `combine_blur_boxes()` và `blur_regions()`; đổi cách thực thi trong `backend/subtitle_blur.py`.

Thiết kế chính cần thử nghiệm: tái sử dụng K slot crop/blur/overlay theo lịch, với K là số vùng cần hoạt động đồng thời và thứ tự overlay phải được bảo toàn. Mỗi slot lấy pixel từ nguồn chưa blur. Cập nhật x/y/w/h và bật/tắt theo PTS với khoảng `[start,end)`. Với clip baseline, mục tiêu là 1 slot hoạt động, split 2 nhánh thay vì 290.

Điểm cần chứng minh trước khi tích hợp: FFmpeg 8.1.2 có runtime commands cho crop và vị trí overlay, nhưng boxblur radius không có runtime command. Đổi kích thước crop có thể gây vấn đề radius clamp, renegotiation hoặc framesync. Slot planner cũng phải giữ thứ tự khi padding tạo vùng chồng nhau; không chỉ phân slot theo khoảng thời gian.

Thử nghiệm nhỏ phải so pixel Y/U/V lossless với graph cũ tại mọi khung: vùng di chuyển/đổi kích thước, gap một khung, biên phân số 30/60 fps, VFR, vùng rất nhỏ/sát mép, nhiều vùng và chồng nhau. Ngoài mask phải giống hệt nguồn; trong mask phải giữ hiệu ứng baseline. Nếu chưa đạt, chưa thay graph mặc định. Phương án dự phòng cần đánh giá riêng là render theo các cửa sổ thời gian với graph giới hạn nhánh, kiểm tra nối PTS/audio trước khi sử dụng.

Tích hợp sau khi prototype đạt:

- `backend/video_utils.py`: AutoDub sử dụng planner mới, giữ NVENC và thông số xuất.
- `backend/workflow_api.py`: Studio có vùng nguồn trước crop/mirror/scale và vùng chữ trên canvas cuối; giữ đúng hệ tọa độ và command target riêng.
- Tăng phiên bản renderer/signature để bỏ cache render cũ; xử lý metadata ASS cũ rõ ràng. Thay renderer đơn thuần tái sử dụng OCR/ASR/TTS đã xác thực.
- Cloud có đường render khác, hiện chưa dùng helper blur chung; không coi thay đổi cục bộ là đã cập nhật Cloud.

Nghiệm thu cấu trúc: số bộ lọc không tăng theo 289 tọa độ lịch sử. Với baseline K=1, chỉ có một pipeline blur/overlay tái sử dụng. Benchmark cả cấu hình nhiều vùng chồng nhau để không đánh đổi tính đúng lấy số nhánh thấp.

## 4. Giảm OCR lặp và lượng nhận dạng cần thiết

Giữ batching 8 ảnh, decoder cho khung gần nhau và cache exact-frame hiện có. Chia công việc thành hai mức để đo được phần cải thiện có căn cứ.

Mức đầu: ledger observation dùng chung cho các pass coarse/source/refinement của một video. Khóa gồm fingerprint nguồn, stream, ordinal/PTS thực, crop/scale, preprocessing và model version. Dùng hash để tra cứu và xác nhận input giống byte trước khi reuse. Kết quả nhận dạng có thể dùng lại; thời điểm xuất hiện, geometry và màu nền vẫn thuộc từng occurrence. Cache có giới hạn bộ nhớ, không lưu toàn bộ khung 1080p.

Mức tiếp theo: thử nghiệm bộ theo dõi thay đổi để tránh nhận dạng lại caption đã được xác minh. Quét kiểm tra nhẹ trên mọi khung hiển thị của toàn timeline, kể cả ngoài ASR; gom candidate thay đổi rồi OCR theo batch. Không dùng hai đầu A–A để kết luận phần giữa không có B/không có chữ. ROI hoặc pHash chỉ gợi ý candidate; đổi chữ, thêm dòng, nhảy vị trí hoặc tín hiệu không đủ chắc phải quay về nhận dạng đầy đủ. Không đơn giản nới gap 3 khung hay bỏ pass nguồn để đạt tốc độ.

Tách metrics decode/seek/hash/PNG/IPC/model/postprocess; ghi requested frames, unique input, cache hit và fallback. Giảm batching/IPC không được báo nhầm là giảm số khung OCR. Sửa số liệu `visual_ocr_calls` nếu hiện chỉ phản ánh một pass.

Nghiệm thu: A–B–A và A–empty–A với B dài 1–2 khung tại mọi vị trí; thay một ký tự nhỏ, tương phản thấp, thêm dòng, di chuyển ngoài ROI, chữ ngoài đoạn ASR và trong im lặng. So trạng thái/geometry theo từng khung, sai lệch biên tối đa một khung hiển thị thực. Test VFR, decoder trả lại khung cũ, PTS thiếu/lặp/không tăng, hết budget, timeout và hủy. Test gap 3 hiện có chưa đủ chứng minh không bỏ chữ một khung.

Nếu bộ theo dõi chưa qua các ca này, chỉ phát hành ledger/batching và giữ đường OCR đầy đủ. Chưa cam kết tỷ lệ giảm inference trên mọi video nền chuyển động trước khi có kết quả thử nghiệm.

## Thứ tự triển khai và kết quả cần bàn giao

1. Chốt snapshot baseline, giữ các thay đổi đang có; hoàn thiện metric và QC 9/9.
2. Triển khai QC tuần tự và test tương đương; đo riêng thời gian decode và inference.
3. Prototype blur ít nhánh, qua pixel tests rồi tích hợp AutoDub/Studio và cache invalidation.
4. Bổ sung ledger OCR; bộ theo dõi thay đổi chỉ bật sau khi qua test caption ngắn và geometry.
5. Chạy các nhóm regression liên quan, sau đó bộ kiểm tra Python/Node theo cấu hình dự án ở cục bộ. GitHub vẫn chờ yêu cầu tiếp tục của người dùng.
6. Benchmark tuần tự trên cùng RTX 4050: clip baseline, clip chữ ổn định, clip đổi chữ nhanh/có khoảng trống, VFR và clip nhiều vùng. Dùng output thử nghiệm riêng; không chạy benchmark GPU song song với queue thật. So cold/warm trong điều kiện tương đương và lấy trung vị các lần chạy.

Mục tiêu thử nghiệm ban đầu trên clip baseline: render giảm ít nhất 60% so với 602,2 giây; phần chuẩn bị/trích khung QC giảm ít nhất 70% so với khoảng 333,6 giây. Đây là mục tiêu để đánh giá thiết kế, chưa phải kết quả đã đạt. OCR báo riêng mức giảm input/lượt IPC/thời gian thực tế; không đặt ngưỡng số frame làm lý do bỏ kiểm tra chất lượng.

Bàn giao gồm diff theo từng phần, test kết quả, bảng trước/sau (OCR/render/QC/tổng, nhánh blur, số khung, RAM/VRAM), mẫu kiểm tra biên và cơ chế quay lại đường cũ. Chỉ áp dụng runtime sau nghiệm thu, tại ranh giới job; không thay module hoặc khởi động lại giữa một job đang chạy.
