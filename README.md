# AutoDub Video Tools

Repository này chứa mã nguồn và tài liệu cho ba nhóm công cụ độc lập: **Tool V1**, **Tool V2** và **AI Video Research Tool**. Các nhánh có thể khác phiên bản; chọn đúng nhánh trước khi cài hoặc chạy. README này là trang điều hướng chung, không phải bằng chứng rằng mọi nhánh hay dịch vụ đang chạy trên máy.

## Mục lục

1. [Chọn đúng công cụ và nhánh](#1-chọn-đúng-công-cụ-và-nhánh)
2. [Tính năng theo từng hệ thống](#2-tính-năng-theo-từng-hệ-thống)
3. [Luồng xử lý Tool V2](#3-luồng-xử-lý-tool-v2)
4. [Cấu trúc repository](#4-cấu-trúc-repository)
5. [Cài đặt và khởi động Tool V2](#5-cài-đặt-và-khởi-động-tool-v2)
6. [Cấu hình model và thư mục](#6-cấu-hình-model-và-thư-mục)
7. [Kiểm thử và nghiệm thu](#7-kiểm-thử-và-nghiệm-thu)
8. [Bảo mật, dữ liệu và giới hạn](#8-bảo-mật-dữ-liệu-và-giới-hạn)
9. [Tài liệu tiếp tục đọc](#9-tài-liệu-tiếp-tục-đọc)

## 1. Chọn đúng công cụ và nhánh

| Công cụ | Nhánh nên dùng | Mục đích | Cổng / điểm vào |
| --- | --- | --- | --- |
| Tool V1 | [tool-v1](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1) | Pipeline V1 và dashboard giám sát độc lập | Dashboard mặc định 8088 |
| Tool V2 | [tool-v2](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v2) | Pipeline mới hơn, bot Telegram, dashboard, xử lý cục bộ và QC | Dashboard 8089; API desktop 8000 |
| Tool tìm kiếm video | [tool-tim-kiem-video](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-tim-kiem-video) | Ứng dụng riêng để nghiên cứu/tìm kiếm và quản lý kết quả | Xem README của nhánh |
| Trang tổng hợp | [main](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/main) | Mục lục và nhánh đích cho các thay đổi chung; có thể chậm hơn nhánh tính năng | Không phải cam kết phiên bản đang chạy |

Nhánh **refactor/pipeline-v2** hiện trùng nội dung với **tool-v2**; dùng **tool-v2** làm tên chuẩn. **ai-video-research-tool** là tên nhánh tương thích của ứng dụng tìm kiếm; ưu tiên **tool-tim-kiem-video**. Luôn kiểm tra commit/PR hiện hành trước khi cập nhật máy chạy.

## 2. Tính năng theo từng hệ thống

### Tool V1

Tool V1 giữ pipeline giám sát/render kiểu V1, với các chức năng chính:

- Nhận video từ thư mục đầu vào, chạy lần lượt theo hàng đợi và bỏ qua sản phẩm đã tồn tại theo quy tắc của pipeline.
- Theo dõi video hiện tại, bước đang chạy, phần trăm, thời gian và trạng thái lỗi/hoàn thành.
- Các bước xử lý gồm trích âm thanh, tách giọng, nhận dạng lời nói, OCR phụ đề gốc, dịch, tạo giọng/lồng tiếng và render.
- Dashboard xem danh sách đầu vào/thành phẩm, log, xem thử video, điều khiển chạy/dừng/tạm dừng/tiếp tục, quản lý hàng đợi và thử lại job khi được hỗ trợ.
- Tùy chọn giọng/âm thanh và các trang A2UI/Studio có trong nhánh V1.

Model thực tế có thể khác theo cấu hình và bản V1 đang dùng. Xem cấu hình local và [tài liệu bàn giao V1](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-v1/DASHBOARD_MONITOR_HANDOVER.md); không dùng README này để suy ra model đã chạy cho một video cụ thể.

### Tool V2

Tool V2 mở rộng xử lý theo job/stage và bổ sung khả năng phục hồi, kiểm định:

- Nhận video từ Telegram dưới dạng link hoặc file; có hàng đợi tuần tự được lưu bền vững.
- Xử lý video cục bộ theo batch; ghi manifest, trạng thái stage và artifact để hỗ trợ tiếp tục job sau gián đoạn.
- Khóa tài nguyên GPU, chạy model nặng trong worker riêng khi cấu hình bật, giới hạn dữ liệu theo batch và đặt timeout.
- Kiểm tra môi trường bằng preflight; kiểm tra chất lượng đầu ra bằng QC gate có chế độ report, warn hoặc block.
- Dashboard theo dõi tiến độ, log, hàng đợi, lịch sử, báo cáo QC, thư mục vào/ra, retry và điều khiển job.
- Có giao diện Electron/React/Vite cho công việc video cục bộ; API HTTP riêng; Telegram bot chạy độc lập.
- Có trang Workflow để chỉnh canvas/khung hình, xem dữ liệu phụ đề và xuất bản chỉnh sửa; Script Studio hỗ trợ phân tích/tạo kịch bản và TTS theo các route được mount trong dashboard/API.

### Các model và provider của Tool V2

Cấu hình hiện tại nằm trong **backend/.env.example** và chính sách runtime trong **backend/ai/model_policy.py**:

| Công đoạn | Cấu hình mặc định/đường dự phòng được khai báo |
| --- | --- |
| Tách giọng | BS-RoFormer khi runtime/model sẵn sàng; Demucs là đường dự phòng |
| ASR và timestamp | Qwen3-ASR 0.6B cùng ForcedAligner; Faster-Whisper Large-v3 là đường dự phòng |
| OCR | PP-OCRv6; có đường fallback theo cấu hình/runtime |
| Dịch | Gemini 3.8 Flash là model mặc định trong cấu hình mẫu; provider/model fallback có thể được chọn tùy cấu hình và tình trạng dịch vụ |
| Giọng đọc | Edge-TTS và các provider tùy chọn; RVC chỉ chạy khi model và cấu hình cần thiết có sẵn |
| Trộn/render | FFmpeg; NVENC được dùng khi môi trường NVIDIA tương thích |

Đây là mô tả cấu hình mặc định, **không phải cam kết model đã xử lý mọi video**. Provider dự phòng có thể được gọi khi lỗi/quota; hãy xem log job để biết model/provider thực tế. README không thay đổi model đang dùng.

### AI Video Research Tool

Đây là ứng dụng riêng, có lịch sử và hướng dẫn cài đặt riêng trên nhánh **tool-tim-kiem-video**. Tính năng, cấu hình trình duyệt, dữ liệu và kiểm thử được mô tả trong [README của ứng dụng](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-tim-kiem-video/README.md). Không dùng script khởi động của V1/V2 cho ứng dụng này.

## 3. Luồng xử lý Tool V2

Dashboard V2 theo dõi 15 stage nội bộ; giao diện có thể nhóm thành các thẻ lớn hơn:

1. Tiếp nhận video.
2. Trích xuất âm thanh.
3. BS-RoFormer hoặc Demucs dự phòng.
4. Qwen3-ASR và căn timestamp, fallback Whisper.
5. PP-OCRv6 đọc vùng phụ đề.
6. Dịch theo provider/model đã cấu hình.
7. Căn thời gian lời đọc.
8. Tổng hợp TTS.
9. Chuyển giọng RVC nếu bật.
10. Tạo phụ đề.
11. Trộn âm legacy nếu cấu hình yêu cầu.
12. Trộn âm V2 nếu bật.
13. Render video.
14. Kiểm tra QC.
15. Xuất bản thành phẩm.

Một stage có thể bị bỏ qua nếu không áp dụng hoặc đã có artifact hợp lệ. Trạng thái stage trong manifest/log là nguồn kiểm tra chi tiết; không suy diễn chỉ từ phần trăm tổng.

## 4. Cấu trúc repository

Không di chuyển các script/launcher trong đợt tài liệu này vì một số đường dẫn được gọi trực tiếp. Các thư mục được phân loại theo vai trò:

~~~text
backend/
  ai/                  Model, provider và xử lý AI
  model_workers/       Worker cô lập cho model nặng
  pipeline_v2/         Orchestrator, stage, manifest, resume, QC, GPU lock
  templates/           Dashboard, Workflow và giao diện web
  scripts_and_tests/   Công cụ phân tích/debug lịch sử; không phải test suite chuẩn
frontend/               Ứng dụng React + Vite + Electron
tests/                  Bộ test Python chính
scripts/                Smoke test, profiler, setup và tiện ích bảo trì
docs/                   Hướng dẫn vận hành và ghi chú kỹ thuật
docs/baseline/           Tài liệu/fixture đối chiếu V1
fixes_for_v1/            Tiện ích sửa/rollback dành riêng cho V1
MyVoiceModel_v2/         Model/index RVC được quản lý bằng Git LFS
*.bat, *.vbs             Launcher và tiện ích Windows ở thư mục gốc
~~~

Các file bàn giao/phiên bản ở thư mục gốc được lập chỉ mục trong [docs/README.md](docs/README.md).

## 5. Cài đặt và khởi động Tool V2

Các lệnh dưới đây dành cho Windows PowerShell và checkout nhánh **tool-v2**.

### Chuẩn bị

- Windows 10/11; Python 3.10 được dùng trong hướng dẫn V2 hiện tại.
- NVIDIA driver/GPU nếu cần tăng tốc CUDA/NVENC; FFmpeg và ffprobe cần có trong PATH.
- Cài bản PyTorch/TorchAudio có CUDA phù hợp trước khi cài các dependency còn lại.
- Node.js LTS tương thích với Vite hiện tại, dùng cho giao diện Electron/React.
- Git LFS nếu cần tải model RVC trong repository. Các model AI lớn khác có thể được tải vào cache khi chạy.

### Cài môi trường

~~~powershell
git clone --branch tool-v2 https://github.com/meolucky051101001-cyber/toll-edit-video.git
cd toll-edit-video
git lfs install
git lfs pull

py -3.10 -m venv backend\venv
# Cài PyTorch CUDA tương thích với driver trước, sau đó:
.\backend\venv\Scripts\python.exe -m pip install -r requirements.txt

py -3.10 -m venv backend\model_venv
.\backend\model_venv\Scripts\python.exe -m pip install -r backend\requirements-models.txt

Copy-Item backend\.env.example backend\.env
~~~

Mở **backend/.env** và tự điền Telegram/Gemini/provider secrets, đường dẫn model, workspace và thư mục video. Không commit file **backend/.env**. Hãy đọc hướng dẫn rollout trước khi chạy job thật.

### Preflight và chạy

~~~powershell
cd backend
.\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface all
cd ..
~~~

Chỉ nhận job thật khi preflight báo **ready=True** và không còn lỗi cấu hình quan trọng.

- **Telegram bot + Electron UI:** chạy **start_bot.bat** từ thư mục gốc. Script preflight, cài npm dependency nếu thiếu, khởi động giao diện Electron/Vite và bot Telegram. Electron khởi chạy API desktop theo cấu hình của nó.
- **Dashboard giám sát V2:** chạy riêng lệnh dưới đây; mở **http://127.0.0.1:8089**:

~~~powershell
.\backend\venv\Scripts\python.exe .\backend\dashboard_monitor.py
~~~

- **Batch cục bộ:** chạy **run_batch_edit.bat**. Kiểm tra đường dẫn đầu vào/đầu ra trước; script hiện đặt fallback riêng nếu biến môi trường chưa được khai báo.
- **API desktop:** **backend/main.py** phục vụ API ở **http://127.0.0.1:8000** khi chạy trực tiếp hoặc được Electron khởi chạy. Tài liệu API: **http://127.0.0.1:8000/docs**.
- **Workflow/Script Studio:** được mount trên Dashboard Monitor V2; các trang chính là **/quy-trinh** và **/kich-ban** khi service tương ứng đang chạy.

Không chạy cùng lúc hai dịch vụ khác nhau trên cùng cổng. AI Video Research Tool cũng dùng cổng 8000 theo cấu hình mặc định; cần đổi cổng một bên nếu chạy đồng thời.

## 6. Cấu hình model và thư mục

Tạo cấu hình local từ **backend/.env.example**. Các nhóm biến thường cần xem:

| Nhóm | Biến tiêu biểu | Mục đích |
| --- | --- | --- |
| Chế độ pipeline | PIPELINE_MODE | legacy, shadow hoặc v2 |
| Secret/provider | BOT_TOKEN, GEMINI_API_KEY, FPT_API_KEY | Kết nối dịch vụ; chỉ lưu local |
| Model | SOURCE_SEPARATOR_BACKEND, ASR_BACKEND, OCR_BACKEND, GEMINI_MODEL | Chọn backend/model và đường dự phòng |
| Runtime | MODEL_RUNTIME_PYTHON, AUTODUB_MODEL_CACHE | Python/model cache riêng |
| Thư mục | AUTODUB_WORKSPACE, AUTODUB_INPUT_DIR, AUTODUB_OUTPUT_DIR | Nơi lưu job, đầu vào và thành phẩm |
| Chất lượng | QC_GATE_POLICY | report_only, warn hoặc block; production được khuyến nghị block |
| Hiệu năng | ENABLE_GPU_PROCESS_ISOLATION, ENABLE_STAGE_CACHE, các batch limit | Điều chỉnh worker/cache/batch theo máy |
| Âm thanh | ATEMPO_MIN, ATEMPO_MAX, TARGET_LUFS, TRUE_PEAK_MAX_DBTP | Căn thời lượng và mức âm thanh |

Các launcher có thể đặt biến môi trường trước khi Python đọc file .env; biến đã có trong process thường được ưu tiên hơn giá trị trong .env. Vì vậy hãy kiểm tra **run_batch_edit.bat** và **backend/.env** cùng nhau nếu input/output không khớp. Đặt workspace trên ổ cục bộ, ngoài OneDrive/thư mục đồng bộ.

## 7. Kiểm thử và nghiệm thu

Chạy từ thư mục gốc với Python environment đã cài dependency:

~~~powershell
.\backend\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
~~~

Kiểm tra frontend:

~~~powershell
cd frontend
npm ci
npm run lint
npm run build
cd ..
~~~

Các unit/regression test không thay thế kiểm thử video thật. Trước production, chạy preflight, xử lý một video ngắn có quyền sử dụng, nghe/xem đầu ra, kiểm tra QC report và manifest. Tích hợp Gemini, TTS, RVC, GPU và NVENC phụ thuộc key, model, driver và cấu hình máy.

## 8. Bảo mật, dữ liệu và giới hạn

- Không đưa **backend/.env**, Telegram token, API key, cookie/profile trình duyệt, log chứa dữ liệu riêng, URL đầu vào cá nhân hoặc video vào commit.
- Nhánh cleanup có mẫu **urls.example.txt**; dùng bản local **urls.txt** và giữ file này ngoài Git.
- **workspace/**, cache model, media và log là dữ liệu runtime; sao lưu/di chuyển riêng, không xem chúng là source code.
- Dashboard/API mặc định phục vụ local; không mở ra mạng công cộng nếu chưa bổ sung xác thực và kiểm soát truy cập.
- Tool không xác nhận quyền sử dụng nội dung, không bảo đảm quyền đăng/kiếm tiền và không thay thế bước kiểm tra giấy phép. Chỉ xử lý nội dung bạn sở hữu hoặc được phép sử dụng; tuân thủ điều khoản nền tảng và pháp luật.
- Kết quả tải, nhận dạng, dịch, lồng tiếng và QC phụ thuộc chất lượng nguồn, provider/model và môi trường. Luôn xem lại thành phẩm trước khi xuất bản.

## 9. Tài liệu tiếp tục đọc

- [README_FIRST.md](README_FIRST.md) — lối vào nhanh cho người cài đặt.
- [VERSIONS.md](VERSIONS.md) — nhánh nào chứa công cụ nào.
- [TOOL_V2_FULL_GUIDE.md](TOOL_V2_FULL_GUIDE.md) — hướng dẫn vận hành/cấu hình V2 chi tiết.
- [docs/pipeline_v2_rollout.md](docs/pipeline_v2_rollout.md) — preflight, mode, resume, QC và rollout.
- [CODE_HANDOFF_2_12.md](CODE_HANDOFF_2_12.md) — ghi nhận bàn giao và giới hạn nghiệm thu V2.
- [PHASE4_ACCEPTANCE.md](PHASE4_ACCEPTANCE.md), [PHASE5_ACCEPTANCE.md](PHASE5_ACCEPTANCE.md) — phạm vi và bằng chứng kiểm thử từng đợt.
- [docs/README.md](docs/README.md) — mục lục tài liệu kỹ thuật.
- [V1 handover](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-v1/DASHBOARD_MONITOR_HANDOVER.md) — dashboard/pipeline V1.
- [AI Video Research Tool README](https://github.com/meolucky051101001-cyber/toll-edit-video/blob/tool-tim-kiem-video/README.md) — hướng dẫn ứng dụng tìm kiếm riêng.
