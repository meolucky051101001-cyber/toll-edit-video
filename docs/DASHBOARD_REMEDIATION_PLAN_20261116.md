**Kế hoạch xử lý lỗi sau review Dashboard Tool V1 — 16/11/2026**

Tài liệu này là kế hoạch triển khai, chưa phải báo cáo các lỗi đã sửa. Phạm vi là chín phát hiện trong lượt review gần nhất và năm regression OCR đã có trong baseline. Mục tiêu là sửa đúng nguyên nhân, thêm bằng chứng chống tái phát và nghiệm thu các luồng người dùng đang bị kẹt.

Worktree thực hiện: `C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1`. Commit nền đã review: `4b2d234acc92fead3fc097e570de523089290aa4`. Lượt review thấy 54 file tracked thay đổi, 28 mục untracked; suite dashboard 322 passed, 7 skipped, JavaScript 5 passed, năm test OCR đã biết vẫn failed. Các con số này là mốc đối chiếu, không phải kết quả sau sửa.

**Phạm vi được chốt như sau.**

| Mã | Lỗi phải đóng | Kết quả người dùng cần thấy |
|---|---|---|
| F1 | A nhả lease rồi ghi đè snapshot của B | Chuyển job luôn giữ đúng execution, tiến độ và quyền điều khiển |
| F2 | Cloud ghép shell command và log Gemini key | Đầu vào được truyền như dữ liệu; secret không vào command/log |
| F3 | Transfer lỗi hoặc file dở vẫn được công nhận | Chỉ output xác minh hợp lệ mới được công bố; không làm hỏng output cũ |
| F4 | Job cloud đã hủy bị cập nhật lại completed | Hủy có điểm phân định rõ với publication; callback cũ không hồi sinh job |
| F5 | Retry batch prepared dùng cache và không gọi lại API | Thử lại khi hết bận chạy đúng batch cũ, không tạo bản sao |
| F6 | Hủy lịch để lại reservation không có đường xử lý trên UI | Có thể chạy batch đã lưu, đặt lịch lại hoặc bỏ batch ngay trên dashboard |
| F7 | Telegram local_file không dispatch/mất video_path | File local đi qua queue và restore đầy đủ, không mất job |
| F8 | Cloud thành công nhưng tracker ghi thất bại | Tracker, queue và thông báo cùng ghi nhận kết quả đã xác minh |
| F9 | Pause A còn lại làm sai trạng thái B | Pause/resume chỉ tác động execution đang được người dùng nhìn thấy |
| O1–O5 | Năm lỗi OCR trong biên bản cũ | Mask đúng thời gian/vị trí, không dùng vùng suy đoán như đã xác minh |

Hai worker thực sự vẫn là gói W9 riêng. Trong đợt này giữ `MAX_SUPPORTED_WORKERS=1`; chỉ kiểm thử khả năng tương thích với registry foundation hiện có. Việc sửa ba lỗi cloud không đồng nghĩa đã hoàn thành ngân sách CU, QC dịch/TTS, runner thật hoặc toàn bộ roadmap cloud.

**Thứ tự thực hiện và phụ thuộc.**

| Bước | Công việc | Phụ thuộc | Đầu ra để review |
|---|---|---|---|
| 0 | Chốt baseline và tái hiện | Không | Manifest thay đổi, test thất bại và bằng chứng fixture |
| 1 | Tracker handoff, completion receipt, pause identity | 0 | F1/F9 đóng; hợp đồng finalization dùng chung |
| 2 | Retry batch trên UI | 0 | F5 đóng, giữ idempotency và frozen config |
| 3 | Hủy/khôi phục lịch và reservation | 0, tích hợp với 1–2 | F6 đóng cả dữ liệu mới và lịch cũ |
| 4 | Telegram local_file và restore | 0–1 | F7 đóng với queue thật, provider giả lập |
| 5 | Cloud adapter an toàn và secret transport | 0 | F2 đóng offline; gate CLI thực được ghi rõ |
| 6 | Cloud cancellation, download và publication | 5 | F3/F4 đóng cùng transaction/recovery contract |
| 7 | Nối completion cloud vào Telegram/tracker | 1, 4, 6 | F8 đóng, không báo thành công trước verification |
| 8 | Sửa năm regression OCR | 0; có thể song song | O1–O5 đóng, ảnh/pixel đối chứng |
| 9 | Regression hợp nhất, migration và browser | 1–8 | Evidence đầy đủ trên cùng revision |
| 10 | Soak, canary và phát hành | 9 | Bằng chứng thời gian quan sát và rollback |

