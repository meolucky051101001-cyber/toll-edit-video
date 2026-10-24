# TRIỂN KHAI NÂNG CẤP STUDIO KỊCH BẢN — 2026-10-18

Tài liệu này ghi lại **những gì đã được triển khai thật** vào repo sau khi đánh giá 20 repo
(xem `docs/DANH_GIA_20_REPO_ANIMATION_VIDEO_2026-10-18.md`), gồm 2 đợt đầu: đợt 1 là sidecar
Satori/Remotion + Agent Skills; đợt 2 là **tích hợp toàn phần vào `/kich-ban`** (orchestrator,
UI Studio, Howler, Lottie, FFmpeg). Các đợt sau: **bot Telegram** (§8), **copy gương ổ D** (§9)
và **đổi mặc định diễn hoạt — bỏ tay vẽ, 12 cảnh, giọng BV075** (§10). Tất cả thay đổi đều
**an toàn mặc định**: mọi lỗi tự động lùi về engine PIL, không bao giờ làm hỏng pipeline sản xuất.

> ⚠️ **Sau khi cập nhật code cần khởi động lại backend** để nạp `.env` mới (2 cờ bên dưới).

---

## 0. Tổng quan

| # | Hạng mục | Trạng thái | File chính | Cách bật |
|---|----------|-----------|------------|----------|
| 1 | Agent Skills (GSAP / Remotion / Motion) | ✅ Đã cài (OpenCode + Antigravity) | `.opencode/skills/`, `.agent/skills/` | Tự động (agent tự nạp) |
| 2 | Satori — Band A banner tiêu đề | ✅ Tích hợp **2 đường render** + toggle UI | `backend/ai/satori_renderer.py`, `backend/ai/whiteboard_hand_engine.py` | Checkbox "✨ Tiêu đề Satori" / `SCRIPT_SATORI_BAND_A=1` |
| 3 | Remotion — engine doodle | ✅ **Đã nối vào orchestrator** (5 art + 6 camera + Lottie) | `backend/ai/remotion_renderer.py`, `tools/render_node/remotion/` | "Phong cách diễn hoạt" = Remotion / `SCRIPT_REMOTION_ENABLED=1` |
| 4 | Howler.js — nghe liền mạch lời thoại | ✅ | `backend/templates/assets/howler.min.js`, `script_studio.html` | Nút mới trong card "Chi Tiết Các Phân Cảnh" |
| 5 | Lottie — sparkle trang trí trong video | ✅ (asset tự soạn, MPL-safe) | `tools/render_node/remotion/lottie/sparkle.json` | Tự động với `showcase` / `brain_think` |
| 6 | FFmpeg `+faststart` | ✅ | `backend/ai/doodle_engine.py` | Tự động |
| 7 | API năng lực engine | ✅ `GET /api/script/render-capabilities` | `backend/script_api.py` | UI tự gọi khi mở trang |
| 8 | Test tự động | ✅ 23 tests (2 test nặng có gate) | `tests/test_satori_band_a.py`, `tests/test_remotion_renderer.py`, `tests/test_telegram_script_bot.py`, `tests/test_doodle_mirror_output.py` | `pytest` |
| 9 | Bot Telegram Studio Kịch Bản (prompt → kịch bản → video) | ✅ Đã chạy nền dưới Task Scheduler | `backend/telegram_script_bot.py`, `backend/background_service.py` | Task `AutoDubV1-scriptbot` / `TELEGRAM_SCRIPT_BOT_TOKEN` |
| 10 | Đổi mặc định diễn hoạt: **bỏ tay vẽ** · **12 cảnh** · giọng **BV075 Thanh Niên Tự Tin** | ✅ Mặc định sẵn (đợt 5) | `backend/templates/script_studio.html`, `backend/telegram_script_bot.py`, `backend/script_api.py`, `backend/ai/doodle_engine.py` | Không cần bật — xem §10 |
| 11 | Studio ưu tiên Gemini bản mới (**gemini-3.6-flash**) khi viết kịch bản | ✅ (đợt 5) | `backend/ai/scriptwriting.py`, `backend/ai/prompt_to_script.py`, `backend/script_api.py`, `backend/.env` | `GEMINI_SCRIPT_MODEL` (rỗng = theo `GEMINI_MODEL`; xem §11) |

Yêu cầu môi trường (đã có trên máy): Node.js ≥ 18 (đang dùng v26), npm 11, FFmpeg 8.1.2.

---

## 1. Agent Skills — bộ kỹ năng chính chủ cho agent code

Cài 22 skills từ 3 repo chính chủ vào **2 nơi** (mỗi agent đọc nơi riêng của nó):

- `.opencode/skills/` → OpenCode V2 (project scope, được agent tự động phát hiện)
- `.agent/skills/` → Antigravity workspace (repo này vốn đã có `.agent/rules/`)

