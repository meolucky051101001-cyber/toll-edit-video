**Tiến độ triển khai Dashboard Tool V1 — cập nhật 16/11/2026**

Đã tiếp tục phần triển khai bị gián đoạn trong chat “Dashboard tool”. Bản hiện tại tích hợp các sửa chữa nền tảng của đợt A, đã qua regression và kiểm tra trình duyệt cách ly. Lượt nối tiếp ngày 16/11 bổ sung nền tảng W9, diễn tập migration và harness soak; số đo mới ở phần cuối biên bản. Đây là bản để tiếp tục nghiệm thu, chưa phải bản phát hành đã hoàn tất W0–W10.

Worktree tại `C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1`, commit gốc `4b2d234acc92fead3fc097e570de523089290aa4`. Baseline gồm 30 file thay đổi có sẵn, ghi SHA-256 trong `dashboard_implementation_baseline.json`. Đối chiếu cuối phiên cho thấy 29 file giữ nguyên hash, riêng `backend/telegram_bot.py` được sửa tiếp theo phạm vi dashboard. Checkout đang dùng tại `C:/tool v1` giữ nguyên. Thay đổi đang ở working tree, chưa commit hoặc triển khai dịch vụ.

**Trạng thái theo kế hoạch**

| Gói | Kết quả hiện có | Điều kiện còn lại |
|---|---|---|
| W0 — Cách ly và baseline | Giữ baseline, test dùng workspace/input/output/data tạm, runner lưu evidence | Bổ sung corpus render thực |
| W1 — Control và phạm vi tiến trình | Hợp nhất control, kiểm tra ownership/PID, cố định đường tracker xuyên tiến trình | Nghiệm thu dịch vụ thật và diễn tập nâng cấp đồng bộ |
| W2 — Dừng và vòng đời | Giữ lease khi chờ thread kết thúc, fence callback theo execution, Telegram dùng chung admission | Thử stop trên stage GPU/native và mạng thật |
| W3 — Batch bất biến | Lưu tập nguồn/cấu hình, idempotency, đối soát phản hồi mất, Start tôn trọng selection | Render đầu cuối đến receipt/output/archive |
| W4 — Queue bền vững | Restore trước watcher, command theo ID/revision/ack, giữ active/snapshot hỏng, retry giữ ID | Thử crash nhiều điểm và xử lý legacy cần review |
| W5 — Recovery | Nguồn chính xác, SHA-256, manifest/workspace riêng, token stale bị từ chối, restart tạo workspace mới | Resume với artifact từ các model thực |
| W6 — Lịch đúng mục tiêu | Target local/Telegram, hold qua restart, cancel giữ job, generation/grace ngăn chạy muộn | Lịch và giao nhận Telegram thật trong canary |
| W7 — Giao diện | Retry cùng operation, trạng thái ack/hold, progress tám bước, Unicode, so sánh/override/capability | Mở rộng browser matrix cho bàn phím, mobile, tên đặc biệt |
| W8 — Hiệu năng | Polling độc lập, cache giới hạn, readiness, benchmark 20 video và 20 job | Corpus lớn, tài nguyên GPU, polling lâu |
| W9 — Hai worker thực sự | Nền tảng registry/lease/stop/output reservation và resource gate đã có test; dispatcher vẫn 1 | Tích hợp dispatcher/tracker/stop đa execution, retention, đo RAM/VRAM/throughput thực |
| W10 — Nghiệm thu/phát hành | Regression, browser, benchmark, migration fixture và soak smoke có evidence | Render thực, soak 24 giờ, canary 24–72 giờ, diễn tập nâng dịch vụ |

**Các đường lỗi sửa trong lần nối tiếp này**

- Bot trước đó vẫn nạp queue bằng mã cũ và đọc flag trigger/clear/reorder. Startup hiện restore snapshot trước khi nhận lệnh, áp command bền vững và xác nhận theo ID. Active job thiếu bằng chứng được giữ ở `needs_review`.
- Telegram chờ quyền xử lý chung trước khi chuyển job thành active. Callback mang execution context, khóa giữ đến hết cleanup. Retry giữ identity và chuyển active về pending cùng snapshot.
- Import module render thất bại trong recovery hiện ghi lỗi và nhả reservation/lock. Cancellation ghi `stopped` trước khi kết thúc ownership.
- Binding `.tracker-location.json` cố định cả status và lease. Tạo `bot_system/control` sau khi API khởi động không đổi đường khóa của bot mới. Lease còn sống hoặc không đọc được ở vị trí khác chặn admission.
- Command legacy có timestamp dạng chuỗi được giữ để review, đồng thời không làm hỏng việc sắp xếp các command mới hợp lệ.
- Recovery bỏ tìm theo tên gần giống và cleanup thư mục suy đoán. Chỉ reuse artifact đã kiểm chứng theo manifest, nguồn và workspace. Output tồn tại nhưng chưa xác minh quyền sở hữu sẽ chặn thao tác. Restart tạo workspace mới, giữ artifact cũ.
- Preview recovery gắn lệnh tiếp tục với `execution_id` và `generation`. Preview của lần chạy cũ không thể mở pause của lần chạy mới cùng tên video.
- Lịch Telegram giữ mục tiêu qua restore, từ chối release quá hạn hoặc từ generation cũ. Hủy lịch chuyển job sang chờ thủ công. Windows có dependency `tzdata`; múi giờ sai trả lỗi validation.
- Browser phát hiện nút “Bắt đầu xử lý” bỏ qua selection. Nút hiện mở xác nhận cho đúng danh sách đã chọn. Khi phản hồi POST mất sau server commit, UI đối soát batch đã nhận.