Có thể làm song song nhóm tracker, nhóm dashboard, nhóm cloud/OCR nếu phân công rõ file. `main.py`, `telegram_bot.py`, test runner và tài liệu bàn giao phải có một người tích hợp để tránh ghi đè thay đổi. Chốt hợp đồng tracker/completion trước khi tích hợp Cloud Telegram.

**Bước 0 giữ một mốc đối chiếu có thể tái lập.**

- Đọc lại Git status, diff tracked và inventory untracked tại đúng worktree; không reset, stash hoặc gom thay đổi có sẵn.
- Ghi hash mã nguồn và danh sách file thuộc từng gói. Giữ manifest baseline cũ như bằng chứng lịch sử; tạo manifest mới cho đợt sửa, không sửa hash cũ để che thay đổi.
- Chuyển các repro đã có thành test hồi quy: handoff hai process, retry prepared, transfer lỗi có partial file, canceled bị completed.
- Bổ sung test thực thi cho các lỗi trước đó mới xác nhận bằng truy vết mã: local_file dispatch, cloud finalization và stale pause.
- Chạy test bằng workspace/input/output/data tạm, Cloud provider/bot giả lập, không đọc secret thật hoặc chạm queue/dịch vụ production.
- Lưu evidence riêng theo bước: lệnh chạy, thời gian, revision/hash, exit code, JUnit/log và thông tin isolation. Không coi log của revision trước là bằng chứng cho revision mới.
- Bảy skip cũ phải được liệt kê với lý do; không thêm skip/xfail cho các lỗi đang sửa.

Điều kiện qua bước: mỗi F1–F9 có tình huống test hoặc harness tái hiện cụ thể; O1–O5 giữ nguyên năm thất bại hiện tại làm mốc.

**Bước 1 sửa quyền sở hữu tracker và pause như một vòng đời thống nhất.**

Mã trọng tâm: [job_tracker.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/job_tracker.py>), [video_pause.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/video_pause.py>), [main.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/main.py>), các caller trong batch/API/Telegram.

Thiết kế handoff:

1. Dùng cùng khóa liên tiến trình đang được legacy tracker và execution registry chia sẻ. Quy định thứ tự khóa: khóa thread tracker rồi khóa OS. Helper đã nằm trong guard không được mở lại guard không tái nhập.
2. Claim và release phải đọc bản snapshot/lease có thẩm quyền bên trong guard, kiểm tra execution, generation và owner PID/create-time. Không dùng so sánh `updated_at` như bằng chứng quyền sở hữu.
3. Ghi trạng thái terminal bền vững trước khi nhả lease; không lưu lại snapshot A sau khi lease A đã được trả. Nếu B đã sở hữu, callback/release của A không được ghi bất kỳ trường nào của B.
4. Khi ghi terminal thất bại, giữ lease và báo lỗi rõ. Nếu xóa lease thất bại, giữ quyền xử lý bị chiếm và báo `stop_incomplete`; không trả kết quả như đã sẵn sàng nhận job khác.
5. Reader lấy snapshot và lease nhất quán trong guard rồi nhả guard trước ffprobe, đo tài nguyên hoặc công việc chậm. `occupies_slot` phải phản ánh lease đã xác minh.
6. Rà toàn bộ đường nhả lease trong finish/fail/stop/finally để chúng dùng cùng quy tắc. Kiểm tra crash sau claim, trước lưu running, trước terminal save và trước release.
7. Finalization trả bằng chứng thành công rõ ràng cho đúng execution/generation. Với `finish_video`, thay trạng thái “success và stale cùng trả None” bằng receipt xác nhận hoặc lỗi stale riêng. Callback stale không được tạo history/completion mới.

Thiết kế pause:

- GET status truyền execution ID và generation từ chính snapshot vừa đọc vào `video_pause.state`. Không có execution chiếm slot thì không báo pause từ flag cũ; GET không xóa file.
- Pause/resume từ UI gửi expected identity; request từ tab cũ nhận `409 stale_execution`, không tự chuyển sang job mới.
- Thêm cleanup bắt buộc identity, chỉ xóa FLAG/ACK khớp execution/generation/token. Request pause, ACK, resume và cleanup dùng cùng khóa liên tiến trình để tránh đọc cũ rồi xóa flag mới.
- Cleanup chạy sau drain công việc và trước release slot, kể cả fail/cancel không đi qua checkpoint tiếp theo. Stop tại checkpoint cũng chỉ dọn pause của chính execution đó.
- Flag legacy không identity không được áp dụng cho execution mới. Chỉ dọn khi đối soát xác nhận không còn owner sống; giữ nguyên service pause/disable flags.

