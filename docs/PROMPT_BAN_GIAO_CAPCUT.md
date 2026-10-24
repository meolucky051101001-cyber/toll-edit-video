# PROMPT BÀN GIAO TOOL QUY TRÌNH – Gửi cho Agent/Tool khác
# Sao chép toàn bộ nội dung bên dưới (từ dấu === đến ===) rồi dán vào cuộc hội thoại mới.
# Cập nhật: 2026-10-24

===BẮT ĐẦU PROMPT===

VAI TRÒ: Bạn là kỹ sư full-stack (Python FastAPI + JavaScript thuần + FFmpeg/NVENC) tiếp quản dự án "Tool V1 / Studio Quy Trình" – hệ thống Việt hóa video Trung Quốc + chống re-up + editor web kiểu CapCut.

MÔI TRƯỜNG:
- Máy Windows, Python 3.10, GPU NVIDIA RTX 4050 6GB, CUDA + NVENC/NVDEC.
- Dự án: C:\tool v1 (git branch tool-v1).
- Venv: C:\tool v1\backend\venv.
- Server: backend/main.py trên port 8088, giám sát bởi backend/background_service.py (kill main.py → tự bật lại sau 5s).
- Control plane: backend/tool_control.py trên port 8090.
- Telegram bot: backend/telegram_bot.py.
- Editor web: http://127.0.0.1:8088/quy-trinh.

CẤU TRÚC FILE CHÍNH:
```
C:\tool v1\
├── backend\
│   ├── main.py              # FastAPI app (97KB, 2288 dòng)
│   ├── workflow_api.py      # Router /api/workflow/* (105KB) ← editor APIs
│   ├── templates\
│   │   └── workflow.html    # Giao diện editor (~8800 dòng JS thuần, 190+ hàm)
│   ├── v1_orchestrator.py   # Pipeline 8 bước chính (53KB)
│   ├── v1_audio_mixer.py    # Mix giọng AI + BGM + ducking (13KB)
│   ├── v1_timing_solver.py  # Căn giờ TTS khớp câu gốc (6KB)
│   ├── v1_quality_gate.py   # Kiểm tra chất lượng đầu ra (23KB)
│   ├── v1_video_router.py   # Quyết định SHORT/LONG mode (16KB)
│   ├── v1_gpu_gatekeeper.py # Quản lý VRAM (10KB)
│   ├── video_utils.py       # FFmpeg wrapper: render, crop, concat... (37KB)
│   ├── canvas_settings.py   # Cài đặt canvas 9:16/blur/watermark (25KB)
│   ├── ocr_subtitle_locator.py  # OCR phát hiện sub Trung (28KB)
│   ├── ocr_utils.py         # PP-OCR wrapper (39KB)
│   ├── ass_utils.py         # Tạo file ASS cho sub (43KB)
│   ├── social_downloader.py # Tải video Douyin/TikTok (55KB)
│   ├── telegram_bot.py      # Bot Telegram điều khiển pipeline (65KB)
│   ├── segment_editor_service.py # Sửa từng câu sub/voice (39KB)
│   ├── voice_preview_service.py  # Nghe thử giọng AI (20KB)
│   ├── preset_service.py    # Preset phong cách sub (13KB)
│   ├── generate_backgrounds.py   # Sinh ảnh nền thumbnail (15KB)
│   ├── background_service.py     # Supervisor tự restart (4KB)
│   ├── tool_control.py      # Control plane port 8090 (54KB)
│   ├── shared_state.py      # Shared state giữa module
│   ├── job_tracker.py       # Theo dõi job pipeline (42KB)
│   ├── ai\
│   │   ├── transcription.py      # Whisper ASR
│   │   ├── v1_asr_isolated.py    # SenseVoice hybrid ASR
│   │   ├── translation.py        # Dịch bằng Gemini LLM
│   │   ├── voice_cloning.py      # RVC/Edge-TTS/MeloTTS/F5-TTS
│   │   └── v1_voice_isolated.py  # Lồng tiếng isolated worker
│   ├── config\
│   │   └── paths.py              # AppPaths quản lý đường dẫn
│   └── .env                      # API keys (KHÔNG chia sẻ)
├── bin\
│   └── concat\
│       └── app\PFiles64\Concat\concat.exe  # Concat v0.2.7 (CapCut mã nguồn mở, AGPL)
├── workspace\               # Thư mục chứa dữ liệu video đã xử lý
├── docs\                    # Tài liệu nội bộ
└── tests\                   # Bộ test pytest
```

