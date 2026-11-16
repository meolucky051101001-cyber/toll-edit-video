# Bàn giao Tool V1 sang giai đoạn Cloud — 14/11/2026

## Phạm vi người dùng yêu cầu

Hoàn tất bàn giao và kế hoạch Cloud, khởi động lại Tool V1 với mã local mới nhất,
commit và tạo PR. Chưa triển khai sửa Cloud hoặc chạy job GPU Cloud.

## Đường dẫn và nhánh

| Thành phần | Vị trí / trạng thái |
|---|---|
| Production local | `C:\tool v1`, nhánh `tool-v1` |
| Base trước bàn giao | `4b2d234acc92fead3fc097e570de523089290aa4` |
| Repo GitHub | `meolucky051101001-cyber/toll-edit-video`, private |
| PR nền đang mở | #16, `tool-v1` -> `main`; đợt này không merge PR này |
| Nhánh bàn giao | `codex/toolv1-local-cloud-handoff-20261114` |
| Checkout để commit | `C:\tool_v1_clean\local-cloud-handoff-20261114` |
| Workspace V1 / CapCut | `D:\workspace` / `D:\CapCut`, giữ nguyên |
| Cloud thử nghiệm | `C:\tool v1\backend\cloud`, chưa đưa vào PR local |
| Runner Cloud shortcut | `start_cloud_dashboard.bat` trỏ Updates, cổng 8089 |
| Updates | `C:\Projects\Tools\Tool V1\Updates`; chưa xác minh lại working tree trong đợt này |

Production vẫn giữ các thay đổi chưa commit của người dùng. Commit được tạo trong
checkout riêng; không chuyển nhánh, reset, pull hoặc merge thư mục đang chạy.
Không coi production là checkout sạch của commit PR: Telegram/Cloud thử nghiệm
vẫn tồn tại tại production và được liệt kê riêng bên dưới.

## Nội dung local được bàn giao

- OCR chỉ thay lời ASR khi đủ độ tương đồng và độ dài; giữ các chốt confidence,
  không cắt câu bằng mảnh OCR ngắn. Có kiểm thử trường hợp chữ trên hình không
  liên quan, câu gần đúng, câu ngắn và nhiều dòng OCR.
- Cấu hình `V1_QC_VISUAL_PIXEL_CHECK` hợp lệ trở lại trong working tree production.
  Base Git đã có cờ này, nên checkout PR giữ nguyên cờ hợp lệ và thêm test hồi quy;
  không đưa hai cờ Cloud thử nghiệm vào commit.
- Dual voice có nhiều cue nhưng chỉ một vai được nhận diện sẽ yêu cầu rà giọng,
  kể cả khi confidence cao. Metadata và thông báo tiếng Việt được đưa tới editor.
  Không đổi vai tự động, không viết lại voice lock của job cũ, không dừng pipeline.
- Bản local pending từ trước cũng được đưa vào snapshot PR: điều chỉnh vùng che ASS,
  cứu vùng phụ đề OCR, lấy DLL ONNX từ môi trường phù hợp và geometry fallback.
  Đây là các thay đổi đã có trước lượt sửa guard; không mô tả chúng là đã nghiệm thu.
- Test mới và hai tài liệu bàn giao/kế hoạch Cloud.

Ngoài PR: `backend/cloud/`, `backend/templates/cloud_dashboard.html`,
`start_cloud_dashboard.bat`, wiring Cloud trong `backend/telegram_bot.py`,
hai cờ Cloud thử nghiệm trong `backend/v1_feature_flags.py`, dữ liệu chạy, video,
DB, `.env`, cookie/token và bản sao lưu. Các file này được giữ nguyên tại production.

## Kết quả kiểm thử và điều kiện merge

- Bộ trọng tâm: 58 passed, exit 0; OCR correction/acquisition, QC policy,
  dual voice, voice cache và pixel cover QC.
- Sau chỉnh thông báo: 15 test dual voice/cache passed, exit 0; đây là tập con.
- Bộ mở rộng trên production working tree: **168 passed, 2 skipped, 5 failed**.
- Đối chứng năm test lỗi bằng các file trước lượt sửa guard từ backup, trong
  checkout riêng: **cùng cả 5 test thất bại**. Đối chứng này là working tree
  trước sửa, không phải bằng chứng rằng commit Git nền cũng có các lỗi đó.
- Kiểm thử phạm vi restart: **34 passed**, gồm loại downloader, chặn khi busy,
  kiểm tra identity tiến trình/task và preview không gây thay đổi.
- Tạo PR **draft** vì các vấn đề local bên dưới còn mở. Không merge hoặc coi là
  nghiệm thu toàn hệ thống chỉ vì bộ 58 test đạt.

