# BÀN GIAO TOOL QUY TRÌNH (TOOL V1) + PROMPT + KẾ HOẠCH NÂNG CẤP CAPCUT

Cập nhật: 2026-10-24 · Repo: `C:\tool v1` · Branch: `tool-v1` (ahead origin 10+ commit)

---
## PHẦN A. BÀN GIAO

### A1. Tổng quan
Tool V1 = dây chuyền Việt hóa video Trung Quốc (Douyin...) + chống re-up, chạy local, giao diện web.
- Backend: Python FastAPI, `backend/main.py` (port **8088**), supervisor `backend/background_service.py` (tự restart khi process chết), control plane `tool_control.py` (port 8090), Telegram bot `telegram_bot.py`.
- Frontend editor: `backend/templates/workflow.html` (~8.800 dòng, ~190 hàm JS) tại `http://127.0.0.1:8088/quy-trinh`.
- API editor: `backend/workflow_api.py` (router `/api/workflow/*`).
- GPU: RTX 4050 6GB (NVENC/NVDEC). Python 3.10, venv `backend/venv`.

### A2. Pipeline (8 bước)
Tải video → tách vocal/BGM (BS-RoFormer, Demucs) → ASR (Whisper + SenseVoice hybrid) → OCR sub Trung (PP-OCR) → dịch Gemini (`gemini-3-flash-preview`) → TTS (CapCut/VieNeu/Edge...) → mix (ducking) → render 9:16 (blur BG, che sub Trung, sub Việt, watermark, NVENC).
Module chính: `v1_orchestrator.py`, `v1_audio_mixer.py`, `v1_timing_solver.py`, `v1_quality_gate.py`, `ocr_*.py`, `video_utils.py`, `canvas_settings.py`, `segment_editor_service.py`, `voice_preview_service.py`.

### A3. Studio Quy Trình (editor web) – đã làm
| Tính năng | Commit |
|---|---|
| Thumbnail chọn khung hình bất kỳ, chèn chữ, prepend đầu video; 12 nền HD, banner Douyin, khung viền | `81dfeab` |
| Timeline clip kiểu CapCut: split/clone/delete/kéo thả đổi thứ tự, ghép GPU | `9ae9c5e` |
| Phím C, Ctrl+Z/Y, Ctrl+C/V, Del; undo 40 bước | `72bd7d4` |
| Sửa sub trực tiếp trên video (contenteditable), Trước/Sau, auto-pause | `2fa9037` |
| Chèn trước/sau, 🧲 Hook, 📌 Nhân bản Hook, phát tuần tự | `05bf730` |
| Playhead + thước đo đồng bộ clip, Shift+←/→ ±0.1s, clip card responsive, re-time SRT khi ghép | `45479a8` |
| Fix NVDEC nv12 xanh nửa phải khi 9:16; tối ưu ASR/OCR/render | `c3dcfde`, `592276d` |

API editor: `GET /api/workflow/videos`, `video-data`, `stream-file`, `thumbnail/{capture,upload,prepend,download}`, `timeline/apply-edits` (nhận `clips[{start,end}]`, `target_version`; trả `video_url`, `subtitles` đã re-time), `save-subtitles`, `download-srt`, `export-custom`, `canvas-settings`, `batch-framing/*`, `detect-crop`.

State JS quan trọng trong `workflow.html`: `videoClipsState`, `selectedClipId`, `timelineUndoStack/RedoStack`, `copiedClipBuffer`, `currentSubtitles`, `currentVideoData`, `canvasSettings`. Hàm: `splitCurrentClip`, `moveClipToHook`, `insertClipAt`, `renderVideoClips`, `renderTimelineRuler`, `updatePlayhead`, `getEditedTimelineTime`, `seekTimelineToRatio`, `applyTimelineEditsToServer`.

### A4. Trạng thái repo / việc dang dở
- **Chưa commit** (git status): `.gitignore`, `requirements.txt`, `backend/ai/kinetic_scene_animator.py` sửa; mới: `backend/ai/{remotion,satori}_renderer.py`, `telegram_script_bot.py`, `telebot_video_downloader.py`, nhiều `tests/`, `docs/`, `tools/`, `bin/`, `temp_dir/`, `=ro` (file rác, nên xóa). Nên rà và commit theo nhóm trước khi làm tiếp.
- **Concat (jub0t/concat v0.2.7)**: đã tải `bin/concat/concat.msi` (97MB). Lệnh giải nén `msiexec /a` bị dừng do restart server → **thư mục `bin/concat/app` chưa có**. Cần chạy lại.
- Nên thêm `bin/`, `temp_dir/`, `*.log`, `test_*.mp4/png` vào `.gitignore`. `backend/app.log` đã 41MB.
- Bí mật: `backend/.env`, `douyin_cookies.txt` — không đưa vào repo/prompt.

