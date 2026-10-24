# Node Sidecar (`tools/render_node`) — Studio Kịch Bản

Bộ công cụ Node phụ trợ cho `/kich-ban` (Tool V1). Python pipeline **không phụ thuộc bắt buộc** vào thư mục này:
mọi lỗi đều tự động lùi về engine PIL hiện tại (fail-open).

## Yêu cầu

- Node.js >= 18 (đã kiểm tra trên Node v26)
- Chạy `npm install` một lần trong thư mục này (khi clone sang máy mới):

```powershell
cd tools\render_node
npm install
```

## 1. Satori — Band A (banner tiêu đề)

- Script: `satori/render_title.mjs` (JSON qua stdin hoặc `--in <file>` → SVG qua stdout)
- Wrapper Python: `backend/ai/satori_renderer.py`
- Bật trong `backend/.env`: `SCRIPT_SATORI_BAND_A=1` (mặc định `0` = dùng PIL như cũ)
- Studio UI: checkbox "✨ Tiêu đề Satori" (mặc định bật — tự lùi PIL nếu thiếu Node)

```powershell
# Thử tay:
node satori/render_title.mjs --in satori/example_props.json > band_a.svg
```

## 2. Remotion — engine dựng phân cảnh (đã tích hợp `/kich-ban`)

- Composition: `remotion/index.tsx` → `DoodleScenePoC` (720×1280, fps 15/24/30 theo UI)
- Props mẫu: `remotion/props.json`; biến thể: `remotion/props_chart_bar.json`, `props_chart_pie.json`, `props_brain.json`, `props_showcase.json`
- Wrapper Python: `backend/ai/remotion_renderer.py`
- Bật engine: `SCRIPT_REMOTION_ENABLED=1` trong `backend/.env` → tuỳ chọn
  "🎬 Remotion Draw-On + Camera (Beta)" xuất hiện trong "Phong cách diễn hoạt" của Studio
  (chọn mới chạy; lỗi tự lùi engine cũ — xem chuỗi fallback bên dưới)

### Art theo `action_type` (backend tự phát hiện từ ngữ nghĩa cảnh)

| action_type | Hình vẽ | Ghi chú |
|-------------|---------|---------|
| `race` | Mèo lái xe đua | kèm vạch tốc độ |
| `chart_bar` | Biểu đồ cột tăng trưởng + mũi tên xu hướng đỏ | dùng `numbers` (%) |
| `chart_pie` | Biểu đồ tròn chia phần xuất hiện tuần tự | dùng `numbers` (%) |
| `brain_think` | Bộ não + bóng đèn pulse | kèm Lottie sparkle |
| `showcase` (mặc định) | Bóng đèn ý tưởng + tia sáng | kèm Lottie sparkle |

- Camera: `zoom_in` (mặc định), `zoom_out`, `pan_right`, `pan_left`, `shake`, `pulse` — lấy từ `camera_motion` của cảnh.
- Lottie: `remotion/lottie/sparkle.json` (tự soạn, looping). **Thêm asset mới:** bỏ file JSON vào
  `remotion/lottie/` rồi import trong `DoodleScenePoC.tsx` (dùng component `<Lottie animationData={...} loop />`).
- **Audio:** MP4 từ Remotion là **video-only**; giọng đọc TTS được **ghép bằng FFmpeg** trong
  `render_doodle_video` (`-c:v copy -c:a aac -shortest`) — mục đích: giữ codec/âm thanh đồng bộ
  với các segment PIL khác để concat `-c copy` không lỗi.

### Chuỗi fallback khi render 1 cảnh (không bao giờ làm hỏng video)

```
Remotion (draw-on + camera)  →  Kinetic / Bàn tay vẽ thật (whiteboard engine)  →  Frame PIL tĩnh + ffmpeg loop
```

```powershell
# Render thử:
npx remotion render remotion/index.tsx DoodleScenePoC out\poc_scene.mp4 --props=remotion/props.json

# Still 1 frame (55% thời lượng) cho từng biến thể art:
node node_modules/@remotion/cli/remotion-cli.js still remotion/index.tsx DoodleScenePoC out\bar.png --props=remotion/props_chart_bar.json --frame=66

# Xem trước trong Remotion Studio:
npx remotion studio remotion/index.tsx
```

Lần render đầu tiên Remotion sẽ tải Chrome Headless Shell (~150 MB), các lần sau dùng cache.

## Test

```powershell
cd C:\tool v1
.\backend\venv\Scripts\python.exe -m pytest tests\test_satori_band_a.py tests\test_remotion_renderer.py -q

# Test render Remotion thật (tốn ~20-30s):
$env:REMOTION_RENDER_TEST = "1"
.\backend\venv\Scripts\python.exe -m pytest tests\test_remotion_renderer.py -q

# E2E pipeline: render_doodle_video(whiteboard_mode="remotion") với TTS giả (TTS gọi mạng bị chặn):
$env:REMOTION_PIPELINE_TEST = "1"
.\backend\venv\Scripts\python.exe -m pytest tests\test_remotion_renderer.py -q
```

## Bảo trì

- Nâng cấp Satori/Remotion: sửa version trong `package.json` rồi `npm install`.
- `node_modules/` đã nằm trong `.gitignore` toàn repo — không commit.
- Nguồn cảm hứng & đánh giá chi tiết: `docs/DANH_GIA_20_REPO_ANIMATION_VIDEO_2026-10-18.md`.
- Nhật ký triển khai (2 đợt): `docs/TRIEN_KHAI_AGENT_SKILLS_SATORI_REMOTION_2026-10-18.md`.