| Việc cần xử lý trước khi chốt local/Cloud | Test đang lỗi | Hướng kiểm chứng |
|---|---|---|
| Không che xuyên khoảng đã xác minh không có chữ nguồn | `test_ocr_geometry_audit.py::SourceMaskAuditTests::test_verified_absence_does_not_bridge_or_hold_mask` | Tách nền chữ dịch với mask nguồn; xác định khoảng thực sự cần nền |
| Không hồi sinh vùng che khi tracking xác minh rỗng | `test_ocr_geometry_audit.py::SourceMaskAuditTests::test_verified_empty_tracks_never_resurrect_best_block` | Không dùng best_block cũ để phủ hết cue khi dữ liệu phủ định còn hợp lệ |
| Không nhận nhãn lặp không liên quan làm phụ đề | `test_ass_cover_dimensions.py::PhaseFourCoverTests::test_repeated_unmatched_label_cannot_use_bracket_recovery` | Phân biệt caption với packaging/watermark; không truyền anchor qua nhãn lặp |
| Không mất sample chữ dịch chuyển theo thời gian | `test_subtitle_tracking_regression.py::SubtitleRegressionTests::test_frames_same_segment_are_not_composited_across_time` | Kiểm tra bộ lọc tâm X không loại observation hợp lệ ở thời điểm khác |
| Geometry chưa xác minh phải báo lỗi sau khi ghi audit | `test_v1_ocr_geometry_repair.py::PipelineTests::test_report_saved_before_unresolved_error` | Không biến fallback suy đoán thành vùng đã xác minh |

Không sửa/xóa các test này để che lỗi. Nếu yêu cầu sản phẩm về nền phụ đề đã đổi,
phải tách rõ mask nguồn và nền chữ dịch, thêm bằng chứng render tương ứng rồi mới
điều chỉnh hợp đồng test. Chưa chạy render thực tế hoặc gọi dịch vụ trả phí.

## Vận hành

Trước restart: dashboard idle/active=false; hàng đợi Telegram available=true,
0 item và không active_job; bot busy=false, polling=true; Cloud mode=false.
Preview scope `all`, `include_downloader=False` chọn 16 tiến trình V1, không có
downloader. Sau lần auto-review từ chối vì thiếu chứng minh rõ phạm vi, đã bổ sung
chốt kiểm tra PID/role downloader và chạy 34 test trước khi yêu cầu lại.

Kết quả restart và hậu kiểm được ghi trong phần cập nhật vận hành ở cuối tài liệu.
Chỉ khôi phục những Scheduled Task đã chạy và đã được helper dừng; không tự bật
watchdog vốn ở trạng thái Ready, không dừng ứng dụng Facebook/YouTube/CapCut.

Backup trước lượt sửa guard (có cả thay đổi người dùng):
`C:\tool v1\scratch\before-local-review-fixes-20261114-152322`.
Evidence restart đặt trong `C:\tool v1\scratch\restart-handoff-*`, gồm inventory,
hash source, preview, kết quả dừng và khôi phục task; không đưa dữ liệu runtime vào Git.

## Điểm bắt đầu cho người tiếp nhận Cloud

1. Đọc tài liệu này và [kế hoạch triển khai](CLOUD_IMPLEMENTATION_PLAN_20261114.md).
2. Xử lý năm vấn đề local hoặc chốt rõ hợp đồng sản phẩm bằng test/render.
3. Kiểm tra lại trạng thái tiến trình, queue, Git và đường dẫn thực; thông tin trong
   bàn giao là snapshot, không thay thế kiểm tra tại thời điểm làm tiếp.
4. Đối chiếu Cloud code giữa production và Updates; chọn một nơi phát triển và
   branch riêng. Không sửa nhầm runner khác với module đang được kiểm thử.
5. Ưu tiên command safety -> lifecycle/cancel -> ledger -> QC/output -> UI -> canary.
   Giữ Cloud mặc định tắt đến khi đủ điều kiện nghiệm thu trong kế hoạch.

## Các giới hạn còn nguyên

Dual voice vẫn dựa vào cao độ, chưa bảo đảm nam cao/nữ trầm đúng vai; warning mới
chỉ giúp rà lại trường hợp nghi ngờ. Video mẫu đã được người dùng nghe xác nhận
không phải bộ đánh giá tổng quát. Chưa kiểm tra lại toàn bộ ba tool khác trong đợt
bàn giao này; không suy trạng thái của chúng từ health Tool V1.

## Cập nhật vận hành sau restart

- Restart thành công lúc khoảng 15:40 ngày 14/11/2026, giờ máy Asia/Bangkok.
- Helper dừng 16 tiến trình V1 đã xác minh, không force, không có survivor.
- Khôi phục 4 task đang chạy trước đó: AutoDub-control, AutoDubV1-telegram,
  AutoDubV1-scriptbot, AutoDubV1-dashboard. Watchdog không tự bật thêm.
- Hậu kiểm: health 5/5 PASS, Python 3.12.14, pip check sạch, preflight 25/25,
  dashboard idle/active=false, control TCP listening (không có HTTP health endpoint).
- Bộ 58 test được chạy lại trên đúng checkout PR riêng: 58 passed, exit 0.
- Evidence: `C:\tool v1\scratch\restart-handoff-20261114-154039`.
- Không chạy render hoặc job Cloud thật trong đợt này.