### A5. Chạy / kiểm thử
- Server: do supervisor quản lý; muốn restart `main.py`: kill python `main.py` (supervisor tự bật lại sau ~5s), kiểm `netstat -ano | findstr :8088`.
- Test editor: Chrome DevTools MCP, mở `/quy-trinh`, gọi `selectVideo(...)` (click thẻ video qua JS có thể không ăn — gọi hàm trực tiếp).
- Tests: `pytest tests/`.

### A6. Về Concat (kết luận đánh giá)
Rust + Slint + wgpu, AGPL-3.0, app desktop độc lập, có `--project`, JSON-RPC, MCP (`concat --mcp`). **Không** có dịch/lồng tiếng/che sub Trung → chỉ dùng làm "trình dựng thủ công" cạnh Tool V1. Lưu ý AGPL: gọi như tiến trình ngoài thì ổn; không copy code vào tool nếu không muốn nhiễm giấy phép.

---
## PHẦN B. PROMPT GỬI TOOL/AGENT KHÁC (copy nguyên khối)

```text
VAI TRÒ: Bạn là kỹ sư full-stack (Python FastAPI + JS vanilla + FFmpeg) tiếp quản dự án "Tool V1 / Studio Quy Trình" tại C:\tool v1 (Windows, Python 3.10, GPU RTX 4050 6GB, NVENC).

BỐI CẢNH
- Tool Việt hóa video Trung Quốc + chống re-up. Editor web: backend/templates/workflow.html (~8800 dòng, JS thuần, không framework) phục vụ tại http://127.0.0.1:8088/quy-trinh. API: backend/workflow_api.py (prefix /api/workflow/*). Server: backend/main.py, được backend/background_service.py giám sát (kill main.py → tự bật lại).
- Editor đã có: timeline clip kiểu CapCut (split phím C, Ctrl+Z/Y/C/V, Del, kéo thả chèn trước/sau, Hook, nhân bản Hook), playhead+thước đo đồng bộ clip, Shift+←/→ ±0.1s, sửa sub trực tiếp trên video, thumbnail editor, watermark text, apply-edits (POST /api/workflow/timeline/apply-edits: nhận clips[{start,end}], ghép bằng FFmpeg GPU, re-time translated.srt/original.srt, trả subtitles).
- Mô hình dữ liệu client: videoClipsState=[{id,start,end}] tham chiếu thời gian của VIDEO NGUỒN; chưa có speed/volume/crop/transition/overlay/nhiều track.

RÀNG BUỘC
1. Không phá tính năng ổn định: che sub Trung, watermark, thumbnail 1080x1920, xuất NVENC, pipeline 8 bước, Telegram bot.
2. Không sửa/lộ backend/.env, douyin_cookies.txt. Không xóa dữ liệu workspace.
3. Giữ nguyên comment/docstring không liên quan. Commit nhỏ, thông điệp rõ (feat/fix(scope)).
4. UI tiếng Việt. Mọi thao tác đều có Ctrl+Z.
5. Render phải format=yuv420p trước crop/split (bug NVDEC nv12 xanh nửa phải).
6. Concat (jub0t/concat, AGPL) chỉ gọi như tiến trình ngoài, không copy code.

NHIỆM VỤ: Thực hiện lần lượt các giai đoạn trong file BAN_GIAO_VA_KE_HOACH_CAPCUT.md (Phần C): P0 dọn repo → P1 nền tảng timeline (zoom, snap, multi-select, ripple) → P2 clip properties (speed, volume, crop/zoom, fade) → P3 text/sticker/overlay track → P4 transition → P5 audio (waveform, tách audio, nhạc nền) → P6 project save/autosave + preview mượt + proxy → P7 tích hợp Concat. Sau mỗi giai đoạn: chạy pytest, kiểm thử trên Chrome DevTools (selectVideo → thao tác → apply-edits → xem video ra), commit, rồi báo cáo ngắn kèm tiêu chí nghiệm thu đã đạt.

BẮT ĐẦU: đọc workflow_api.py (api_apply_timeline_edits) và các hàm timeline trong workflow.html (renderVideoClips, pushTimelineHistory, applyTimelineEditsToServer), tóm tắt hiểu biết 10 dòng, rồi làm P0.
```