| Nguồn | Nội dung |
|-------|----------|
| [greensock/gsap-skills](https://github.com/greensock/gsap-skills) | `gsap-core`, `gsap-timeline`, `gsap-scrolltrigger`, `gsap-plugins`, `gsap-react`, `gsap-frameworks`, `gsap-performance`, `gsap-utils` |
| [remotion-dev/skills](https://github.com/remotion-dev/skills) | `remotion-best-practices` + 11 skill con (`remotion-create`, `remotion-render`, `remotion-captions`, `remotion-multimedia`, `remotion-maps`, `remotion-studio`, ...) |
| [motiondivision/ai-kit](https://github.com/motiondivision/ai-kit) | `motion` (best-practices React/Vue, CSS spring, performance audit, transition preview) |

**Cách dùng:** không cần làm gì — khi agent (OpenCode/Antigravity) xử lý việc liên quan animation,
nó sẽ tự thấy skill trong danh sách và nạp khi cần (ví dụ: đúng chuẩn code Remotion, tối ưu GSAP).

**Cập nhật skills về sau:**

```powershell
# Ví dụ repo GSAP (làm tương tự cho remotion/skills và motiondivision/ai-kit)
$tmp = "$env:TEMP\skills_update"
curl.exe -sL -o "$tmp.zip" "https://codeload.github.com/greensock/gsap-skills/tar.gz/refs/heads/main"
# giải nén rồi copy đè vào .opencode\skills\ và .agent\skills\
```

---

## 2. Satori — Band A (banner tiêu đề) chuyên nghiệp hơn

**Kiến trúc (không thay pipeline):**

```
Python: backend/ai/satori_renderer.py
  → node tools/render_node/satori/render_title.mjs  (HTML/CSS flexbox → SVG)
  → resvg_py (đã có sẵn trong backend)              (SVG → PNG)
  → PIL.Image dán vào Band A
```

**Điểm đã xử lý:**

- Tự động **auto-fit cỡ chữ** theo độ dài tiêu đề (52→36px, tối đa ~3 dòng).
- **Tô màu từ khóa** (`keyword_accent`) — mặc định màu đen như hiện tại, đổi `accent_color` để nhấn đỏ.
- **Font fallback** cho ký tự đặc biệt: bản PIL hiện tại **không render được `★` (ra ô vuông)** — Satori render đúng nhờ fallback Segoe UI Symbol.
- Giữ khoảng trắng đúng ở ranh giới cụm tô màu (`whiteSpace: pre-wrap`).

**Đợt 2 — tích hợp sâu vào 2 đường render + UI:**

- `compose_doodle_frame(..., prefer_satori=...)` — **tham số thắng biến môi trường** (UI toggle điều khiển trực tiếp từng lần render).
- `whiteboard_hand_engine.compose_band_a_c_base(..., prefer_satori=...)` — **đường "Bàn tay vẽ thật" cũng dùng Satori Band A**.
- 3 request nhận thêm field `satori_band_a`: `AIDoodleVideoRequest`, `RenderDoodleScenesRequest`, `PreviewSceneFrameRequest`.
- **Studio UI:** checkbox "✨ Tiêu đề Satori" (mặc định **bật**) — gửi kèm cả 3 luồng (tạo video 1-click, render cảnh, preview frame).
- `backend/.env` trên máy này đã bật `SCRIPT_SATORI_BAND_A=1` (áp dụng cho đường không có UI, ví dụ Telegram bot).

**An toàn (fail-open):** mặc định = PIL như cũ; khi bật nếu thiếu node / lỗi / timeout → trả `None` → tự lùi PIL (có log warning).

**So sánh kết quả:** `workspace/doodle_cache/satori_demo/` (`frame_3band_pil.png` vs `frame_3band_satori.png`).

---

## 3. Remotion — engine doodle (đã tích hợp toàn phần vào `/kich-ban`)

**Kiến trúc:**

```
Python: backend/ai/remotion_renderer.py  (build_scene_props + render_doodle_scene)
  → node node_modules/@remotion/cli/remotion-cli.js render ...
  → Composition "DoodleScenePoC" (tools/render_node/remotion/)
  → MP4 720×1280 VIDEO-ONLY (draw-on nét vẽ + camera + Lottie)
  → FFmpeg ghép giọng đọc TTS (-c:v copy -c:a aac -shortest) → seg_xxx.mp4
  → concat + loudnorm -16 LUFS như các segment khác (giữ nguyên hậu kỳ cũ)
```

**Luồng chọn engine (trong `render_doodle_video`):**

1. Người dùng chọn "Phong cách diễn hoạt" = **🎬 Remotion Draw-On + Camera (Beta)**
   (option này tự xuất hiện khi `SCRIPT_REMOTION_ENABLED=1` **và** sidecar khả dụng — UI dò qua
   endpoint mới `GET /api/script/render-capabilities`).
2. Mỗi cảnh: `build_scene_props` tự phát hiện `action_type` / `camera_motion` / `numbers`
   (tái sử dụng `detect_scene_action_spec` của engine kinetic) → render → ghép TTS.
3. **Fallback 3 tầng từng cảnh:** Remotion lỗi → Kinetic / Bàn tay vẽ thật → Frame PIL tĩnh.
   Không bao giờ có cảnh nào làm fail cả video.

**5 biến thể art (thay cho 1 style demo cũ):**

| action_type | Hình vẽ | Hiệu ứng |
|-------------|---------|----------|
| `race` | Mèo lái xe đua + vạch tốc độ | draw-on tuần tự + camera |
| `chart_bar` | Biểu đồ cột (số liệu thật từ kịch bản) | cột mọc dần, mũi tên xu hướng đỏ vẽ sau |
| `chart_pie` | Biểu đồ tròn chia phần | từng phần nở ra, nhãn % hiện |
| `brain_think` | Bộ não + bóng đèn pulse | + 2 Lottie sparkle |
| `showcase` (mặc định) | Bóng đèn ý tưởng + tia sáng | + 2 Lottie sparkle |

- **6 camera motions**: `zoom_in` (mặc định), `zoom_out`, `pan_right`, `pan_left`, `shake`, `pulse`.
- **Lottie**: `remotion/lottie/sparkle.json` tự soạn (ngôi sao nở + xoay 72°, loop liền mạch) — nhúng bằng `@remotion/lottie` + `loop`.
- **Audio**: MP4 Remotion là video-only; TTS ghép ngoài bằng FFmpeg **để đồng bộ codec/âm thanh với concat `-c copy`** (nếu nhúng audio trong composition, sample rate có thể lệch và concat sẽ hỏng).

**Số đo thực tế trên máy (RTX 4050, concurrency 6x):**

| Hạng mục | Thời gian | Ghi chú |
|----------|-----------|---------|
| Cảnh 5s (120 frames) | ≈ 25–35s | ~300 KB (đã cache bundle) |
| Cảnh 2s (48 frames) | ≈ 5s | ~150 KB |
| **E2E pipeline 2 cảnh** (TTS giả 1.2s + render + ghép + concat + loudnorm) | **≈ 9.5s tổng** | test `REMOTION_PIPELINE_TEST=1` |

**Demo & still so sánh:** `workspace/doodle_cache/remotion_demo/poc_scene.mp4`,
`workspace/doodle_cache/remotion_demo/variants/still_{race,chart_bar,chart_pie,brain,showcase}.png`.

**Giới hạn còn lại (đợt 3 nếu cần):**

- `wallet_squeeze` / `fight` / `confrontation` chưa có art riêng → rơi về `showcase` (bóng đèn).
- Chưa benchmark RAM/VRAM đỉnh khi render nhiều cảnh song song (pipeline hiện chạy tuần tự từng cảnh).

---

## 4. Preview thông minh trong Studio (mới — đợt 2)

### 4.1 Howler.js — "🔊 Nghe liền mạch lời thoại các cảnh"

- `backend/templates/assets/howler.min.js` (v2.2.4, MIT) — **vendored local, không phụ thuộc CDN**,
  phục vụ qua route mới `GET /api/script/assets/howler.min.js` (có chặn path traversal).
- Nút mới trong card "📋 Chi Tiết Các Phân Cảnh":
  1. Tạo TTS từng cảnh theo thứ tự (hiện tiến độ `cảnh i/N`).
  2. Phát liền mạch bằng Howler: **fade-in 200ms** mỗi cảnh, cảnh kế tiếp tự phát khi cảnh trước kết thúc.
  3. Bấm lần nữa để dừng (unload sạch toàn bộ).
- Cảnh lỗi TTS → tự bỏ qua cảnh đó, không chặn chuỗi phát.

### 4.2 Lottie

- Sparkle tự soạn dùng trong video Remotion (mục 3). Thêm asset khác: xem `tools/render_node/README.md`.

### 4.3 API phụ trợ mới

- `GET /api/script/render-capabilities` → `{remotion: {enabled, available, label}}` — UI tự dò để
  hiện/ẩn option engine.
- `GET /api/script/assets/<tên>` — phục vụ tài nguyên tĩnh của Studio (Howler...).

---

## 5. FFmpeg — tối ưu đã áp dụng & lý do phần còn lại

| Hạng mục | Quyết định | Lý do |
|----------|-----------|-------|
| `-movflags +faststart` | ✅ Đã áp dụng cho file final | Tải/stream nhanh hơn, chi phí ~0 |
| `.ass` subtitles nâng cao | ⛔ Không áp dụng | Phụ đề của pipeline này **đã bake trong Band C** (PIL/Remotion render), không phải burn sau |
| `xfade` chuyển cảnh | ⛔ Không áp dụng | Concat hiện dùng `-c copy` (nhanh, bền); xfade đòi re-encode toàn bộ + canh timing audio — rủi ro không tương xứng lợi ích |
| `zoompan` | ⛔ Không áp dụng | Remotion đã có camera; đường PIL giữ nguyên để ổn định |
| SVGO | ⛔ Không áp dụng | SVG nội bộ nhỏ, giá trị thấp |

---

## 6. Vận hành & bảo trì

**`backend/.env` — 2 cờ mới (máy này đã bật cả 2):**

```ini
SCRIPT_SATORI_BAND_A=1     # Band A bằng Satori cho mọi đường render (fail-open về PIL)
SCRIPT_REMOTION_ENABLED=1  # Hiện tuỳ chọn engine Remotion trong Studio
# Nhớ khởi động lại backend sau khi sửa .env
```

**Máy mới clone repo:**

```powershell
cd tools\render_node
npm install        # Satori + Remotion + Lottie (node_modules bị gitignore, không commit)
# Lần render Remotion đầu tiên sẽ tự tải Chrome Headless Shell (~150MB)
```

**Chạy test:**

```powershell
# Bộ nhẹ (nhanh ~1s):
.\backend\venv\Scripts\python.exe -m pytest tests\test_satori_band_a.py tests\test_remotion_renderer.py -q

# Test render Remotion thật (~20-30s):
$env:REMOTION_RENDER_TEST = "1"
.\backend\venv\Scripts\python.exe -m pytest tests\test_remotion_renderer.py -q

# E2E pipeline render_doodle_video(whiteboard_mode="remotion") với TTS giả (không gọi mạng):
$env:REMOTION_PIPELINE_TEST = "1"
.\backend\venv\Scripts\python.exe -m pytest tests\test_remotion_renderer.py -q
```

**Lệnh thủ công hữu ích:**

```powershell
cd tools\render_node
node satori/render_title.mjs --in satori/example_props.json > band_a.svg   # Satori → SVG
npx remotion render remotion/index.tsx DoodleScenePoC out.mp4 --props=remotion/props.json  # Remotion → MP4
node node_modules/@remotion/cli/remotion-cli.js still remotion/index.tsx DoodleScenePoC out\bar.png --props=remotion/props_chart_bar.json --frame=66
npx remotion studio remotion/index.tsx                                     # xem/sửa trực quan
```

**Ghi chú kỹ thuật (bug đã gặp & cách xử lý):**

- Concat `-c copy` yêu cầu cùng codec/pixel-format/sample-rate → Remotion xuất video-only, TTS ghép bằng FFmpeg như các segment khác.
- Satori/Remotion đều gặp bug **mất khoảng trắng tại ranh giới cụm tô màu** → fix bằng `whiteSpace: pre-wrap`.
- Satori bị tràn content-box khi padding lớn → fix bằng `boxSizing: border-box`.
- `.gitignore` gốc có rule `test_*` từng chặn file trong `tests/` → đã thêm `!tests/**`.
- Route tài nguyên mới kiểm tra `..`, `/`, `\` để chống path traversal.

---

## 7. Việc còn theo dõi (CHƯA triển khai)

| Hạng mục | Lý do chưa làm | Điều kiện để làm |
|----------|----------------|------------------|
| Art riêng cho `wallet_squeeze` / `fight` / `confrontation` | 3 art mới tốn công vẽ path; hiện rơi về showcase | Khi các cảnh này xuất hiện nhiều trong kịch bản thật |
| Benchmark RAM/VRAM khi render song song nhiều cảnh | Pipeline hiện tuần tự; chưa phải bottleneck | Khi cần tăng tốc video dài (>15 cảnh) |
| dotlottie-web preview trong dashboard | Chỉ cần khi có bộ asset doodle thật | Khi sưu tầm/đặt vẽ bộ `.lottie` ưng ý |
| SVGO | Giá trị thấp với pipeline hiện tại | Khi tối ưu khối lượng SVG lớn |
| xfade / .ass cho pipeline này | Không phù hợp cấu trúc (bake-in + concat copy) | Chỉ xem lại nếu đổi kiến trúc hậu kỳ |

> Nguồn gốc quyết định: `docs/DANH_GIA_20_REPO_ANIMATION_VIDEO_2026-10-18.md`

---

## 8. Đợt 3 — Bot Telegram riêng cho Studio Kịch Bản (2026-10-18)

Bot Telegram **tách riêng** khỏi bot AutoDub (`telegram_bot.py` / `BOT_TOKEN`), chuyên cho luồng:
**gửi prompt → AI viết kịch bản phân cảnh → duyệt → dựng video 9:16 → nhận video ngay trong chat.**

### 8.1 Thành phần

| Thành phần | File / đối tượng | Ghi chú |
|-----------|------------------|---------|
| Bot chính | `backend/telegram_script_bot.py` | python-telegram-bot 22.8 (đã có trong venv), long-polling |
| Supervisor | `backend/background_service.py` `--service scriptbot` | Respawn tự động khi bot thoát, log xoay vòng |
| Task Scheduler | `AutoDubV1-scriptbot` (logon trigger, user `admin`) | Cùng chuẩn với `AutoDubV1-dashboard` / `AutoDubV1-telegram` |
| Test nhẹ | `tests/test_telegram_script_bot.py` | 6 test, không cần mạng/token |
| Log | `workspace/bot_system/service_logs/scriptbot.log` (bot) + `scriptbot_supervisor.log` (supervisor) | — |

### 8.2 Biến môi trường (`backend/.env`)

| Key | Ý nghĩa | Mặc định |
|-----|---------|----------|
| `TELEGRAM_SCRIPT_BOT_TOKEN` | Token bot (BotFather). **Khác** `BOT_TOKEN` của AutoDub — code có guard chống trùng token (thoát nếu trùng) | (bắt buộc) |
| `TELEGRAM_SCRIPT_ALLOWED_CHAT_IDS` | Danh sách Chat ID được phép (phân cách `,`/`;`/khoảng trắng). Để trống = mở cho mọi người (giống bot AutoDub). Lấy ID bằng lệnh `/chatid` | trống |
| `TELEGRAM_SCRIPT_API_BASE` | Địa chỉ dashboard mà bot gọi | `http://127.0.0.1:8088` |
| `TELEGRAM_SCRIPT_AI_ENGINE` | Engine AI viết kịch bản | `muse` |

### 8.3 Luồng hoạt động (bám đúng API của Studio)

1. Người dùng gửi prompt → bot gọi `POST /api/script/ai-generate-script` (timeout 900s).
2. Bot gửi **bản xem trước kịch bản** (tiêu đề, từ khóa nhấn, từng cảnh + nhãn doodle) kèm nút:
   - **🎬 Dựng video ngay** → `POST /api/script/render-doodle-scenes` với đúng tham số UI gửi
     (`whiteboard_mode`/`use_hand_draw` theo phong cách đang chọn).
   - **🔁 Tạo kịch bản khác** · **⚙️ Cài đặt**.
3. Bot theo dõi `GET /api/script/pipeline/progress/{task_id}` mỗi 4 giây, sửa tin nhắn tiến độ
   theo % + bước, gửi timeout 90 phút.
4. Xong → `send_video` (fallback `send_document` → ghi đường dẫn file). Render lỗi → báo lý do trong chat.

**Chống xung đột:** mọi render đi qua HTTP API của dashboard nên dùng chung `SCRIPT_PROCESS_LOCK` —
web và bot **không bao giờ render song song**; nếu hệ thống bận, bot nhận HTTP 423 và nhắc chờ.

### 8.4 Lệnh trong chat

| Lệnh | Tác dụng |
|------|----------|
| (gửi văn bản thường) | Tạo kịch bản từ prompt |
| `/start`, `/help` | Hướng dẫn + Chat ID |
| `/settings` | Bảng cài đặt: phong cách (kinetic/remotion — đã bỏ bàn tay vẽ từ 18/10), số cảnh (4–16), giọng đọc (8 giọng CapCut/Edge) |
| `/engine`, `/voice`, `/scenes` | Mở nhanh bảng cài đặt (nhận cả tham số, vd `/scenes 8`) |
| `/stop` | Gửi lệnh dừng render hiện tại (`POST /api/script/stop`) |
| `/chatid` | In Chat ID để điền allowlist |

Cài đặt lưu **theo từng chat, trong RAM** (reset khi bot/service khởi động lại). Mặc định (từ chiều 18/10, đợt 5):
⚡ Kinetic Reveal (không tay vẽ) · **12 cảnh** · giọng *Thanh Niên Tự Tin (BV075)*.

### 8.5 Vận hành

```powershell
# Trạng thái + bật/tắt
Get-ScheduledTask -TaskName AutoDubV1-scriptbot | Select TaskName, State
Start-ScheduledTask -TaskName AutoDubV1-scriptbot
Stop-ScheduledTask  -TaskName AutoDubV1-scriptbot     # supervisor dừng; process con tự thoát theo

# Restart nhanh bot (supervisor tự respawn sau ~5–20s, backoff tăng dần nếu chết sớm):
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match "venv.*telegram_script_bot\.py" } |
  ForEach-Object { taskkill /PID $_.ProcessId /T /F }

# Xem log
Get-Content "workspace\bot_system\service_logs\scriptbot.log" -Tail 30
```

### 8.6 An toàn & giới hạn đã biết

- Token nằm trong `backend/.env` (đã gitignore). **Không commit**; ai có token = toàn quyền bot.
- Log `httpx` đã hạ xuống `WARNING` để không ghi token vào file log mỗi vòng `getUpdates`.
- `drop_pending_updates=True`: tin nhắn gửi đúng lúc bot restart có thể bị bỏ qua (tránh dội lệnh cũ).
- Chưa làm (đợt sau nếu cần): lưu cài đặt per-chat xuống đĩa; hỗ trợ nhóm chat; rate-limit chống spam;
  upload video > 50MB qua Bot API (hiện rơi về gửi dạng file rồi tới ghi đường dẫn).

---

## 9. Copy gương video thành phẩm sang ổ D (2026-10-18)

- Biến môi trường `SCRIPT_OUTPUT_COPY_DIR` trong `backend/.env` — rỗng = tắt (hành vi cũ).
  Máy này: `SCRIPT_OUTPUT_COPY_DIR=D:\kich_ban`.
- `render_doodle_video()` sau khi tạo xong file final sẽ **copy thêm 1 bản** sang thư mục đó.
  Áp dụng cho **mọi đường render**: web 1-click, `render-doodle-scenes`, bot Telegram.
- Kết quả trả về thêm khóa `copied_to` (đường dẫn bản copy, `null` khi tắt hoặc lỗi).
- **An toàn mặc định:** lỗi copy (ổ D ngắt kết nối, hết chỗ, quyền...) chỉ ghi log cảnh báo —
  video chính trong `workspace/output` luôn nguyên vẹn, pipeline không bao giờ gãy.
- Bot Telegram hiển thị thêm dòng “💾 Đã lưu ổ D: …” khi gửi video về chat.
- Nghiệm thu thật (2026-10-18 16:25): render 2 cảnh qua API → file C + file `D:\kich_ban\...` trùng byte 100%.
- Test: `tests/test_doodle_mirror_output.py` (4 test nhẹ) + nhánh E2E
  (`REMOTION_PIPELINE_TEST=1`) kiểm tra bản copy trùng byte với file gốc.

---

## 10. Đổi mặc định Studio: bỏ tay vẽ · nhiều cảnh hơn · giọng Thanh Niên Tự Tin (2026-10-18, đợt 5)

Yêu cầu người dùng: “khả năng làm video rất tệ → **bỏ bàn tay vẽ**, muốn **animation nhiều cảnh hơn**,
dùng giọng **Thanh Niên Tự Tin** (CapCut TTS)”.

### 10.1 Thay đổi mặc định

| Hạng mục | Trước | Sau |
|---|---|---|
| Phong cách (web `/kich-ban`) | ✍️ Bàn Tay Vẽ Thật (`3band`) | ⚡ **Kinetic Reveal** — dropdown bỏ hẳn lựa chọn tay vẽ; Remotion vẫn thêm động khi engine bật |
| Bot Telegram (`/settings`, `/engine`) | ✍️ Bàn Tay Vẽ Thật · 6 cảnh | ⚡ Kinetic Reveal · **12 cảnh** (hàng nút chỉ còn kinetic / remotion) |
| Giọng đọc | Nam Trầm `multi_male_felipe_uranus_bigtts` | **`BV075_streaming` — Thanh Niên Tự Tin (Nam Trẻ)** — web + bot + API model + engine |
| `use_hand_draw` (API + engine) | `True` | **`False`** — không bao giờ vẽ tay, trừ khi caller tự ép `True` |
| Alias cũ trong bot | `3band` / “bàn tay” → tay vẽ | tự chuyển sang **kinetic** (không tay) |
| Độ dài mặc định | 6 cảnh (~25s) | **12 cảnh (~50s)** — bot `/scenes` 2–30, nút nhanh 4–16 |

- UI: gỡ checkbox “bàn tay vẽ”; JS chỉ đọc `whiteboard-mode-select` (mặc định `kinetic`, fallback `kinetic`).
- Fail-open giữ nguyên: Remotion lỗi → kinetic → PIL; Satori lỗi → PIL.

### 10.2 File đụng vào

`backend/templates/script_studio.html` · `backend/telegram_script_bot.py` · `backend/script_api.py`
(`AIDoodleVideoRequest`, `AIDoodleScriptRequest`, `RenderDoodleScenesRequest`) · `backend/ai/doodle_engine.py`
· `tests/test_telegram_script_bot.py` (test mới `test_mac_dinh_moi_bo_tay_ve_nhieu_canh_giong_bv075`) · mục này.

### 10.3 Video mẫu so sánh Kinetic vs Remotion (đã render thật)

- Kịch bản mẫu 5 cảnh (bộ não → phần thưởng → 50-30-20 → thống kê → cuộc đua), giọng BV075,
  `use_hand_draw=False`, `satori_band_a=True`; gọi lần lượt qua `/api/script/render-doodle-scenes`.
- Kết quả (2026-10-18):
  - ⚡ Kinetic: `workspace/output/doodle_1792317053_81eb0e_final.mp4` (18.8s, render 54s) → `D:\kich_ban\MAU-1-Kinetic-Reveal.mp4`
  - 🎬 Remotion: `workspace/output/doodle_1792317107_ceff2b_final.mp4` (18.8s, render 78s) → `D:\kich_ban\MAU-2-Remotion-Beta.mp4`
- Kiểm tra frame: **không có bàn tay trong cả 2**; kinetic = nét vẽ tự chạy kiểu sketch đen trắng;
  remotion = art tô kín + label bo tròn + sparkle Lottie.
- Người dùng xem 2 file này để chốt phong cách mặc định chính thức (nếu chọn Remotion chỉ cần đổi 1 dòng
  `selected` trong HTML + `ENGINES`/settings bot — không đụng engine).

### 10.4 Trạng thái nạp code

- Bot Telegram: đã restart **16:36:51** → chạy code mới (mặc định kinetic · 12 cảnh · BV075).
- Dashboard (web): đã restart **17:03** sau khi hàng đợi AutoDub dub xong hết (6/6 — không cắt ngang job nào).
  Nghiệm thu trang `/kich-ban` đang chạy: `kinetic` selected ✓, `BV075_streaming` selected ✓, 12 cảnh ✓,
  không còn `hand-draw-toggle`/`3band` ✓, `useHandDraw=false` ✓; Remotion vẫn `enabled+available`
  (dropdown tự thêm lựa chọn khi mở trang).
- Test: **21 passed / 2 skipped** (bộ nhẹ; +1 test mặc định mới), `py_compile` 3 file Python OK.

---

## 11. Studio ưu tiên Gemini bản mới khi viết kịch bản (2026-10-18, đợt 5)

**Bối cảnh:** Google đã khai tử `gemini-2.5-flash` (probe thật trả 404: *“no longer available to new users,
please update to models/gemini-3.8-flash”*), trong khi nhãn UI cũ vẫn ghi “Gemini 2.5 Flash”.

**Đo tốc độ thực tế với key trên máy (câu ngắn, 1 lượt):**

| Model | Kết quả | Ghi chú |
|---|---|---|
| `gemini-3.8-flash` | OK nhưng **77s** | Bản cao nhất nhưng quá chậm so với timeout 60s/lượt của `call_gemini` |
| `gemini-3.7-flash` | OK nhưng 41s | Chậm |
| **`gemini-3.6-flash`** | **OK 4.2s** | ✅ Chọn làm ưu tiên cho Studio |
| `gemini-3.5-flash` / `-lite` | OK ~1s | Vẫn nằm trong chuỗi fallback |
| `gemini-3-flash-preview` | OK 11s | Model cũ Studio đang dùng — giữ làm dự phòng |
| `gemini-2.5-flash` | ❌ 404 | Google khai tử |

**Thay đổi:**
- `call_gemini(..., preferred_model=...)`: model ưu tiên được thử **trước tiên**; danh sách fallback cũ giữ nguyên.
- `prompt_to_script.generate_doodle_script_from_ai` truyền `preferred_model=GEMINI_SCRIPT_MODEL`.
- `.env`: thêm `GEMINI_SCRIPT_MODEL=gemini-3.6-flash` — **chỉ áp dụng đường kịch bản Studio**;
  `GEMINI_MODEL=gemini-3-flash-preview` giữ nguyên cho AutoDub/AutoPipeline (không đụng dubbing).
- `/api/script/ai-engines` trả model + tên động theo env; UI `/kich-ban` tự cập nhật nhãn khi mở trang
  (kèm ghi chú “Muse — chưa đăng nhập (tự dùng Gemini)” khi chưa có `~/.config/muse/auth.json`).
- Nếu 3.6 lỗi/quá tải: tự lùi về `gemini-3-flash-preview` → 3.5 → 3.6/3.7/3.8 (danh sách cũ) — không bao giờ kẹt.
- Muốn đổi bản khác: sửa `GEMINI_SCRIPT_MODEL` trong `backend/.env` rồi restart dashboard là xong.
- **Nghiệm thu live (hàng đợi AutoDub 14/14 xong mới restart):** `/api/script/ai-engines` → `gemini-3.6-flash`;
  test thật endpoint `/api/script/ai-generate-script` (gemini) → HTTP 200, kịch bản 4 cảnh tiếng Việt;
  probe qua chính `call_gemini` → log `Đang gọi Gemini (gemini-3.6-flash) [lần 1/2]` và trả lời trong **3.5s**;
  `/kich-ban` không còn chuỗi “2.5 Flash”, có `refreshAiEngineLabels` tự cập nhật nhãn + tự chọn Gemini khi Muse chưa đăng nhập.

---

## 12. Kiểm tra Veo 3.1 & quyết định giữ engine nội bộ (2026-10-18, đợt 5)

- Probe thật với key trên máy (không tốn phí): `/v1beta/models` liệt kê 3 model Veo —
  `veo-3.1-generate-preview`, `veo-3.1-fast-generate-preview`, `veo-3.1-lite-generate-preview`;
  `predictLongRunning` với body rỗng trả HTTP 400 “No instances” → **key có quyền dùng Veo 3.1**
  (các ID dạng `*-001` cũ trả 404 — qua Gemini API chỉ có bản preview).
- Giá tham khảo (Gemini API, có audio): Lite 720p $0.05/s · Fast 720p $0.10/s · Standard $0.40/s
  → clip 8s lần lượt ~$0.40 / ~$0.80 / ~$3.20; video 12 cảnh (~96s) ≈ **$4.8 / $9.6 / $38.4**.
- Model video mã nguồn mở (LTX-Video, Wan 2.2, HunyuanVideo 1.5) cần ≥8–14GB VRAM;
  máy hiện tại (RTX 4050 Laptop **6GB**) không đủ chạy ở mức dùng được.
- **Quyết định của chủ máy (18/10): GIỮ NGUYÊN engine nội bộ (kinetic/Remotion)** — miễn phí vô hạn,
  hợp phong cách kể chuyện; chưa tích hợp Veo/AI video. Muốn cảnh thực tế sau này thì đường rẻ nhất
  đã sẵn sàng (Veo 3.1 Lite, key đã có quyền).

---

## 13. Cảnh "mèo đánh nhau" thật sự — rig 2 chiến binh mèo (2026-10-18, đợt 5)

- Vấn đề: cảnh `fight` của engine Kinetic trước đây chỉ **ghép 2 icon + tia chớp** (video mèo bản đầu
  hiện icon đầu mèo + kiếm giao nhau) → chủ máy yêu cầu animation *2 con mèo đánh nhau thật*.
- Giải pháp: thêm rig `_draw_cat_fighter_rig()` trong `backend/ai/kinetic_scene_animator.py`
  (vẽ bằng PIL, cùng phong cách nét mực đen–trắng, **không bàn tay vẽ, không gọi AI**):
  - 2 biến thể: `tabby` (mèo vàng — thân trắng viền đen, vằn lưng/đuôi/trán) và `black`
    (mèo mun — thân đen, **viền trắng tách khối**, mắt/mũi/miệng trắng, ria đen).
  - Chân tát được vẽ **sau cùng** (không bị đầu che), cung vung từ sau lưng 165° → tát tới trước −10°,
    móng vuốt bung ra khi vung hết tầm; thân nghiêng theo cú lao, đuôi vẫy, nhún lao vào.
  - Điều kiện kích hoạt: `action_type == "fight"` **và** cảnh có mèo (icon `cat` hoặc chữ "mèo"/"cat"
    trong narration/visual_concept/doodle_label/visual_type/image_prompt). Cảnh confrontation thường
    (2 đối tượng tương tác) vẫn dùng icon như cũ — không bị ảnh hưởng.
- Kết quả (đã render thật, 68s): `D:\kich_ban\Meo-Danh-Nhau.mp4` — 34,36s · 3,18MB; cảnh 1 & 3 giờ là
  2 mèo đánh nhau thật (thay phiên vung chân, tóe lửa giữa, camera rung); các cảnh khác giữ nguyên.
- Phát hiện phụ: key **còn có 8 model tạo ảnh**: `gemini-2.5-flash-image` (Nano Banana),
  `gemini-3-pro-image` + `-preview` + `nano-banana-pro-preview` (Nano Banana Pro),
  `gemini-3.1-flash-image` + `-preview` (Nano Banana 2), `gemini-3.1-flash-lite-image` (Nano Banana 2 Lite),
  `gemini-nano-banana-2.1`. Nếu sau này muốn minh họa kiểu **ảnh sketch tĩnh** chất lượng cao thì gọi
  `generateContent` — lưu ý ảnh tĩnh (không chuyển động) và có phí theo ảnh.
- Vận hành: sửa engine → **phải restart dashboard** (module import 1 lần lúc khởi động):
  tìm launcher `venv\Scripts\python.exe ... main.py` rồi `taskkill /PID <launcher> /T /F`,
  supervisor tự respawn sau ~5–35s (AutoDub phải đang rảnh — kiểm tra `/api/status` trước).

---

## 14. Vũ đạo trận đấu v2 — "animation nhiều hơn, thật hơn" (2026-10-18, đợt 6)

- Yêu cầu: render lại "mèo đánh nhau" nhưng animation nhiều hơn và thật hơn.
- Nâng rig `_draw_cat_fighter_rig()`: thêm 3 tham số diễn xuất — `claw_power` (móng bung theo đạo diễn),
  `ear_flick` (tai dẹp sát đầu), `shout` (miệng há gầm gừ to dần). Thêm hàm `_compose_cat_duel()` đạo diễn
  toàn bộ vũ đài (thay nhánh cũ trong `_compose_confrontation_or_fight_scene`):
  - 0.00–0.12 thủ thế gườm nhau (thở nhẹ, đuôi ngoáy, tai dẹp dần);
  - 0.12–0.72 **3 hiệp tát xen kẽ**: lấy đà → nhảy bổ (squash/stretch giữ chân chạm đất) → vung chân
    nhanh dần → trúng đòn (đối thủ bật lùi + run + **sao va chạm ở đỉnh đầu**) hoặc né (vệt gió);
  - 0.72–0.90 **liên hoàn tốc độ cao** (cả hai lao vào tát liên tục); 0.90–1.00 áp sát "mũi đối mũi".
  - Hiệu ứng mới: `_draw_impact_star` (sao 16 cánh + tia), `_draw_whoosh_arcs` (3 cung vệt gió),
    `_draw_dust_puff` (bụi tiếp đất), vệt tốc độ sau lưng khi lao; tâm chấn giữa sàn chỉ bùng đúng nhịp chạm.
  - Kịch bản có chữ "trượt/hụt" → phần lớn đòn là tát trượt (khớp lời thoại cảnh 3 "TÁT TRƯỢT LIÊN HỒN").
- Nghiệm thu (render thật 42s, dashboard đã respawn sau 12s): `D:\kich_ban\Meo-Danh-Nhau.mp4` (ghi đè bản cũ,
  giữ bản v1 ở workspace để so sánh) — 34,36s · 3,2MB. Bằng chứng khung: 2,4s thủ thế; 3,0s mèo vàng lao có
  vệt tốc độ + mèo mun trúng đòn (sao nổ trên đầu); 4,2s liên hoàn cận chiến; 12,4s cảnh 3 tát trượt có vệt gió.
- Lưu ý: khung mèo mun trông "thiếu thân" thoáng qua ở ~0,9–2,1s đầu cảnh là hiệu ứng **vẽ-ra** đang hoàn thiện
  (mọi bản render đều có) — không phải lỗi.
- Vẫn nhắc lại: sau MỌI lần sửa engine phải restart dashboard (kill launcher, supervisor respawn) mới có hiệu lực.

---

## 15. Engine v3 — hết trùng lặp footage, hình "chân thực" + chuyển động thật hơn (2026-10-19, đợt 7)

- Phản hồi user (nguyên văn): *"Các footage bị trung lặp nhau, mình muốn footage đẹp hơn, chân thực
  và chuyển động thật hơn"* (sau khi xem v2 `Meo-Danh-Nhau.mp4`).
- **Chẩn đoán trùng lặp** (đối chiếu 17 khung hình trích từ v2 với `visual_concept` từng cảnh trong
  kịch bản): không phải do kịch bản xấu mà do **bộ nhận diện cảnh (detect) đọc hết vào nhánh
  fight/confrontation mặc định**, bỏ qua ngữ nghĩa riêng của từng cảnh:
  - s1 (đại chiến) và s3 (tát trượt) render gần như y hệt nhau; s2 (biểu đồ 90%) ra 33/34/33 vì
    chuỗi "90%" lặp 3 lần (narration + nhãn + prompt) khiến bộ tách số đếm trùng; s4 (đĩa thức ăn)
    ra icon tĩnh; s5 (kính hiển vi) ra 2 đối tượng so sánh; s6 (2 não đấu trí) ra 1 não tĩnh;
    s7 (rút lui + tắm nắng) lọt sang **cảnh đua xe** vì từ khóa "xe" khớp bừa trong "người **xe**m";
    s8 (mèo bình luận) trùng icon tim.
- **Cách chữa** (giữ nguyên kịch bản vì concept từng cảnh đã tốt — sửa engine):
  1. **Rig mèo v3 pose-driven** (`_draw_cat_pose` + `_render_cat_sprite`): supersample ×3 khử răng
     cưa; đầu tách khối (nghiêng, mắt nháy, đồng tử liếc, miệng gầm, râu); **2 tay IK 2 khúc** (khuỷu
     gập thật theo đích đến, có móng vuốt bung); 2 chân sau có chu kỳ bước; đuôi chuỗi 6 đốt lượn
     sóng; lông xù; thêm tư thế **mèo nằm** (`_draw_cat_lying`). Sprite mirror theo hướng mặt TRƯỚC
     khi xoay và xoay quanh mốc chân (y=208) nên **chân không trượt khỏi đất**; có squash/stretch.
  2. **8 đạo diễn riêng cho 8 cảnh** (dispatch theo detect spec, đúng concept kịch bản):
     s1 `battle` (2 hiệp tát + liên hoàn + mũi đối mũi, camera rung); s2 `chart_pie` + 2 mèo ở góc
     giả vờ tát (vệt gió ngay đầu bàn chân, callout "GIẢ VỜ / ĐÁNH THẬT"); s3 `sloppy` (3 cú tát
     trượt + né vụng về + giọt mồ hôi); s4 `cat_food` (đĩa đầy thức ăn bị gạt sang phải + mũi tên
     nét đứt chỉ về góc nắng); s5 `microscope` (kính lúp cận cảnh mèo gầm + sóng âm); s6 `brain_duel`
     (2 não tai mèo + tia chớp + tia lửa); s7 `retreat` (mèo mun nằm dài tắm nắng + "Z" + đồng hồ
     "30 PHÚT" + mèo vàng đi khuất); s8 `cat_comment` (khung bình luận tim + mèo ngồi dưới + tim bay).
     Đạo diễn `_cat_duel_choreo` trả về (khung hình, hiệu ứng, sự kiện, tỉ lệ, mốc X) để composer dùng chung.
  3. **Sửa detect**: gộp trùng số trước khi tách (hết 33/34/33); so khớp từ khóa theo **ranh giới từ**
     (hết bug "xe" trong "người xem" → không còn cảnh đua xe); thêm nhánh microscope (chặn TRƯỚC brain
     vì "tâm lý" chứa "lý"... ), cat_food → cat_retreat → cat_comment (đều chặn TRƯỚC fight);
     `brain_variant="duel"` khi có "hai bộ não / tia chớp / đấu trí"; `cat_mode`: sloppy > battle > threat.
  4. Nghiệm thu nội bộ trước khi render video: lưới **9 pha × 8 cảnh** + 6 khung full-res soi rig
     (khớp từng concept, không lặp bố cục giữa các cảnh).
- Nghiệm thu chính thức: render thật qua API (dashboard respawn sau ~3s, AutoDub đã rảnh để không đụng
  pipeline khác): `D:\kich_ban\Meo-Danh-Nhau.mp4` — **34,36s · 3,73MB** (ghi đè bản v2 cùng tên; bản v2
  vẫn giữ ở `workspace\output` để so sánh). Lưới 16 khung trích từ video + 3 khung full-res xác nhận
  8 cảnh khác nhau hoàn toàn và đúng concept: s1 đại chiến, s2 biểu đồ 90% + 2 mèo giả vờ tát (callout
  "GIẢ VỜ / ĐÁNH THẬT" — đổi để không lặp chữ với nhãn đầu cảnh), s3 cận chiến tát trượt, s4 đĩa đầy bị
  gạt đi + mũi tên chỉ về góc nắng, s5 kính lúp mèo gầm, s6 hai não + tia chớp, s7 mèo mun nằm tắm nắng
  + đồng hồ "30 PHÚT", s8 khung bình luận tim; tổng render 96s.
- Ghi chú kỹ thuật: phần tử xuất hiện giữa cảnh (vd mèo mun nằm xuống ở s7) được pipeline **cross-fade
  mềm ~1s** khi phát video — đây là cơ chế có sẵn của pipeline (làm mượt thay đổi giữa các khung), không
  phải lỗi engine; khung render gốc đã kiểm sạch từng pixel.