Test bắt buộc:

| Tình huống | Kết quả phải giữ |
|---|---|
| A release, B nhận ngay bằng hai process và barrier | Snapshot/lease cùng B; strict status không lỗi; không dùng sleep để đoán race |
| Callback terminal/progress A đến sau khi B chạy | B không đổi trạng thái, history hoặc pause |
| Ghi snapshot lỗi; xóa lock lỗi; process chết giữa handoff | Không admission trùng; trạng thái lỗi/recovery rõ; dữ liệu hỏng không bị ghi đè |
| Lease PID bị tái sử dụng hoặc creation time không xác minh được | Không thu hồi nhầm owner sống |
| Pause A → Stop A → B | B running, can_pause=true, can_resume=false |
| Cleanup A tranh với pause/ACK B | Flag và ACK B còn nguyên |
| Resume từ tab A khi B chạy | 409; B không đổi |
| Native work chưa drain | Slot vẫn bị chiếm |
| Registry foundation và legacy tranh admission | Không vượt hợp đồng một execution production |

Điều kiện qua bước: một stress test tối thiểu 1.000 lượt handoff có kiểm soát không sai identity, ngoài test barrier xác định. Stress là bổ sung, không thay test race xác định.

**Bước 2 sửa Retry mà không thay selection hoặc cấu hình đã đóng băng.**

Mã trọng tâm: [dashboard.html](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/templates/dashboard.html>), [batch_plan_service.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/batch_plan_service.py>), API batch trong main.

Mỗi lần bấm Retry phải lấy trạng thái server mới. `intent.result` chỉ phục vụ hiển thị, không quyết định có cần admission lại hay không.

| Kết quả đối soát | Xử lý |
|---|---|
| 404 | POST lại nguyên payload và idempotency key cũ |
| prepared, intent chạy ngay | POST lại `/api/jobs/batch` với nguyên payload/key; backend reuse plan rồi thử admission |
| accepted/running/completed | Kết thúc submission intent; không tạo execution thứ hai |
| scheduled | Mở trạng thái/lựa chọn lịch; không tự release |
| failed/stopped/recovery_required | Hiển thị kết quả/recovery; không tự render lại |
| abandoned sau bước 3 | Báo batch đã bỏ; muốn chạy phải tạo intent mới |
| Timeout/5xx/JSON lỗi/chưa xác minh | Giữ intent/key, cho đối soát lại; không sinh key mới |

Mỗi click có tối đa một request làm thay đổi trạng thái; GET sau lỗi chỉ đối soát, không tạo vòng lặp POST nền. Load trang và polling chỉ đọc. Lưu intent trước lần gửi đầu; reload giữ nguyên nguồn, config, mode và roots. Giữ mã lỗi cụ thể như pipeline_busy, service_disabled, target_reserved để UI giải thích đúng.

Test: busy→prepared→retry→accepted; mất phản hồi trước/sau commit; GET lỗi; reload; double-click; hai tab; cache accepted nhưng server đã failed; nguồn/config cũ không bị freeze lại. Điều kiện đạt: hai click trong repro busy tạo hai lần thử POST cùng key nhưng chỉ một execution; người dùng thao tác hoàn toàn qua dashboard.

**Bước 3 hoàn thiện vòng đời lịch bị hủy và batch giữ chỗ.**

Quyết định sản phẩm: hủy lịch chỉ hủy thời điểm tự chạy, vẫn giữ batch đã xác nhận. Không tự chạy, không tự xóa video và không âm thầm nhả reservation.

Mã trọng tâm: [night_schedule_service.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/night_schedule_service.py>), BatchPlanStore, main và dashboard.

UI của batch giữ chỗ phải có ba thao tác:

| Thao tác | Hợp đồng |
|---|---|
| Chạy batch đã lưu | Dùng Start của batch cũ; giữ items/job IDs/config; busy cho thử lại |
| Đặt lại lịch | Cùng batch, operation mới, schedule ID/generation mới; giữ reservation |
| Bỏ batch đã lưu | Chuyển terminal `abandoned`, nhả reservation, giữ audit/config; không xóa nguồn hoặc chạy pipeline |

Các API bổ sung dự kiến: `POST /api/batches/{batch_id}/abandon`, `GET /api/batches?recoverable=true`, `GET /api/schedule-operations/{operation_id}`. Start hiện có được tái sử dụng.