---
## PHẦN C. KẾ HOẠCH NÂNG CẤP GIỐNG CAPCUT

Nguyên tắc kiến trúc: chuyển từ "danh sách clip phẳng" sang **project JSON nhiều track** (single source of truth), preview bằng `<video>` + overlay HTML, render cuối bằng một `filter_complex` FFmpeg sinh từ JSON.

```json
{ "version":1, "fps":30, "canvas":{"w":1080,"h":1920},
  "tracks":[
   {"id":"v1","type":"video","clips":[{"id":"c1","src":"raw","in":0,"out":5,"at":0,"speed":1,"volume":1,"crop":{"x":0,"y":0,"w":1,"h":1,"zoom":1},"fadeIn":0,"fadeOut":0,"transition":null}]},
   {"id":"t1","type":"text","clips":[{"id":"x1","text":"...","at":2,"dur":3,"style":{}}]},
   {"id":"a1","type":"audio","clips":[...]}]}
```

### P0 – Dọn nền (0.5 ngày)
- Rà `git status`, commit theo nhóm (renderer/telebot/tests/docs), xóa `=ro`, bổ sung `.gitignore` (bin/, temp_dir/, *.log, test_*.mp4).
- Chạy lại giải nén Concat; ghi lại đường dẫn `concat.exe`.
- **Nghiệm thu**: `git status` sạch; pytest xanh; server lên 8088.

### P1 – Nền tảng timeline (2–3 ngày)
1. Zoom timeline (Ctrl+lăn chuột / slider), cuộn ngang, ruler thích ứng (hiện sửa `getTotalActiveDuration`).
2. **Snap** vào playhead, mép clip, mép sub (ngưỡng 8px) + bật/tắt (phím `N`).
3. Multi-select (Shift/Ctrl click, kéo chọn vùng), thao tác hàng loạt.
4. Ripple delete (mặc định) vs delete để trống (`Shift+Del`); khoảng trống (gap) hiển thị được.
5. Trim kéo mép có preview khung hình; phím `Q`/`W` trim đầu/cuối tới playhead; `J/K/L` tua; `,`/`.` lùi/tiến 1 frame.
6. Tách `videoClipsState` thành project JSON ở trên + migrate tương thích ngược; mọi history dùng snapshot JSON.
- **Nghiệm thu**: zoom 10%–800% mượt với video 4 phút; snap hoạt động; undo/redo đúng cho mọi thao tác.

### P2 – Thuộc tính clip (3–4 ngày)
1. **Speed** 0.25–4x (đổi nhịp clip trên timeline; backend `setpts` + `atempo` chuỗi); tuỳ chọn giữ pitch.
2. **Volume** từng clip + mute; fade in/out audio & video.
3. **Crop/Zoom/Pan** khung hình từng clip (kéo trên preview), xoay, lật.
4. **Freeze frame**, **Reverse**, **Tách audio** khỏi video.
5. Panel "Thuộc tính" bên phải theo clip đang chọn.
6. Backend: mở rộng `/timeline/apply-edits` nhận schema mới (`speed`, `volume`, `crop`, `fade*`); sinh filter từng clip → concat. Re-time sub phải tính theo speed.
- **Nghiệm thu**: clip 2x ngắn đi đúng, sub khớp; crop hiển thị đúng ở preview và file xuất; không còn nv12 xanh.

### P3 – Text / Sticker / Overlay (3–4 ngày)
1. Track text độc lập sub: thêm chữ tự do, font, màu, viền, bóng, nền, vị trí kéo thả trên preview, thời lượng kéo trên timeline.
2. Preset kiểu chữ (tái dùng preset sub hiện có) + animation vào/ra (fade, pop, trượt).
3. Track ảnh/video overlay (logo, sticker) với vị trí, scale, opacity; chroma/blend tuỳ chọn.
4. Watermark text hiện có chuyển thành một clip text khoá được.
5. Backend: burn bằng ASS (`ass_utils.py` sẵn có) cho text; `overlay` filter cho ảnh.
- **Nghiệm thu**: text xuất ra đúng vị trí/thời điểm như preview (sai lệch ≤ 2px, ≤ 1 frame).