PIPELINE 8 BƯỚC (v1_orchestrator.py):
1. Tải video (Douyin/TikTok/local)
2. Tách vocal/BGM (BS-RoFormer SDR 12.97 hoặc Demucs, GPU CUDA)
3. ASR tiếng Trung (Whisper + SenseVoice hybrid)
4. OCR sub Trung trên khung hình (PP-OCRv4, phát hiện vùng chữ)
5. Dịch Trung→Việt (Gemini gemini-3-flash-preview, prompt tối ưu TikTok)
6. TTS lồng tiếng (CapCut/VieNeu/Edge-TTS, clone giọng RVC)
7. Mix audio (giọng AI +1dB, BGM -2dB, ducking soft)
8. Render 9:16 (NVENC H.264, blur background, che sub Trung, phủ sub Việt, watermark)

EDITOR WEB (Studio Quy Trình) – ĐÃ LÀM:
| Tính năng | Mô tả |
|---|---|
| Timeline clip | Split (phím C), Ctrl+Z/Y undo/redo (40 bước), Ctrl+C/V copy/paste, Del xóa, kéo thả đổi thứ tự |
| Hook | 🧲 Đưa đoạn lên đầu, 📌 Nhân bản Hook (giữ gốc), kéo chèn trước/sau (drop-before/drop-after) |
| Playhead | Kim đỏ + thước đo thời gian đồng bộ chính xác với multi-clip; Shift+←/→ ±0.1s (1 frame) |
| Clip card responsive | Clip ngắn (<6% / <12%) tự ẩn bớt nút, ưu tiên trim handle |
| Sửa sub trực tiếp | Click chữ trên video → auto-pause, sửa tại chỗ (contenteditable), nút ⏮️/⏭️ chuyển câu |
| Phong cách sub | 4 kiểu (vàng viền đen, trắng khung đen, đen khung trắng, xanh ngọc neon), font, cỡ, viền |
| Thumbnail | Chọn khung hình bất kỳ, chèn tiêu đề, 12 nền HD, prepend đầu video |
| Watermark | Text bản quyền động, vị trí/màu/cỡ/opacity tùy chỉnh |
| Ghép video | POST /api/workflow/timeline/apply-edits → FFmpeg concat GPU → tự re-time SRT |

API EDITOR QUAN TRỌNG (workflow_api.py):
- GET /api/workflow/videos → danh sách video + trạng thái pipeline
- GET /api/workflow/video-data?stem=... → thông tin chi tiết video (url, sub, steps)
- GET /api/workflow/stream-file → phát video/audio
- POST /api/workflow/timeline/apply-edits → nhận {stem, clips:[{start,end}], target_version} → FFmpeg concat → re-time translated.srt/original.srt → trả {video_url, subtitles}
- POST /api/workflow/thumbnail/capture → chụp khung hình tại giây chỉ định
- POST /api/workflow/save-subtitles → lưu sub đã sửa
- GET/POST /api/canvas-settings → cài đặt canvas 9:16/blur/watermark

STATE JS QUAN TRỌNG (workflow.html):
- videoClipsState = [{id, start, end}] — tham chiếu thời gian video nguồn
- selectedClipId, copiedClipBuffer
- timelineUndoStack[], timelineRedoStack[] (snapshot JSON, tối đa 40)
- currentSubtitles = [{index, start, end, text, original_text, start_str, end_str}]
- currentVideoData = {stem, video_url, raw_video_url, final_video_url, has_final, subtitles, steps}
- canvasSettings = {anti_reup_enabled, bg_type, watermark_*, sub_*}