- Cancel xác định đúng schedule_id/generation; không hủy “lịch hiện tại” bằng request thiếu identity từ tab cũ.
- Start/reschedule/abandon dùng expected_revision và binding lịch. SQLite phải kiểm tra revision/binding, không chỉ state vì reschedule có thể là scheduled→scheduled.
- Abandon chỉ cho batch chưa accepted/chưa có execution và không có lịch active/dispatch chưa đối soát. Replay key cũ trả abandoned, không hồi sinh batch.
- Cancel và claim tranh cùng khóa. Cancel thắng thì không dispatch; claim thắng thì cancel trả already_dispatching và UI không báo hủy thành công.
- Local schedule coordinator thống nhất thứ tự khóa liên tiến trình → schedule lock → transaction plan. Không giữ khóa OS qua await/network/render. Khóa asyncio hiện tại không thay thế khóa giữa process.
- Watcher admission/finish xác minh cả schedule ID và generation. Callback cũ không tác động lịch mới.
- Ledger operation lịch cập nhật ở mọi transition, không chỉ create. Đối soát dùng operation ID chính xác; cùng batch ID chưa đủ chứng minh cùng thao tác.
- Vì JSON schedule và SQLite không chung một transaction, ghi intent/binding bền vững, hỗ trợ reconciliation idempotent. Nếu commit một nửa, giữ reservation và hiển thị needs_review, không auto-dispatch.
- Khi target_reserved, UI liên kết tới batch đang giữ chỗ và các hành động hợp lệ, thay vì lỗi chung.

Xử lý lịch cũ:

| Dữ liệu | Hành động |
|---|---|
| Batch scheduled khớp lịch canceled/expired, chưa dispatch | Đưa vào danh sách batch cần xử lý và mở ba thao tác |
| Lịch canceled cũ đã bị lịch khác thay thế | Vẫn tìm được batch giữ chỗ qua danh sách recoverable |
| Ledger stale nhưng current schedule cùng operation còn đủ bằng chứng | Đối soát ledger từ trạng thái hiện tại |
| Thiếu binding, từng dispatch nhưng thiếu ACK, không rõ owner | needs_review; không tự nhả hoặc tự chạy |
| Đã accepted/running/completed | Hiển thị execution; chặn abandon |

Test API/JS/browser bao gồm: hủy→chạy cũ; hủy→đặt lại; hủy→bỏ→chọn lại nguồn; stale tab; mất ACK cancel rồi tạo lịch mới; replay operation; tranh cancel/claim, abandon/start, reschedule/start; crash giữa các lần ghi. Test giữ nguyên A/B và cấu hình: chọn A thì B không được chạy.

Điều kiện đạt: mọi batch giữ chỗ đã xác minh đều có đường xử lý trên UI sau reload, không cần curl hoặc sửa DB.

**Bước 4 sửa Telegram local_file và dữ liệu queue bền vững.**

Mã trọng tâm: [telegram_bot.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/telegram_bot.py>), [telegram_queue_monitor.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/telegram_queue_monitor.py>), queue normalization/restore.

- Dispatch local_file qua `_process_telegram_job`, cùng admission/context/stop/retry/finally như URL/video; không gọi Cloud helper trực tiếp để bỏ qua vòng đời worker.
- Persist video_path, source_path và source_sha256; giữ job ID, frozen settings, force_cloud, GPU/voice. Không serialize bot/Update/context.
- Chuẩn hóa đường dẫn tuyệt đối và xác minh file video khi enqueue; kiểm tra lại tồn tại/hash trước chạy. Local_file được dùng đường dẫn người dùng chỉ định; URL/video vẫn bị giới hạn trong downloads.
- Nguồn mất/đổi/hash không khớp là lỗi xác định, không rơi vào download file_id hoặc tự gọi lại Cloud.
- Legacy có path đã lưu thì dùng đúng path đó; thiếu video_path nhưng có resume_state.source_path tuyệt đối thì phục hồi từ giá trị persisted. Hai path mâu thuẫn hoặc thiếu cả hai thì giữ job với lý do cần xử lý nguồn, không đoán bằng basename.
- Pending mất nguồn được giữ manual hold không thể release mù; UI giải thích để người dùng bỏ đúng job và gửi lại đường dẫn. Active job sau crash tiếp tục needs_review, không tự chuyển pending để render lại.

Test dùng worker và queue thật trong thư mục tạm, provider/bot stub: dispatch một lần; round trip Unicode/khoảng trắng; tất cả ca legacy trên; source đổi; persistence failure; stop/cancel giữ record đến khi drain; URL/video và network retry cũ không hồi quy. Các job đã bị task_done mất từ phiên trước chỉ phục hồi khi có bằng chứng bền vững, không tự dựng lại từ log mơ hồ.

