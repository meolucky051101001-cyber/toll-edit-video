# Phase 5 — môi trường Python 3.12 song song

## Phạm vi

Môi trường chuẩn bị: `backend/venv_py312` (Python 3.12.14). Không cài/gỡ hay sửa
`backend/venv`, `backend/model_venv`, `backend/model_venv_separator`; không dừng
dịch vụ đang chạy. Phase 4 cùng bản bổ sung đã được tích hợp fast-forward vào
`tool-v1` tại `932c389`, trước khi tạo nhánh `phase-5-env-requirements`.

Thay đổi package duy nhất trong Phase 5 là gỡ `qwen-asr==0.0.6` khỏi venv_py312.
Không cài lại Qwen, RVC, fairseq hoặc dependency cho các backend đã bỏ. Không
nâng/hạ các package còn lại khi chúng đang thỏa requirements và pip check sạch.

## Đối chiếu import và requirements

Quét AST 304 file Python dưới backend, loại trừ venv, model_venv, node_modules
và bytecode. Không có lỗi parse. Các khai báo mới dựa trên import thực tế:

| Package | Nơi sử dụng |
| --- | --- |
| soundfile, scipy | `ai/audio_enhancer.py`, `ai/v1_sensevoice_adapter.py`, mixer và quality gate |
| onnxruntime | `ai/v1_sensevoice_adapter.py` (SenseVoice trong runtime chính) |
| pydantic | route API, `ai/scriptwriting.py`, cấu trúc whiteboard |
| google-genai | `whiteboard_animator/detect_regions.py`, CLI whiteboard |
| resvg-py | `ai/doodle_engine.py`, `ai/satori_renderer.py` |
| scikit-image | whiteboard animator, các bước handdraw |
| safetensors | `handdraw/subject.py` |

Demucs còn được dùng qua `python -m demucs`, không được bỏ chỉ vì thiếu import
trực tiếp. `cryptography` còn phục vụ `tools/sheet_crypto.py` ngoài backend.
PyTorch, TorchAudio và TorchVision cần bộ wheel CUDA khớp nhau, cài trước
requirements; không tự đổi chúng trong Phase 5.

Các constraint liên quan: `numpy>=1.23.5,<2`, `protobuf<6`, và giữ chính xác
`opencv-python-headless==4.10.0.84` đã tương thích NumPy 1.x. Venv hiện có
NumPy 1.26.4, protobuf 5.29.6, OpenCV 4.10.0.84.

Không gộp PaddleOCR/audio-separator vào runtime chính: chúng có requirements
riêng cho model_venv/model_venv_separator. CapCut TTS, Vie-TTS và PyNvVideoCodec
là provider/tính năng tùy chọn, không tự cài vào core. Thay khai báo SDK Google
cũ không còn import trong requirements-dev bằng `google-genai` đang được dùng.

## File lock và giới hạn tái tạo

`requirements.lock.txt` là snapshot đầy đủ từ lệnh sau, sau khi pip check sạch:

```powershell
.\backend\venv_py312\Scripts\python.exe -B -m pip check
.\backend\venv_py312\Scripts\python.exe -B -m pip freeze
```

`requirements.lock` chỉ là entry point tương thích, include file chuẩn bằng
`-r requirements.lock.txt`; không duy trì hai danh sách version độc lập.

Snapshot này mô tả môi trường trên máy hiện tại, không phải bộ cài offline:

- `capcut-tts-api==1.0.0` được cài từ wheel local. `pip freeze` ghi tham chiếu
  `file:` đúng metadata. Đường dẫn build gốc đã mất; ngày 01/11/2026 đã phục hồi
  wheel từ các file đang cài sau khi đối chiếu SHA256/size với RECORD. Đã cài lại
  offline bằng `--no-deps --no-index` chỉ trong venv_py312, xác nhận cả 8 file mã
  nguồn giữ nguyên hash và pip check sạch. Lock hiện trỏ tới
  `backups/wheels/capcut_tts_api-1.0.0-py3-none-any.whl`, có hash trong URL.
  Đây là archive phục hồi, không khẳng định giống từng byte wheel gốc hoặc có
  chữ ký nhà phát hành. Wheel được ignore, cần sao lưu riêng cùng lock khi chuyển
  máy; xem [báo cáo chuẩn bị Phase 7](pre_phase7_hardening.md).
- Torch 2.5.1+cu124 / TorchAudio 2.5.1+cu124 / TorchVision 0.20.1+cu124 cần nguồn
  wheel CUDA phù hợp. `pip freeze` không lưu cấu hình index đã dùng lúc cài.
- Python base của venv_py312 nằm trong cache runtime Codex, được ghi trong
  `pyvenv.cfg`. Không xóa/di chuyển Python base đó khi còn dùng venv này.
- Các gói cũ khác vẫn có trong freeze vì đây là snapshot chính xác; không tự
  autoremove dependency sau khi bỏ Qwen. Không còn bốn package bị loại là
  qwen-asr, qwen-omni-utils, rvc-python, fairseq.

## Preflight và kiểm thử

Chạy full suite bằng interpreter rõ ràng, không dùng activation hoặc pytest.exe:

```powershell
.\backend\venv_py312\Scripts\python.exe -B -m pytest tests -q -p no:cacheprovider
```

Mốc trước Phase 5 là 1095 passed / 0 failed / 79 skipped sau bổ sung Phase 4;
mốc bàn giao ban đầu là 1090 passed / 79 skipped. Không thêm skip hoặc sửa assert
để đạt mốc.

