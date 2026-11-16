# Kế hoạch triển khai Cloud sau bàn giao Tool V1 — 14/11/2026

## Mục tiêu và phạm vi

Đưa một job video lên GPU Cloud, xử lý đủ các bước, nhận thành phẩm đã kiểm tra,
và thu hồi đúng tài nguyên của job. Người dùng phải biết job đang ở đâu, hủy được,
biết chi phí đang ghi nhận và không mất thành phẩm cũ. Local vẫn hoạt động độc lập.

Tài liệu này là kế hoạch, không phải chứng nhận Cloud đã sẵn sàng. Chưa chạy job
Cloud, chưa thuê GPU, chưa thay dependencies hoặc bật tự động chuyển job lên Cloud.
Không áp dụng quy tắc cứu dual voice của một video mẫu cho mọi video.

## Điều kiện đầu vào

1. Chốt bản local và xử lý hoặc giải thích có bằng chứng năm test OCR/phụ đề lỗi
   ghi trong tài liệu bàn giao. Không sửa kỳ vọng test chỉ để chuyển thành xanh.
2. Xác định một checkout Cloud có thẩm quyền. Hiện mã thử có trong
   `C:\tool v1\backend\cloud`, nhưng `start_cloud_dashboard.bat` chạy
   `run_cloud_node.py` tại `C:\Projects\Tools\Tool V1\Updates`.
   Phải đối chiếu hai nơi trước khi sửa; chưa xác nhận runner của Updates trong đợt này.
3. Giữ workspace V1 `D:\workspace`, CapCut `D:\CapCut`, thành phẩm theo cấu hình.
   Không tự di chuyển dữ liệu, reset Git, xóa job hay thay interpreter hệ thống.
4. Ghi lại trạng thái repo, diff, hash, cấu hình không chứa bí mật và bản sao DB trước
   migration. Không đưa `.env`, token, cookie, DB, log hoặc video vào Git/PR.
5. Cloud mặc định tắt. Chỉ chạy thật sau khi hoàn tất kiểm thử cô lập và người dùng
   chọn video được phép tải lên cùng hạn mức chi phí cho đợt thử.

## Thứ tự thực hiện và đầu ra

| Giai đoạn | Đầu ra bắt buộc | Phụ thuộc | Ước lượng công sức* |
|---|---|---|---|
| 0 | Chốt baseline local, checkout Cloud và hợp đồng job | Không | 0,5–1 ngày |
| 1 | Adapter chạy lệnh an toàn và xác thực giao diện | 0 | 0,5–1 ngày |
| 2 | Máy trạng thái, hủy, lease và phục hồi session | 1 | 1–2 ngày |
| 3 | Sổ chi phí và giữ chỗ ngân sách | 2 | 1 ngày |
| 4 | Môi trường GPU có phiên bản cố định, kiểm tra đầu vào | 1 | 0,5–1 ngày |
| 5 | Chặn bản dịch/TTS thiếu, QC và xuất file an toàn | 2, 4 | 1–2 ngày |
| 6 | API, Telegram và dashboard dùng chung hợp đồng | 2, 3, 5 | 0,5–1 ngày |
| 7 | Kiểm thử lỗi, canary thực tế và hồ sơ nghiệm thu | 1–6 | 1–2 ngày |
| 8 | Bật có kiểm soát, theo dõi và diễn tập khôi phục | 7 | 0,5 ngày |

*Ước lượng cho một người triển khai, không phải cam kết thời gian hay chi phí GPU.
Thời gian chờ nhà cung cấp, tải model và sửa năm vấn đề local chưa tính cố định.
Không dùng số CU/giờ đang hardcode làm biểu giá được xác nhận.

## Giai đoạn 0 — Khóa phạm vi và hợp đồng

- Đối chiếu repo/nhánh/runner thật, cổng 8089, phiên bản WSL và CLI Colab; kiểm tra
  chỉ đọc các lệnh hỗ trợ tạo, tra cứu, chạy và dừng session. Không đoán API.
- Đọc `cloud_config.py`, `colab_bridge.py`, `cloud_job_store.py`,
  `cloud_orchestrator.py`, `cloud_worker.py`, `cloud_routes.py`,
  `gpu_scripts/setup_env.py`, `gpu_scripts/full_pipeline.py`, requirements và runner.
- Vẽ luồng thực: Telegram -> hàng đợi local -> `run_pipeline_cloud_for_telegram`
  và Dashboard -> CloudWorker. Hiện Telegram gọi orchestrator trực tiếp sau enqueue;
  phải loại khả năng worker khác claim cùng job hoặc chạy vượt ngân sách đồng thời.