**Bước 5 sửa ranh giới chạy lệnh Cloud và cách truyền secret.**

Mã trọng tâm: [cloud_orchestrator.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/cloud/cloud_orchestrator.py>), [colab_bridge.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/cloud/colab_bridge.py>), runner GPU.

- Khảo sát read-only CLI thực: version/help và runner có thẩm quyền; xác minh exec -f có lưu source vào notebook/history hay không, timeout local có hủy remote hay chỉ ngừng chờ, và cách exit code/exception được trả về. Code hiện dùng exec -f; chưa có bằng chứng provider hỗ trợ env/stdin, nên không tự giả định các tùy chọn đó.
- Tách adapter nhận argv/payload có schema khỏi orchestrator. Remote runner dùng subprocess argv với shell=False; các lệnh setup tĩnh cũng tránh trộn dữ liệu người dùng vào shell.
- Validate URL scheme, loại GPU/task/voice; đường dẫn Unicode, dấu nháy và khoảng trắng phải đi qua đúng dưới dạng dữ liệu. Không “chữa” injection bằng cách làm sai tên file hợp lệ.
- Ưu tiên cơ chế secret được provider hỗ trợ. Nếu không có, chỉ dùng file secret tạm khi transport/permission đã kiểm chứng: quyền tối thiểu, ngoài bundle/result, giới hạn theo session/attempt, xóa trong cleanup. Nếu chưa có transport đạt yêu cầu thì Cloud giữ tắt.
- Log operation/job/session và mã lỗi đã làm sạch; không log raw command, env, secret payload hoặc toàn bộ output chưa qua lọc. Redaction chỉ là lớp bổ sung, không thay việc bỏ secret khỏi command line.
- Local/WSL/remote đều không được đưa key vào argv, traceback, metadata, artifact hoặc Telegram. Cleanup kiểm tra cả đường thành công, lỗi, timeout và cancellation.
- Nếu điều tra sau sửa xác nhận khóa đã từng lộ, xử lý thay khóa qua quy trình tài khoản tương ứng; không in lại khóa trong evidence.

Test offline: đầu vào chứa dấu nháy đơn, nháy kép, xuống dòng, ký tự shell, Unicode và khoảng trắng vẫn là một tham số; sentinel secret không xuất hiện trong bất kỳ command/log/result nào; timeout/crash vẫn dọn đúng artifact của attempt. Chưa chạy paid job ở bước này.

**Bước 6 sửa cancellation và publication Cloud cùng nhau.**

Mã trọng tâm: cloud store/orchestrator/bridge/routes/worker. Bổ sung revision, attempt/claim token, cancel intent và thông tin session/publication theo migration có phiên bản. Job đã terminal không được update_progress hồi sinh.

Hợp đồng trạng thái:

| Tình huống | Trạng thái và phản hồi |
|---|---|
| Hủy queued chưa có tài nguyên | stopped bền vững, không tạo session |
| Hủy khi đang tạo/upload/chạy/download/validate | cancel_requested → stopping → stopped sau xác nhận cleanup |
| Chưa xác nhận session đã thu hồi | stop_incomplete/recovery_required; không báo đã dừng xong |
| Completion và cancel cạnh tranh | Một phía thắng theo transaction/guard; phía còn lại nhận kết quả thật |
| Callback/worker attempt cũ | Bị từ chối, không đổi state/progress/result |
| Retry có chủ đích | Attempt mới; không mở lại terminal bằng progress update |

Thiết kế cancellation:

1. Persist cancel intent trước khi gọi provider. Request lặp trả cùng kết quả; không gọi stop rồi mới ghi cancel.
2. Claim của CloudWorker và Telegram dùng cùng primitive atomically; bỏ đường Telegram tự chuyển processing rồi chạy mà không claim. Một job/attempt chỉ có một owner.
3. Mọi transition dùng expected state/revision/attempt. Callback terminal hoặc progress sau cancel không đổi stopped thành processing/completed.
4. Kiểm tra cancel trước/sau provisioning, upload, exec, download, validation và ngay tại commit publication. Các lời gọi dài có timeout và cơ chế quan sát cancel; không chỉ kiểm tra giữa hai bước kéo dài hàng chục phút.
5. Liên kết bền vững queue job ↔ cloud job ↔ attempt/session. Stop từ Telegram phải chuyển thành cancel intent của đúng cloud job, không chỉ đặt flag local rồi chờ Cloud chạy hết.
6. Cleanup chỉ nhắm session có ownership đúng attempt. Chưa thu hồi được thì giữ bằng chứng/lease cần đối soát; không dừng mọi session trên tài khoản.