**Bằng chứng kiểm tra**

| Kiểm tra | Kết quả |
|---|---|
| Python regression, 20 file suite | **191 passed, 4 skipped**, 19,40 giây, exit 0 |
| JavaScript, 3 file test | **5 passed**, 0 failed |
| Chromium với FastAPI ở cổng tạm | **5 kiểm tra đạt**, 0 lỗi JavaScript của trang |
| Benchmark API | **540 lượt đọc warm + 6 cold**, 0 lỗi warm, 1 và 2 client |
| Whitespace | `git diff --check` đạt |
| Tracker giữa hai tiến trình | Process thứ hai bị chặn khi lease cũ còn sống và thư mục control vừa tạo |

Evidence cục bộ nằm trong `test_evidence/dashboard/`, gồm `pytest.log`, `results.xml`, `run.json`, `browser.json`, `benchmark.json`. Thư mục này được ignore trong Git. Báo cáo này giữ số đo khi evidence chưa được chia sẻ.

Bốn skip có sẵn gồm ba test kỳ vọng tracker trước P0 và một test checkpoint cũ. Không thêm skip để làm xanh kết quả. Suite còn cảnh báo dependency về Starlette/httpx và Python `audioop`.

Browser tạo `A thử.mp4` và `B.mp4`, kiểm tra tải danh sách, giả lập logs lỗi nhưng status/input vẫn hoạt động, chọn A, làm mất phản hồi sau server commit, xác nhận lịch chỉ chứa A, hủy lịch, kiểm tra hai nguồn còn nguyên và preview recovery đúng Unicode. Harness dừng và chờ cả tiến trình preview con trước khi xóa dữ liệu test trên Windows. Browser chưa chạy model AI hoặc gửi Telegram.

Benchmark dùng video tổng hợp dài 0,4 giây, 20 input, 20 output và 20 bản ghi queue, đo qua ASGI TestClient. P95 theo endpoint, đơn vị mili giây.

| Endpoint | 1 client | 2 client |
|---|---:|---:|
| `/api/status` | 8,66 | 25,68 |
| `/api/queue` | 5,01 | 10,65 |
| `/api/phoi` | 11,11 | 29,41 |
| `/api/banve` | 9,83 | 32,49 |
| `/api/logs` | 3,59 | 8,92 |
| `/health/ready` | 7,95 | 18,86 |

RSS process benchmark tăng từ 128.274.432 lên 129.396.736 byte trong lượt đo ngắn. Số đo này chưa chứng minh độ bền bộ nhớ dài ngày hoặc hiệu năng khi render/GPU đang hoạt động.

**Chạy lại trong worktree**

Dùng Python có dependency dự án/test, `tzdata` trên Windows, Node.js và FFmpeg trên PATH. Browser cần Playwright cùng Chromium đã cài. Có thể truyền `--browser-executable` với đường dẫn trình duyệt hiện có.

```powershell
python tools/run_dashboard_acceptance.py
node --test tests/test_dashboard_progress.cjs tests/js/queue_schedule.test.js tests/js/dashboard_control_token.test.js
python tools/benchmark_dashboard.py --samples 30
python tools/check_dashboard_browser.py
```

Phiên này dùng executable Python của môi trường hiện có, bổ sung package test trong `scratch/dashboard-test-deps` và `scratch/dashboard-browser-deps` qua PYTHONPATH. Venv đang dùng giữ nguyên. Sandbox Windows chặn loopback khiến asyncio treo khi tạo event loop; các lượt thành công chạy với quyền loopback qua cơ chế approval của tool.

**Điểm nối tiếp và điều kiện phát hành**