- Chốt schema job: UUID, job nguồn, input fingerprint, options snapshot, attempt,
  session ownership, timestamps UTC, deadline, trạng thái, lỗi có mã, output manifest.
- Chốt một điểm admission/claim dùng chung; endpoint không tự chạy pipeline.
- Nghiệm thu: một sơ đồ luồng, bảng trạng thái, danh sách config, baseline test và
  danh sách lỗi còn mở; không có hai nơi tự ý quyết định cùng một job.

## Giai đoạn 1 — Lệnh, bí mật và ranh giới truy cập

Vấn đề: `cloud_orchestrator.py` ghép input/voice/key vào shell bằng dấu nháy đơn;
`colab_bridge.py` thực thi nội dung bằng `shell=True` trên máy từ xa.

- Tách adapter provider khỏi orchestrator. Ưu tiên truyền argv và payload JSON có
  schema thay cho ghép chuỗi lệnh. Nếu CLI bắt buộc dùng shell, quote từng giá trị
  ở đúng tầng và kiểm thử cả Windows -> WSL -> remote; không coi quote tầng ngoài
  là đã bảo vệ câu lệnh bên trong.
- Allowlist loại GPU, task, voice và URL scheme. Đường dẫn local phải là file video
  hợp lệ; không chấp nhận thư mục, chuỗi điều khiển hoặc path thoát vùng artifact.
- Truyền khóa qua cơ chế bí mật được provider hỗ trợ; nếu phải dùng file tạm thì
  giới hạn quyền, phạm vi session, thời gian sống và không log nội dung. Không đưa
  key vào command line, traceback, URL query, thông báo Telegram hay metadata kết quả.
- Kiểm tra runner Cloud có bind loopback, local control token và kiểm soát Origin/Host
  tương đương V1; từ chối thao tác ghi thiếu token. Không suy rằng router tự có auth.
- Kiểm thử: tên có dấu nháy, Unicode, khoảng trắng, `&`, chuỗi chèn shell; input lỗi
  phải bị từ chối hoặc truyền nguyên dạng, không phát sinh lệnh phụ. Log không có secret.

## Giai đoạn 2 — Vòng đời job và hủy thực sự

Vấn đề: `update_progress` có thể ghi đè `stopped`; stop worker chỉ đặt cờ ngoài
orchestrator; kết quả dừng session trong `finally` chưa được kiểm tra.

- Chốt trạng thái: queued -> provisioning -> uploading -> preparing -> processing
  -> downloading -> validating -> completed. Nhánh hủy đi qua cancel_requested,
  cancelling và chỉ sang cancelled khi đã xác nhận tài nguyên không còn chạy.
  failed/cancelled/completed là trạng thái cuối, không nhận cập nhật tiến độ muộn.
- Transition bằng câu lệnh có điều kiện state/version/attempt trong transaction.
  Worker claim có lease, owner và heartbeat; chỉ owner hiện tại được cập nhật.
- Cancellation token tồn tại bền vững; kiểm tra trước/sau mỗi thao tác chặn.
  Adapter hỗ trợ timeout và cancel cho subprocess đang chạy, không chỉ giữa các bước.
- Đóng race hủy trong lúc tạo session: nếu tạo xong sau yêu cầu hủy, thu hồi session
  ngay, không upload hoặc chạy pipeline. Tiến độ/kết quả từ attempt cũ bị bỏ qua.
- `worker.stop()` trả đúng trạng thái còn dừng hay đã dừng; không báo thành công
  khi thread vẫn sống. Dừng nhận job mới trước, xử lý job hiện tại theo chính sách rõ ràng.
- Session phải có dấu sở hữu theo job/attempt. Retry và cleanup chỉ tác động tài
  nguyên thuộc job; không dùng `stop_all_sessions` để dọn chung tài khoản.
- Cleanup thất bại phải thành sự kiện cần xử lý, có retry giới hạn và tra cứu lại;
  không xác nhận đã hết tính phí khi chưa biết kết quả.
- Startup đối soát lease/session thật; không đánh dấu error theo tuổi `created_at`
  rồi bỏ mặc máy GPU còn chạy. Ghi nhận trạng thái không xác định khi provider mất kết nối.
- Kiểm thử tại mọi bước, gồm mất tiến trình, lease hết hạn, cập nhật muộn, hai worker
  tranh job, session tạo muộn và provider từ chối dừng. Không có job bị hồi sinh.

## Giai đoạn 3 — Ngân sách có thể kiểm toán