Thiết kế download và output:

- Download vào thư mục staging riêng theo job/attempt, không dùng file cũ làm bằng chứng thành công. Phải có rc=0 và file mới thuộc attempt hiện tại.
- Xác minh file tồn tại, size>0, container/streams/duration hợp lệ, hash/metadata phù hợp manifest. Dùng gate media chung khi có thể; có hình và âm thanh chưa đủ chứng minh dịch/TTS đúng nội dung.
- Giữ quy tắc tên output hiện tại để tương thích; nếu đích đã thuộc job khác hoặc không xác minh được ownership thì trả output_conflict. Không ghi đè tự động. Replay đúng job/hash nhận lại kết quả đã commit.
- Chuẩn bị file đã kiểm tra trên cùng volume với thư mục output. Copy khác volume chỉ vào staging; publication dùng thay tên atomic ở bước cuối.
- Phân định publication bằng một transaction/guard ngắn: kiểm tra cancel và attempt, ghi publication intent bền vững, chuyển publishing, rồi atomic rename, ghi receipt, chuyển completed. Không giữ khóa qua network hoặc probe.
- Nếu cancel đã được chấp nhận trước commit publication, không công bố output. Nếu publication đã thắng, cancel không được trả “đã hủy”; trả trạng thái completed hoặc publication đang cần đối soát.
- SQLite và filesystem không atomic chung. Recovery phải xử lý từng điểm crash: trước rename, sau rename trước receipt, sau receipt trước completed. So sánh job/attempt/hash để hoàn tất đúng publication; mâu thuẫn thì needs_review, không render lại hoặc ghi đè.
- Receipt gắn source/job/attempt/output hash; output cũ được bảo toàn byte-for-byte khi transfer/validation/publication thất bại.

Test bắt buộc: rc!=0+partial; rc=0+missing; stale file; zero-byte; thiếu audio/video; ffprobe timeout; hash sai; file đích có sẵn; replay; cancel tại từng phase; cancel tranh publication bằng barrier; callback attempt cũ; crash ở các điểm commit; provider stop lỗi; worker và Telegram claim cùng job. Tất cả dùng fake provider và DB/temp artifacts.

Điều kiện đạt: job canceled không phát sinh output mới sau điểm cancel đã thắng; completed luôn có receipt/media hợp lệ; output trước không bị hỏng; trạng thái thu hồi tài nguyên được báo trung thực.

**Bước 7 nối kết quả Cloud với tracker và thông báo Telegram.**

- Cloud helper chỉ trả thành công sau publication đã xác minh.
- Sau native task drain, kiểm tra stop/cancel và gọi finalization chung với execution/generation hiện tại. Chỉ receipt được tracker chấp nhận mới cho đặt tracker_finalized=true.
- Sau đó mới phát thông báo thành công và để worker hoàn tất batch/queue/release theo cùng outcome.
- Không sửa worker thành “result.status=success thì tin luôn”; giữ gate media và identity.
- Dùng lỗi verification riêng để file hỏng/thiếu audio/ffprobe không xác minh được không kích hoạt paid rerender tự động.
- Giữ đường local V1 đã tracker_finalized để không ghi history/completion hai lần.
- Crash sau Cloud completed nhưng trước local receipt không được enqueue một cloud job mới; đối soát cloud_job_id/attempt/output receipt trước, nếu chưa rõ giữ needs_review.

Test kiểm tra thứ tự bằng fake bot: verified output → receipt → tracker completed → notification → queue completion → release. Provider failure, output invalid, stale receipt hoặc stop thắng đều không có success notification. Test integration dùng video tổng hợp nhỏ có hình/âm thanh; không gửi Telegram thật.

**Bước 8 sửa năm regression OCR theo hợp đồng dữ liệu và bằng chứng hình ảnh.**

Mã trọng tâm: [ass_utils.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/ass_utils.py>), [ocr_subtitle_locator.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/ocr_subtitle_locator.py>), [v1_ocr_geometry.py](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/v1_ocr_geometry.py>), đồng bộ subtitle/OCR và report.