1. Nghiệm thu corpus render riêng đến ffprobe/QC/receipt/archive, gồm Unicode, phụ đề cứng, không lời, nguồn hỏng và cùng stem. Giới hạn mẫu gọi AI thật theo kế hoạch.
2. Thực hiện 24 giờ soak với queue/polling/restart/fault injection trên fixture, sau đó 24–72 giờ canary có giới hạn nếu đạt. Regression và benchmark chưa thay thế thời gian quan sát này.
3. Diễn tập migration/rollback trong workspace thử. Chặn admission, drain writer, lưu inventory và snapshot/code nhất quán. Nâng API, bot, controller và supervisor đồng bộ vì code cũ không đọc binding tracker mới. Nếu bản mới đã nhận job, đối soát inventory/receipt trước khi dùng snapshot cũ.
4. Hoàn thiện W9 khi nền tảng đủ bằng chứng. Giữ feature flag tắt đến khi hai execution độc lập và GPU dùng theo lượt được kiểm chứng. Không nâng `MAX_SUPPORTED_WORKERS` chỉ để mở tùy chọn UI.

Phiên trước chưa chạy soak/canary, chưa thử render AI/GPU đầy đủ, chưa giao Telegram thật và chưa nâng cấp dịch vụ đang dùng.

**Nối tiếp nền tảng worker và chạy bền — 16/11/2026**

Đã đọc lại biên bản, status và diff tại đúng worktree cách ly trên commit gốc `4b2d234`. Khi bắt đầu có 54 file tracked thay đổi, cùng các file chưa tracked từ phiên trước; không reset hoặc gom commit các thay đổi này. Đối chiếu 30 hash baseline vẫn có 29 file nguyên vẹn, chỉ `backend/telegram_bot.py` khác từ lần triển khai dashboard trước. Lượt này không chỉnh file bot đó. Git cần `-c safe.directory` theo lệnh và quyền truy cập worktree trên Windows; không sửa global Git config.

Phần còn dang dở được xác định rõ: `execution_registry.py` và resource admission đã có mã nhưng chưa nối vào dispatcher. Cờ thử nghiệm trước đó khiến GET cấu hình báo hai worker trong khi POST và dispatcher chỉ dùng một. Hiện GET/POST cùng báo `max_supported_workers=1` và `effective_concurrency=1`, vẫn lưu lựa chọn mong muốn của người dùng. Cờ môi trường chỉ phục vụ thử nghiệm foundation với telemetry tiêm vào, không mở hai job sản xuất.

Các sửa chữa và kiểm tra mới:

- Registry kiểm tra schema, PID/creation time hữu hạn, generation, slot và output reservation. Dữ liệu hỏng chặn admission và giữ nguyên byte. PID còn sống nhưng không xác minh được creation time không bị thu hồi; PID/creation time kiểu boolean cũng không được coi là identity hợp lệ ở đường legacy. Test chứng minh stop/progress của hai lease độc lập, giới hạn hai slot, chống trùng output, stale callback, cleanup giữ slot và phục hồi sau tiến trình con thoát.
- Legacy batch admission kiểm tra registry dưới cùng khóa OS. Registry đang giữ slot hoặc không đọc được sẽ chặn batch legacy; lease legacy sống cũng chặn registry. Terminal status và yêu cầu stop chưa tự nhả slot.
- Resource admission trả về một slot khi cấu hình, RAM/VRAM, timestamp hoặc ngưỡng không hợp lệ. Kiểm tra RAM thấp, VRAM thấp, quá tải, telemetry cũ và auto-throttle bằng số đo fixture.
- GPU gate dọn context khi stop trước acquisition, lỗi setup, cancellation trong cleanup; kiểm tra stop lần nữa sau probe VRAM. Test khóa OS giữa thread/process và sibling asyncio task, reentry lồng nhau, hủy waiter và stage timeout giữ GPU đến khi native thread kết thúc. GPU/model được giả lập.
- Sửa inventory migration đọc file `.owner.lock` đang bị Windows khóa. File đồng bộ này được loại khỏi backup/inventory; trạng thái queue/config vẫn kiểm tra hash và rollback từ chối dữ liệu mới sau nâng cấp.
- `tools/soak_dashboard.py` mặc định 86.400 giây, tạo runtime tạm và child process riêng. Poll sáu API khi queue/tracker còn active, xác minh đúng execution ID rồi mới drain; giả lập lỗi ghi snapshot, snapshot hỏng, mất ACK sau commit, thoát đột ngột khi còn job rồi khôi phục đúng ID. Poll interval tối đa 10 giây, request không quá 5 giây, khoảng cách poll không quá interval + 5 giây; lượt cuối phải có phản hồi thành công sau thời lượng yêu cầu. Báo cáo ghi độ phủ, độ trễ, RSS/thread/handle và lỗi; dùng atomic writer retry lỗi chia sẻ Windows. Giữ tối đa 256 sample/segment và 16 segment gần nhất trong JSON, đồng thời append summary mọi segment vào JSONL. Khi lỗi, giữ runtime fixture và stderr cuối tối đa 64 KiB. `soak_completed` chỉ có thể true khi đủ thời gian quan sát, độ phủ hợp lệ và xác minh recovery cuối; không hỗ trợ resume một lượt bị ngắt.