Preflight `--interface all` tạo rồi xóa file tạm để kiểm tra quyền ghi thư mục.
Trong lượt chuẩn bị này, AUTODUB_WORKSPACE/AUTODUB_OUTPUT_DIR chỉ được override
cho process preflight tới thư mục tạm dưới `scratch/phase5`, để không ghi vào
D:\workspace hoặc D:\banve. Cấu hình secret vẫn đọc từ backend/.env; không in giá
trị secret. Kết quả ready của lượt này xác nhận module, binary, CUDA và runtime
model cùng các thư mục tạm; chưa xác nhận quyền ghi output production hoặc một
lượt render thật bằng Python 3.12. Một số subprocess của pipeline vẫn trỏ tới
`backend/venv` cho tới khi người dùng cutover; không đổi đường đi pipeline ở Phase 5.

## Cutover/rollback — chỉ người dùng tự chạy sau

Đã bổ sung [health check chỉ đọc sau cutover](integration_stages_abcd.md#stage-d--health-check-chỉ-đọc-sau-cutover):
`backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py`.
Mặc định kỳ vọng Python 3.12, ngân sách 5 giây; import quá hạn báo FAIL/incomplete,
có thể chạy lại với `--timeout 30`. Lệnh này không dừng/chuyển/khởi động dịch vụ.

Các script được viết và kiểm tra cú pháp; **không chạy trong Phase 5**.

1. Chọn lúc không còn job đang xử lý. Tự dừng Tool V1 bằng `TatHoanToan.bat`.
   Đảm bảo các supervisor/watchdog và lịch khởi động tự động không bật dịch vụ
   trở lại trong lúc đổi thư mục. Script không tự dừng tiến trình hoặc sửa Task Scheduler.
2. Từ console Windows, chạy `scripts\cutover_py312.bat` khi đã sẵn sàng.
   Script kiểm tra các cổng 8088/8090, tiến trình Tool V1, đúng phiên bản Python,
   đường dẫn và pip check của venv 3.12. Nếu không đọc được tiến trình thì dừng;
   dùng console nâng quyền khi cần quyền đọc trạng thái.
3. Cutover chỉ rename `backend\venv` → `backend\venv_py310`, sau đó
   `backend\venv_py312` → `backend\venv`. Không ghi đè backup có sẵn, không dùng
   junction/symlink và không xóa dữ liệu. Nếu bước giữa thất bại, script thử khôi
   phục tên thư mục; nếu không khôi phục được thì báo rõ để kiểm tra thủ công.
4. Sau khi rename thành công, chỉ dùng interpreter đích trực tiếp:

   ```powershell
   .\backend\venv\Scripts\python.exe -B -m pip check
   .\backend\venv\Scripts\python.exe -B backend\v1_preflight.py --interface all
   ```

   Lượt preflight thủ công này sẽ kiểm tra các thư mục production theo cấu hình
   thực. Tự khởi động dịch vụ và thử video ngắn sau khi các kiểm tra đều đạt.
5. Khi cần rollback, tự dừng lại mọi dịch vụ rồi chạy
   `scripts\rollback_py310.bat`. Nó rename `venv` (3.12) → `venv_py312`, rồi
   `venv_py310` → `venv`. Không bắt buộc pip check sạch ở venv 3.10 đã tồn tại vì
   mục tiêu rollback là khôi phục nguyên trạng, không sửa môi trường cũ.

Quan trọng: rename không cập nhật đường dẫn nhúng trong `pip.exe`, `pytest.exe`,
`uvicorn.exe`, `activate.bat` hoặc activation Git Bash. Dùng
`backend\venv\Scripts\python.exe -m pip`, `-m pytest`, `-m uvicorn`; không dùng
những launcher/activation cũ sau khi đổi tên. Script không sửa chúng để giữ đúng
phạm vi chỉ rename và bảo toàn môi trường cũ. Các launcher dịch vụ Tool V1 hiện
dùng `backend\venv\Scripts\python.exe`/`pythonw.exe` trực tiếp.

Script không khởi động lại dịch vụ sau khi chuyển. Kiểm tra syntax không thay
thế việc kiểm chứng cutover/rollback thật; bước đó được dành cho người dùng.

## Nghiệm thu phần chuẩn bị — 31/10/2026

| Kiểm tra | Kết quả |
| --- | --- |
| pip check venv_py312 | No broken requirements found |
| Full pytest Python 3.12 | 1095 passed / 0 failed / 79 skipped; 110.03 giây |
| Full pytest Python 3.10 | 1095 passed / 0 failed / 79 skipped / 185 subtests passed; 150.18 giây |
| Preflight Python 3.12, interface all | ready=true; 25 kiểm tra pass, workspace/output tạm như mô tả trên |
| Lock | 149 dòng, khớp chính xác pip freeze; không có 4 package bị loại |
| Script | Parser Windows PowerShell 5.1 hợp lệ; BAT/PS1 CRLF; chưa chạy chuyển môi trường |

Mốc trước Phase 5: 1095 passed / 79 skipped / 185 subtests (Python 3.10).
Python 3.12 chưa cài pytest-subtests nên không có bộ đếm subtest riêng. Không
thêm/sửa test hoặc skip trong Phase 5; không sửa source pipeline. API dashboard
vẫn HTTP 200 và PID lắng nghe 8088/8090 không đổi tại lúc kiểm tra.

Log kiểm thử, JSON preflight và bằng chứng chi tiết ở `scratch/phase5` (được
ignore, không commit dữ liệu runtime). Phase 5 dừng ở chuẩn bị môi trường song
song; chưa đổi tên venv, chưa restart dịch vụ, chưa chuyển sang Phase 6.