| Mã | Hướng sửa | Tiêu chí |
|---|---|---|
| O1 | Tách mask che chữ nguồn khỏi nền chữ dịch và timeline TTS | Mask nguồn chỉ tồn tại ở khoảng OCR xác minh có chữ; không bridge/hold qua khoảng verified absence |
| O2 | Phân biệt chưa có dữ liệu với đã xác minh tracking rỗng | Tracking rỗng đã xác minh không hồi sinh best_block cũ |
| O3 | Chỉ dùng anchor đã khớp nguồn; không dùng anchor rescued để cứu dây chuyền | Nhãn lặp/static không liên quan không thành phụ đề chỉ vì nằm giữa hai anchor |
| O4 | Giữ identity theo timestamp/sample; lọc hình học yếu trong cùng frame | Observation mạnh ở thời điểm khác không bị xóa vì lệch tâm X |
| O5 | Validator chỉ kiểm tra, không nâng median fallback thành located/verified | Unresolved giữ provenance, audit được ghi rồi strict validation báo lỗi |

Chi tiết cần giữ:

- Việc kéo dài TTS hoặc đồng bộ cue không được kéo source mask hoặc lấp gap đã xác nhận vắng chữ. Nền chữ dịch có layout/layer riêng theo cấu hình, không được dùng làm bằng chứng OCR.
- Không giải quyết O1/O2 bằng cách chỉ đổi tên ASS style. Phải tách dữ liệu và thời gian thực của source mask với text background.
- O3 giữ tập original matched anchors bất biến; không nới cờ reliable để nhãn lặp vượt quality gate.
- O4 không gộp box từ hai thời điểm thành một frame giả. Khung hình thiếu chữ và sample mạnh đều có provenance.
- Cập nhật cover QC để phân biệt source mask và nền dịch, đồng thời đọc được ASS cũ. Nếu đổi hợp đồng geometry, tăng phiên bản geometry và vô hiệu hóa đúng cache OCR/ASS liên quan; không xóa toàn bộ workspace hoặc các artifact ASR/audio còn hợp lệ. Test resume phải chứng minh không dùng lại mask từ schema cũ.
- O5 hiện report đã được ghi trước validate ở một đường chạy; nguyên nhân chính là fallback sửa unresolved thành located. Cần sửa classification/validator, không chỉ đổi thứ tự save.
- Giữ năm test hiện tại làm acceptance. Nếu cần tách layer trong helper đọc ASS, chỉ điều chỉnh cách phân biệt lớp dữ liệu, không nới điều kiện về khoảng mask hoặc độ xác minh.
- Bổ sung clip tổng hợp/frame fixtures: khoảng trống giữa cue, tracking rỗng, packaging/watermark lặp, chữ di chuyển, cue nhiều dòng, Unicode và TTS dài hơn chữ nguồn.
- Xuất ASS, audit JSON, frame trước/sau và pixel-diff ở thời điểm biên. Kiểm tra mask trong vùng cần che và không che vùng verified absence; nền dịch được đánh giá riêng.

Năm test phải xanh:

```text
tests/test_ocr_geometry_audit.py::SourceMaskAuditTests::test_verified_absence_does_not_bridge_or_hold_mask
tests/test_ocr_geometry_audit.py::SourceMaskAuditTests::test_verified_empty_tracks_never_resurrect_best_block
tests/test_ass_cover_dimensions.py::PhaseFourCoverTests::test_repeated_unmatched_label_cannot_use_bracket_recovery
tests/test_subtitle_tracking_regression.py::SubtitleRegressionTests::test_frames_same_segment_are_not_composited_across_time
tests/test_v1_ocr_geometry_repair.py::PipelineTests::test_report_saved_before_unresolved_error
```

Sau đó chạy toàn bộ các file suite liên quan để tránh sửa đúng fixture nhưng làm hỏng OCR khác, dual voice hoặc render layout.

**Bước 9 nghiệm thu hợp nhất trên cùng revision.**

Không chỉ cộng số test pass của từng nhánh. Sau tích hợp chạy lại:

1. Runner dashboard hiện có, bổ sung các suite mới cho handoff, Cloud adapter/state/output, UI retry và schedule recovery.
2. Các suite OCR đầy đủ và các regression voice/render chịu tác động. Yêu cầu 0 failed trong phạm vi đã chốt; số pass có thể tăng, không cố giữ đúng 322.
3. JavaScript và browser với server/cổng/workspace tạm.
4. Migration/rollback trên bản sao fixture.
5. Benchmark cùng corpus để so trước/sau và phát hiện khóa mới làm chậm polling.
6. git diff --check, compile/import các module thay đổi trong môi trường cách ly.

Browser matrix phải có: busy→retry; reload khi request chưa rõ kết quả; cancel schedule→ba hành động; hai tab; stale pause; job A/B khác nhau; Unicode/khoảng trắng; mô phỏng local_file/cloud success/error. Assertion dựa vào trạng thái server/runner call count, không chỉ toast.