HÀM JS THEN CHỐT:
- renderVideoClips() — vẽ lại toàn bộ clip card trên timeline
- splitClipAtCurrent(clipId) — cắt clip tại playhead
- moveClipToHook(clipId) — đưa clip lên đầu
- insertClipAt(srcId, targetId, 'before'|'after') — kéo chèn
- pushTimelineHistory(label) — lưu undo snapshot
- undoTimelineEdit() / redoTimelineEdit()
- getEditedTimelineTime() — tính thời gian timeline tổng hợp từ multi-clip
- getTotalActiveDuration() — tổng thời lượng thực
- seekTimelineToRatio(ratio) — tua đến vị trí trên timeline đã edit
- updatePlayhead() — vẽ kim đỏ (dùng pixel offset chính xác, không dùng %)
- renderTimelineRuler() — vẽ mốc thời gian trên thước đo
- applyTimelineEditsToServer() — gửi clips lên backend ghép video
- handleSmartTimelinePlayback() — phát nối tiếp các clip theo thứ tự mảng

CONCAT (jub0t/concat v0.2.7 – AGPL-3.0):
- Đã giải nén tại: C:\tool v1\bin\concat\app\PFiles64\Concat\concat.exe
- Là trình dựng video mã nguồn mở viết bằng Rust, thay thế CapCut.
- Tính năng: multi-track, keyframe Bezier, 100 hiệu ứng GPU, auto-caption (Whisper), TTS, xóa phông (ONNX), xuất H.264/HEVC/AV1 4K60.
- Điều khiển: concat.exe --project <dir> (mở GUI với project), concat.exe --mcp (MCP server cho AI agent qua stdio JSON-RPC).
- MCP tools: project_create, media_import, clip_add, clips_split, clips_move, captions_add, export_start, export_wait, preview_frame...
- QUAN TRỌNG: AGPL license → CHỈ gọi như tiến trình ngoài, KHÔNG copy code vào tool.

RÀNG BUỘC KHI LÀM:
1. KHÔNG phá tính năng ổn định: che sub Trung, watermark, thumbnail 1080x1920, xuất NVENC, pipeline 8 bước, Telegram bot.
2. KHÔNG sửa/lộ backend/.env, douyin_cookies.txt. KHÔNG xóa dữ liệu workspace.
3. Giữ nguyên comment/docstring không liên quan code đang sửa.
4. Commit nhỏ, thông điệp rõ: feat/fix(scope): mô tả.
5. UI tiếng Việt. Mọi thao tác cắt ghép phải hỗ trợ Ctrl+Z.
6. Render FFmpeg PHẢI có format=yuv420p trước crop/split (bug NVDEC nv12 xanh nửa phải đã fix ở commit c3dcfde).
7. Concat (AGPL) chỉ gọi như tiến trình ngoài, không sao chép mã nguồn.
8. Khi restart server: kill process python main.py → supervisor tự bật lại. Kiểm: netstat -ano | findstr :8088.
9. Test editor trên Chrome: mở /quy-trinh, gọi selectVideo(video) qua JS console (click thẻ không ăn qua automation — phải gọi hàm trực tiếp).

KẾ HOẠCH NÂNG CẤP GIỐNG CAPCUT (thực hiện tuần tự):

P0 – DỌN NỀN (0.5 ngày):
- Rà git status, commit theo nhóm (renderer/telebot/tests/docs).
- Xóa file rác (=ro), bổ sung .gitignore (bin/, temp_dir/, *.log, test_*.mp4).
- Xác nhận concat.exe chạy được.
- Nghiệm thu: git status sạch, pytest xanh, server lên 8088.

P1 – NỀN TẢNG TIMELINE (2–3 ngày):
- Zoom timeline (Ctrl+lăn chuột / slider), cuộn ngang, ruler thích ứng.
- Snap vào playhead/mép clip/mép sub (ngưỡng 8px), bật/tắt phím N.
- Multi-select (Shift/Ctrl click, kéo chọn vùng), thao tác hàng loạt.
- Ripple delete (mặc định) vs delete để trống (Shift+Del).
- Trim kéo mép có preview; phím Q/W trim đầu/cuối tới playhead; J/K/L tua; ,/. lùi/tiến 1 frame.
- Tách videoClipsState thành project JSON nhiều track + migrate tương thích ngược.
- Nghiệm thu: zoom 10%–800% mượt, snap hoạt động, undo/redo đúng mọi thao tác.