Vấn đề: chỉ cộng `cu_actual` của job completed theo ngày tạo; job lỗi/hủy và xóa
bản ghi có thể làm mất khoản chi. Chỉ kiểm tra trước job không phải giới hạn cứng.

- Thêm bảng cost ledger/session riêng, liên kết job/attempt; mọi trạng thái đều
  ghi usage. Xóa/ẩn lịch sử giao diện không xóa chi phí hoặc dữ liệu đối soát.
- Admission giữ chỗ ngân sách atomically cùng claim; hai worker không được dùng
  chung phần dư. Kiểm tra số dư provider và ngân sách theo timezone đã cấu hình.
- Lưu estimated, reserved, observed và reconciled riêng. Hiển thị rõ số ước tính;
  nếu API không có usage thực theo session, ghi giới hạn đo thay vì gọi đó là thực tế.
- Theo dõi trong lúc chạy với khoảng kiểm tra có giới hạn. Tính độ trễ dừng và
  làm tròn billing vào phần dự phòng; không hứa tuyệt đối không vượt khi provider
  không hỗ trợ hard cap. Khi số liệu không xác định, chặn nhận job mới phù hợp chính sách.
- Job qua nửa đêm phải phân bổ theo thời gian sử dụng/chính sách đã chốt, không
  lọc theo `created_at`. Chỉ giải phóng reservation sau đối soát việc thu hồi session.
- Kiểm thử: job lỗi đã ghi 3 CU vẫn tính 3 CU; job hủy/xóa vẫn giữ ledger; hai job
  cạnh tranh phần ngân sách cuối; retry; nửa đêm; provider trả số dư cũ hoặc mất kết nối.

## Giai đoạn 4 — Dựng môi trường và tiền kiểm

- Kiểm tra lockfile, phiên bản CUDA/PyTorch/FFmpeg/ASR/solver và API TTS thực dùng.
  Pin dependency/model revision theo kết quả tương thích; lưu manifest môi trường.
- Setup idempotent, timeout rõ, mã thoát thật; báo riêng lỗi mạng, thiếu package,
  GPU không đúng loại, thiếu dung lượng hoặc model không tải được.
- Mọi cài đặt diễn ra trong môi trường repo/VM dành riêng; không sửa venv production.
- Kiểm tra quyền sử dụng model/phụ thuộc và dung lượng upload trước khi thuê VM lâu.
- Input có fingerprint, kích thước/duration/codec; upload có xác minh. Giữ nguồn
  local nguyên vẹn, không xóa sau khi gửi lên Cloud.
- Nghiệm thu: môi trường dựng lại được từ manifest; setup lần hai không làm đổi
  kết quả; tình huống mất mạng dừng có lỗi rõ và vẫn thu hồi tài nguyên.

## Giai đoạn 5 — Chất lượng và xuất thành phẩm

Vấn đề: dịch lỗi giữ tiếng gốc, thiếu TTS bị bỏ qua, không có TTS thì xuất nhạc nền;
`copy2` tới tên cố định có thể ghi đè thành phẩm cũ.

- Dịch phải đủ đúng cue ID, đúng ngôn ngữ mục tiêu, không rỗng/trùng/sai schema.
  Missing key hoặc model lỗi không được biến thành “dịch thành công”. Retry có giới hạn.
- TTS phải đủ cue cần đọc, file decode được và duration hợp lệ. Fallback chỉ được
  dùng khi tạo ra âm thanh đạt kiểm tra; không bỏ qua cue lỗi hoặc xuất BGM-only như
  một bản lồng tiếng thành công. Video cố ý không lời cần chế độ riêng có metadata.
- Dùng các quy tắc timing, giới hạn tốc độ, overlap, âm lượng, phụ đề và QC local
  phù hợp cho Cloud; chốt version/snapshot để khác biệt có thể giải thích.
- Download vào thư mục attempt riêng; kiểm tra size/checksum khi có, ffprobe,
  duration, streams và QC trước khi chuyển sang completed hoặc gửi Telegram.
- Tên output chứa job ID và attempt/version. Xuất atomic trên cùng filesystem;
  không ghi đè bản cũ, không nhận đường dẫn từ metadata remote thiếu kiểm tra.
- Trạng thái completed kèm output manifest/QC report. Download dở hoặc QC fail
  giữ bằng chứng chẩn đoán trong vùng riêng, không đưa file lỗi vào danh sách thành phẩm.
- Kiểm thử: phản hồi dịch thiếu một câu, TTS lỗi cuối video, toàn bộ TTS lỗi,
  output cắt cụt/không audio/sai duration, tên trùng, đĩa đầy, gián đoạn copy.