Evidence cho mỗi gói gồm test thất bại trước sửa, test đạt sau sửa, revision/hash, log/JUnit, dữ liệu migration và ảnh/video khi liên quan giao diện/OCR. Kiểm tra evidence không chứa secret.

**Bước 10 chạy bền và phát hành có điều kiện.**

Sau smoke khoảng 2–5 phút và một lượt stress trung gian khoảng 30–60 phút đạt, tiến hành hai kiểu soak riêng:

| Lượt | Mục tiêu |
|---|---|
| 24 giờ có restart/fault injection | Handoff, queue, lost ACK, migration/recovery và identity qua restart |
| 24 giờ một process sống liên tục | RAM/thread/handle, tích lũy queue/receipt và polling khi có tải |

Mở rộng harness để có corpus input/output, batch UI và các ca mới, không chỉ empty listing. Lượt đánh giá tích lũy không reset history giữa các segment. Phân biệt lỗi được tiêm có chủ đích với lỗi ngoài kịch bản; báo cáo phải có đủ thời lượng quan sát.

Gate đề xuất cho fixture cố định, cần đóng băng trước lượt soak chính thức:

- Không mất/trùng job, sai execution, ghi đè output, hồi sinh canceled hoặc success giả.
- 100% thao tác đã ACK đối soát được sau restart; không có request quá 5 giây hoặc khoảng poll quá interval+5 giây ngoài downtime được chủ động ghi nhận.
- P95 API đọc dưới 500 ms trên corpus/máy test đã ghi rõ.
- Ghi baseline sau warm-up 30 phút. RSS median giờ cuối không tăng quá max(32 MiB, 20%) so với baseline; không có xu hướng tăng đều theo số cycle chưa giải thích được.
- Sau khi drain và idle 2 phút, thread trở về trong baseline+2, handle trong baseline+20. Các ngưỡng này là gate fixture đề xuất, không áp trực tiếp cho model GPU thật.
- Phân biệt cache bị chặn kích thước với dữ liệu lịch sử tăng có chủ đích; lịch sử tăng phải có số đo bytes/job và chính sách lưu giữ, không gọi đó là đã chứng minh chạy vô hạn.

Canary thật và triển khai là bước sau, không được suy ra từ việc kế hoạch đã lập. Cần checkout/runner cloud có thẩm quyền, video được phép tải lên, phạm vi Telegram và ngân sách cho đợt thử. Các gate chưa hoàn tất trong roadmap cloud vẫn chặn bật cloud production dù F2–F4 đã xanh offline.

Trước nâng dịch vụ: chặn admission, drain công việc, ghi inventory, backup nhất quán DB/snapshot/output receipts và phiên bản code. SQLite dùng backup API hoặc snapshot khi writer đã dừng, không copy tùy ý file DB đang ghi/WAL. Nâng API, bot, controller và supervisor đồng bộ.

Migration phải chạy lặp an toàn; legacy chưa xác minh giữ needs_review. Rollback code chỉ khi schema tương thích; nếu đã nhận job mới, đối soát dữ liệu mới và ưu tiên forward repair, không chép DB cũ lên làm mất job/output. Giữ bản lỗi và evidence cho tới khi phục hồi được kiểm chứng.

**Điều kiện đóng toàn bộ đợt sửa.**

| Điều kiện | Bằng chứng bắt buộc |
|---|---|
| F1–F9 đã đóng | Test regression tương ứng và trace đúng identity/outcome |
| O1–O5 đã đóng | Năm test cũ xanh, suite liên quan xanh, audit và frame/pixel đối chứng |
| Người dùng tự thoát được batch/lịch bị giữ | Browser acceptance, không dùng SQL/curl để cứu |
| Cloud không báo thành công sai | Negative tests và receipt/media verification |
| Dữ liệu cũ không bị mất | Migration/recovery fixtures và inventory trước/sau |
| Chạy bền đạt | Báo cáo đủ thời lượng; không dùng smoke thay soak |
| Bàn giao rõ | Diff theo gói, hướng chạy test, giới hạn còn mở, deployment/rollback record |

Triển khai từng gói thành thay đổi dễ review; không trộn sửa OCR, đổi schema và thay UI vào một commit không phân biệt. Không commit/stage baseline có sẵn ngoài phạm vi của từng gói. Biên bản dashboard phải được cập nhật bằng kết quả thực tế sau mỗi gate, tuyệt đối không đổi trạng thái kế hoạch thành “đã nghiệm thu” chỉ vì code đã viết.