| Kiểm tra lượt nối tiếp | Kết quả |
|---|---|
| Runner Python mở rộng, 30 file | **322 passed, 7 skipped**, 34,23 giây pytest, exit 0 |
| JavaScript, 3 file | **5 passed**, 0 failed |
| Migration/rollback CLI | **4 kiểm tra đạt**, chỉ fixture |
| Soak smoke | **120,359 giây quan sát**, 129,875 giây tổng, exit 0 |
| Queue/API trong smoke | **93 vòng queue, 558 request** khi queue/tracker active, không lỗi ngoài lỗi 503 được chủ động tiêm |
| Độ phủ/độ trễ smoke | **100% độ phủ**, request lớn nhất **78 ms**, khoảng cách poll lớn nhất **1,453 giây** |
| Fault/restart/recovery | **15 fault checks, 4 lần thoát đột ngột, 4 recovery checks** |
| Tài nguyên child smoke | RSS peak **132.227.072 byte**, tối đa **20 thread**, **442 handle** |
| `git diff --check` | Đạt; Git chỉ cảnh báo chuẩn hóa CRLF ở file có sẵn |

Bảy skip đều thuộc quarantine có sẵn: ba tracker cũ, một checkpoint, hai resource-control và một separator-lock. Không thêm skip. Hai warning còn lại từ Starlette/httpx và `audioop`.

Evidence bản cuối: `test_evidence/dashboard/continuation-20261116-final/{run.json,results.xml,pytest.log}`, `test_evidence/dashboard/soak-smoke-final.json` và `soak-smoke-final.segments.jsonl`. Evidence JavaScript/migration ở `test_evidence/dashboard/continuation-20261116/{node.log,migration.json}`. Thư mục evidence được Git ignore. Runner đã thêm các suite worker foundation/bridge, GPU foundation, soak và migration vào danh sách mặc định. Status cuối có 54 file tracked thay đổi và 28 mục untracked (gồm thư mục), chưa stage/commit; 29/30 hash baseline vẫn nguyên vẹn như lúc nhận việc.

Chạy lại bằng môi trường Python/dependency đã mô tả ở trên:

```powershell
python tools/run_dashboard_acceptance.py --output test_evidence/dashboard/continuation-20261116-final
python tools/dashboard_migration_rehearsal.py
python tools/soak_dashboard.py --duration-seconds 120 --restart-seconds 30 --poll-interval 1 --output test_evidence/dashboard/soak-smoke-final.json
# Nghiệm thu 24 giờ với restart có chủ đích mỗi 5 phút:
python tools/soak_dashboard.py --output test_evidence/dashboard/soak-24h.json
```

**Điều kiện còn mở sau lượt này**

- `MAX_SUPPORTED_WORKERS=1` giữ nguyên. Cần nối registry vào dispatcher/tracker/stop/recovery và nghiệm thu hai execution đầu cuối trước khi bật W9. History registry hiện giữ ID đã release để fence callback; chưa có chính sách retention phù hợp chạy dài ngày.
- Scope GPU hỗ trợ nested native work trong một async owner. Nếu tương lai tạo nhiều native call trực tiếp song song dưới cùng owner, chúng có thể kế thừa scope tái nhập; cần giải quyết và kiểm thử trước khi mở kiểu fan-out đó. Orchestrator hiện tại chạy các native call này tuần tự.
- Smoke chỉ chứng minh harness và đường lỗi ngắn hạn. Các process smoke được restart mỗi khoảng 30 giây, nên RSS/thread/handle trên chưa chứng minh không rò rỉ qua ngày. Lượt mặc định cũng restart có chủ đích; cần thêm lượt process sống lâu với `--restart-seconds 86400` để đánh giá rò rỉ trong một process. Receipt/tombstone/command fixture được reset sau mỗi segment, không đo tích lũy history. Input/output listing của smoke rỗng; corpus media đã đo riêng trong benchmark trước. Chưa đặt ngưỡng nghiệm thu tăng RAM/VRAM, chưa đo throughput render thực.
- Chưa chạy đủ 24 giờ, canary 24–72 giờ, corpus AI/GPU/Telegram thật hoặc nâng dịch vụ. Migration hiện chỉ diễn tập trạng thái file fixture. Không có soak chạy nền được bỏ lại sau lượt này. Mọi thay đổi vẫn ở working tree, chưa commit.