## Giai đoạn 6 — API, Telegram và dashboard

- Chuyển provider call đang chặn trong async route/handler sang executor có giới
  hạn hoặc API async thật. Không tạo vô hạn thread khi dashboard polling liên tục.
- Cache usage/sessions với tuổi dữ liệu; status vẫn phản hồi nhanh khi provider lỗi.
  Không gọi status để vô tình khởi tạo worker, DB hoặc session mới không cần thiết.
- Dashboard hiển thị: bước hiện tại, lỗi có cách xử lý, trạng thái hủy đang chờ,
  chi phí ước tính/đã đối soát, tài nguyên chưa thu hồi và link QC/thành phẩm.
- Telegram và dashboard dùng cùng admission/cancel/status service; request retry
  có idempotency key, không nhân đôi job hoặc gửi trùng thành phẩm.
- Snapshot lựa chọn local/cloud theo job. Đổi mặc định không chuyển những job đang
  chờ/chạy. Không tự chuyển lên Cloud khi local lỗi nếu chưa có chính sách được chọn.
- Kiểm thử: provider status treo 30 giây nhưng local status/cancel vẫn phục vụ;
  nhiều tab polling, request trùng, payload không hợp lệ, người gọi không có quyền.

## Giai đoạn 7 — Ma trận nghiệm thu

Trước hết dùng adapter giả và SQLite tạm; không gọi provider hay đọc/ghi DB thật.

| Nhóm | Tình huống tối thiểu | Tiêu chí đạt |
|---|---|---|
| Input/lệnh | Nháy đơn, Unicode, shell metacharacters, path traversal | Không lệnh phụ, không thoát vùng file |
| Đồng thời | Hai worker, retry HTTP, attempt cũ cập nhật | Một owner hợp lệ, không nhân đôi job |
| Hủy | queued và từng bước đến validating | Không hồi sinh; cleanup có kết quả xác minh |
| Recovery | Crash trước/sau tạo VM, mất lease, provider offline | Đối soát được, không bỏ quên VM |
| Chi phí | Error/cancel/delete/retry/nửa đêm | Ledger đầy đủ và reservation không đếm đôi |
| Dịch/TTS | Thiếu cue, sai ngôn ngữ, file hỏng, TTS rỗng | Chặn hoàn thành và bàn giao sai |
| Thành phẩm | Trùng tên, download dở, đĩa đầy | Không mất file cũ; không công bố file chưa đạt |
| Giao diện | Provider chậm, polling nhiều, cancel lặp | Status phản hồi; thông tin nhất quán |
| Local | Cloud tắt, backend Cloud lỗi hoặc không cài | Local vẫn chạy; cấu hình/workspace giữ nguyên |
| Bí mật | Lỗi provider, log, traceback, artifact, PR | Không lộ token/key/cookie |

Sau khi kiểm thử cô lập đạt, canary có giới hạn: một video ngắn, một video có tên
đặc biệt, một ca hủy. Người dùng xác định input và trần chi phí trước khi chạy.
Đối chiếu cùng input local/cloud về đủ lời, vai giọng, phụ đề, timing, chất lượng
video và chi phí. Không lấy một video đúng làm tỷ lệ chính xác tổng quát.

## Giai đoạn 8 — Phát hành và khôi phục

- PR theo từng nhóm: (1) adapter/auth, (2) lifecycle/cancel, (3) ledger,
  (4) quality/artifacts, (5) UI/wiring, (6) canary/runbook. Mỗi PR ghi test và giới hạn.
- Bật thủ công cho một job trước; concurrency=1 cho đến khi kiểm thử nhiều worker
  đạt. Không bật chế độ tự động mặc định ngay sau khi merge.
- Theo dõi session chưa thu hồi, job vượt deadline, lỗi QC, ledger chưa đối soát,
  latency status và lỗi gửi trùng. Cảnh báo chỉ chứa mã job, không chứa bí mật.
- Khôi phục: ngừng admission Cloud, xử lý job đang chạy theo cancel policy, đối soát
  VM/chi phí, chuyển job mới về local. Khôi phục mã từ bản đã lưu trong cửa sổ idle;
  không reset working tree đang có sửa đổi và không phục hồi đè DB/video đang dùng.
- Hoàn tất khi không còn P1 mở, toàn bộ test bắt buộc đạt, canary đạt, không còn
  session thuộc đợt thử và có biên bản chi phí/QC/khôi phục. Nếu chưa đủ, giữ draft.