### P4 – Transition & hiệu ứng (2–3 ngày)
1. Transition giữa 2 clip: fade, dissolve, slide, zoom, wipe (`xfade` FFmpeg, 0.2–1s); kéo thả vào điểm nối.
2. Filter cơ bản: độ sáng/tương phản/bão hoà/nhiệt màu (`eq`, `colorbalance`), làm mờ, vignette; preset.
3. Preview bằng CSS filter/canvas (gần đúng), render chính xác bằng FFmpeg.
4. Lưu ý: xfade làm ngắn tổng thời lượng → re-time sub/audio.
- **Nghiệm thu**: 3 loại transition xuất đúng, âm thanh crossfade không lệch.

### P5 – Âm thanh (2–3 ngày)
1. Waveform trên track audio (tính bằng ffmpeg/`audiowaveform` hoặc Web Audio, cache).
2. Track audio nhiều lớp: giọng AI, BGM, nhạc nền thêm vào, SFX; volume/ducking từng lớp, fade.
3. Kéo thả file nhạc từ máy; beat marker (tuỳ chọn, librosa).
4. Nối với mixer hiện có (`v1_audio_mixer.py`) thay vì viết lại.
- **Nghiệm thu**: waveform khớp âm thanh; mix xuất đúng mức dB đã đặt.

### P6 – Project, preview & hiệu năng (3 ngày)
1. Lưu/mở project JSON theo video (`workspace/<stem>/project.json`), **autosave** 10s, khôi phục sau crash.
2. Preview mượt: tạo **proxy 540p** nền, tua theo clip bằng 2 thẻ video preload (tránh giật khi nối), chế độ preview "ghép ảo" không cần render.
3. Render nền có tiến độ (job + SSE/polling), huỷ được, hàng đợi; xuất nhiều preset (1080x1920/1:1/16:9) — dùng NVENC VBR hiện có.
4. Tối ưu `workflow.html`: tách JS ra `static/js/timeline.js`, `project.js`, `inspector.js` (hiện 8.800 dòng 1 file), thêm test JS thuần cho logic thời gian.
- **Nghiệm thu**: đóng/mở trình duyệt vẫn còn project; render 3 phút < 2 phút trên GPU.

### P7 – Tích hợp Concat (1–2 ngày, song song được)
1. Cài Concat (MSI/giải nén), cấu hình đường dẫn trong Settings.
2. Nút "Mở trong Concat": gọi `concat.exe --project <dir>`; xuất project từ video đã dịch (video + sub SRT → track caption) qua JSON-RPC/MCP của Concat (`media_import`, `clips_add_sequence`, `captions_add`).
3. Nút "Nhập lại": lấy file Concat export → đưa vào thư viện Studio.
4. Tuân thủ AGPL: chỉ tiến trình ngoài.
- **Nghiệm thu**: 1 click mở Concat với đúng video + sub; export quay lại thư viện.

### P8 – Hoàn thiện (liên tục)
- Phím tắt panel (`?`), tooltips tiếng Việt, theme nhất quán.
- Bộ test: pytest cho schema/filter-graph builder; kịch bản DevTools cho thao tác chính.
- Tài liệu hướng dẫn dùng + changelog.

### Thứ tự ưu tiên (giá trị/nỗ lực)
P0 → P1 → P2 (speed, volume, crop) → P6.1 (autosave) → P3 (text) → P5 → P4 → P6.2–4 → P7.

### Rủi ro & cách giảm
| Rủi ro | Giảm |
|---|---|
| `workflow.html` 8.8k dòng dễ vỡ | tách module dần ở P6, test từng PR nhỏ |
| Preview ≠ file xuất | một schema, một bộ công thức thời gian dùng chung; test so khung hình |
| Re-time sub sai khi speed/transition | hàm `mapSourceToTimeline()` duy nhất, có unit test |
| VRAM 6GB khi render + pipeline | dùng `v1_gpu_gatekeeper`, không render khi pipeline đang chạy |
| NVDEC nv12 | luôn `format=yuv420p` đầu graph |
| AGPL của Concat | chỉ gọi ngoài tiến trình |