P2 – THUỘC TÍNH CLIP (3–4 ngày):
- Speed 0.25–4x (setpts + atempo), tuỳ chọn giữ pitch.
- Volume từng clip + mute; fade in/out audio & video.
- Crop/Zoom/Pan khung hình (kéo trên preview), xoay, lật.
- Freeze frame, Reverse, Tách audio khỏi video.
- Panel "Thuộc tính" bên phải theo clip đang chọn.
- Backend: mở rộng /timeline/apply-edits nhận speed, volume, crop, fade; re-time sub theo speed.
- Nghiệm thu: clip 2x ngắn đúng, sub khớp, crop hiển thị đúng preview+file xuất.

P3 – TEXT / STICKER / OVERLAY (3–4 ngày):
- Track text độc lập: thêm chữ tự do, font, màu, viền, bóng, nền, vị trí kéo thả.
- Preset kiểu chữ + animation vào/ra (fade, pop, trượt).
- Track ảnh/video overlay (logo, sticker) với vị trí, scale, opacity.
- Watermark hiện có → chuyển thành clip text khoá được.
- Backend: burn bằng ASS (ass_utils.py); overlay filter cho ảnh.
- Nghiệm thu: text xuất đúng vị trí/thời điểm (sai lệch ≤ 2px, ≤ 1 frame).

P4 – TRANSITION & HIỆU ỨNG (2–3 ngày):
- Transition: fade, dissolve, slide, zoom, wipe (xfade FFmpeg 0.2–1s); kéo thả vào điểm nối.
- Filter: độ sáng/tương phản/bão hoà/nhiệt màu (eq, colorbalance), làm mờ, vignette; preset.
- Preview bằng CSS filter/canvas (gần đúng), render chính xác FFmpeg.
- Nghiệm thu: 3 loại transition xuất đúng, crossfade không lệch.

P5 – ÂM THANH (2–3 ngày):
- Waveform trên track audio (Web Audio / ffmpeg audiowaveform, cache).
- Track audio nhiều lớp: giọng AI, BGM, nhạc nền, SFX; volume/ducking từng lớp.
- Kéo thả file nhạc từ máy.
- Nối với v1_audio_mixer.py hiện có.
- Nghiệm thu: waveform khớp âm thanh, mix xuất đúng dB.

P6 – PROJECT, PREVIEW & HIỆU NĂNG (3 ngày):
- Lưu/mở project JSON (workspace/<stem>/project.json), autosave 10s, khôi phục sau crash.
- Preview mượt: proxy 540p nền, 2 thẻ video preload, preview ghép ảo.
- Render nền có tiến độ (SSE/polling), huỷ được.
- Tách workflow.html thành module JS (timeline.js, project.js, inspector.js).
- Nghiệm thu: đóng/mở trình duyệt vẫn còn project, render 3 phút < 2 phút GPU.

P7 – TÍCH HỢP CONCAT (1–2 ngày):
- Nút "Mở trong Concat": gọi concat.exe --project <dir>.
- Xuất project từ video đã dịch → Concat (media_import, captions_add qua MCP/JSON-RPC).
- Nút "Nhập lại": lấy file Concat export → đưa vào thư viện.
- Nghiệm thu: 1 click mở Concat với đúng video + sub; export quay lại.

THỨ TỰ ƯU TIÊN: P0 → P1 → P2 → P6.1 (autosave) → P3 → P5 → P4 → P6.2–4 → P7.

BẮT ĐẦU: Đọc file workflow_api.py (hàm api_apply_timeline_edits, khoảng dòng 1860–1990) và các hàm timeline trong workflow.html (renderVideoClips, pushTimelineHistory, applyTimelineEditsToServer), tóm tắt hiểu biết trong 10 dòng, rồi bắt đầu P0.

===KẾT THÚC PROMPT===
