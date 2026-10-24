# ĐÁNH GIÁ 20 REPO ANIMATION / VIDEO / 3D — KHẢ NĂNG ÁP DỤNG CHO STUDIO KỊCH BẢN (TOOL V1)

- **Ngày đánh giá:** 2026-10-18
- **Người thực hiện:** AI Review (OpenCode) — đọc README, GitHub API, npm registry, đối chiếu trực tiếp mã nguồn `C:\tool v1`
- **Đối tượng áp dụng:** Studio Kịch Bản (`/kich-ban`) — `backend/script_api.py`, `backend/ai/prompt_to_script.py`, `backend/ai/doodle_engine.py`, `backend/ai/kinetic_scene_animator.py`, `backend/ai/whiteboard_hand_engine.py`, `backend/templates/script_studio.html`
- **Môi trường máy đã xác minh:** Node.js **v26.4.0**, npm **11.17.0**, FFmpeg **8.1.2-full (gyan)**, GPU NVIDIA (CUDA 0) — **máy đã sẵn sàng chạy toàn bộ stack JS bên dưới**
- **Chú thích mức độ:** ⭐ = nên áp dụng ngay (rủi ro thấp, lợi ích rõ) · 🟡 = thử nghiệm có kiểm soát (PoC trước khi cam kết) · 🔵 = theo dõi (điều kiện chưa chín) · ⚪ = bỏ qua ở giai đoạn này

> Số liệu (sao, ngày cập nhật, version) là **snapshot tại 2026-10-18**.

---

## 0. BỐI CẢNH: TOOL KỊCH BẢN HIỆN TẠI ĐANG LÀM GÌ

Để đánh giá "áp dụng được gì" cho đúng chỗ, trước tiên chốt lại kiến trúc hiện tại (đã đọc code):

1. **Đầu vào:** Prompt ý tưởng hoặc kịch bản thô (user dán vào `/kich-ban`).
2. **Gemini sinh kịch bản:** `prompt_to_script.py` → JSON `{title, keyword_accent, scenes[]}`. Mỗi scene gồm: `narration` (12–18 từ), `doodle_label`, `image_prompt` (tiếng Anh), `visual_type`, `icon_keywords`, `layout` (dual_confrontation / single_centered / action_duo), `action_type` (race, chart_pie, chart_bar, wallet_squeeze, brain_think, fight, confrontation, showcase), `camera_motion` (zoom_in, pan_right, shake, pulse).
3. **Render hình (Band B):** `doodle_engine.py` + `kinetic_scene_animator.py` — vẽ **thủ công bằng PIL/cv2**: icon vector tải từ **Iconify API** → rasterize bằng `resvg_py`, cộng bộ vẽ procedural vẽ từng hành động (đua xe, biểu đồ, hỗn chiến…); chế độ bảng trắng dùng `whiteboard_hand_engine.py` (thư viện `handdraw`, bàn tay thật vẽ nét + CUDA/NVENC).
4. **Render chữ:** Band A (banner tiêu đề) và Band C (phụ đề) — **layout chữ bằng PIL thủ công** (wrap chữ, căn giữa, tô màu từ khóa).
5. **Ghép cảnh:** mỗi cảnh 1 clip ngắn (TTS CapCut + hình), nối bằng FFmpeg; có `camera_motion` xử lý kiểu zoom/shake/pulse procedural.
6. **Hậu kỳ & xuất:** FFmpeg NVENC H.264, 720×1280, 3 dải (Banner / Doodle / Phụ đề).

**Các "điểm đau" mà 20 repo này có thể giải quyết:**

| # | Điểm đau hiện tại | Nhóm repo giải quyết |
|---|---|---|
| P1 | Layout chữ tiêu đề/phụ đề thủ công bằng PIL — chỉnh style đẹp tốn công, khó auto-fit | Satori, Remotion, GSAP/SplitText |
| P2 | Hành động procedural chỉ có 8 `action_type` cố định, khó thêm hiệu ứng mới | Remotion, GSAP, Lottie |
| P3 | Chuyển động máy quay cơ bản (zoom/pan/shake/pulse) viết tay | Remotion + GSAP/Motion, FFmpeg zoompan |
| P4 | Không có hiệu ứng "vẽ nét" cho icon (chỉ có hand-draw của bảng trắng) | Lottie draw-on, `@remotion/paths` |
| P5 | Preview cảnh trong dashboard tĩnh/đơn giản | Howler, dotLottie-web, GSAP/Motion |
| P6 | Muốn style mới (kinetic typography, 3D, giải thích dữ liệu) | Remotion, three.js stack |
| P7 | AI agent viết code frontend/animation hay sai cú pháp lib | 3 repo Agent Skills (GSAP/Motion/Remotion) |

**Tiêu chí đánh giá từng repo:** (a) cải thiện chất lượng/năng suất render video kịch bản? (b) cải thiện dashboard/preview? (c) cung cấp tài nguyên hình ảnh (icons, Lottie, Rive)? (d) cải thiện quy trình dev với AI agent? (e) license — bảo trì — chi phí tích hợp.

---

## 1. BẢNG TỔNG KẾT NHANH (20 REPO)

| # | Repo | Là gì | License | Hoạt động | Liên quan tới tool kịch bản | Kết luận |
|---|---|---|---|---|---|---|
| 1 | remotion-dev/skills | Bộ Agent Skills chính chủ của Remotion | (repo skills, dùng tự do) | Rất tích cực (10/2026) | Dạy AI agent viết Remotion đúng chuẩn + kỹ năng captions/multimedia | 🟡 cài ngay, dùng khi PoC Remotion |
| 2 | motiondivision/ai-kit | Plugin Cursor + skill + MCP cho Motion | Skill MIT | Mới, cập nhật liên tục | Dạy agent viết animation Motion đúng; hỗ trợ OpenCode | 🟡 |
| 3 | greensock/gsap-skills | Bộ Agent Skills chính chủ của GSAP | MIT | Ổn định (7/2026) | Dạy agent viết GSAP chuẩn (core, plugins, React, performance) | ⭐ cài ngay |
| 4 | svg/svgo | Trình tối ưu SVG (Node CLI + API) | MIT | Rất tích cực | Làm sạch/nén SVG trước khi rasterize (Iconify cache, asset user upload) | 🟡 |
| 5 | vercel/satori | HTML/CSS → SVG trong Node (không cần browser) | MPL-2.0 | Rất tích cực | **Thay layout chữ PIL** cho Band A/C, sinh thumbnail cảnh | 🟡 tiềm năng cao |
| 6 | greensock/GSAP | Engine animation JS kinh điển (timeline, plugins) | Miễn phí 100% (kể cả thương mại) | Rất tích cực | UI dashboard + animation trong cảnh Remotion; DrawSVG/SplitText/MotionPath | ⭐ |
| 7 | juliangarnier/anime | Engine animation JS nhẹ | MIT | Rất tích cực (v4.5.0) | Phương án nhẹ thay GSAP cho canvas/SVG; **không dùng song song** | 🔵 |
| 8 | motiondivision/motion | Thư viện animation React/JS/Vue (ex-Framer Motion) | MIT | Rất tích cực | UI dashboard (spring, layout, gesture); motion-ai-kit hỗ trợ | 🟡 |
| 9 | rive-app/rive-wasm | Runtime Rive (WASM) cho web | MIT (runtime) | Tích cực | Nhân vật/icon tương tác — nhưng cần asset `.riv` dựng trong editor SaaS | 🔵 |
| 10 | LottieFiles/dotlottie-web | Player Lottie/dotLottie (Rust+WASM, ThorVG) | MIT | Tích cực | Icon/hoạt họa vector draw-on hợp style doodle; preview dashboard | 🟡 |
| 11 | remotion-dev/remotion | "React → video" — render bằng Chrome headless + FFmpeg | **License đặc biệt** (miễn phí ≤3 nhân sự) | Rất tích cực (v4.0.536) | **Ứng viên nâng cấp engine doodle lên chuẩn thiết kế chuyên nghiệp**; captions từ Whisper | 🟡 PoC quyết định |
| 12 | motion-canvas/motion-canvas | Anim vector bằng code TS + editor realtime | MIT | **Đình trệ** (release cuối 2/2025) | Đúng concept "explainer + voiceover" nhưng không nên đặt cược | 🔵/⚪ |
| 13 | mrdoob/three.js | Engine 3D web chuẩn công nghiệp | MIT | Cực kỳ tích cực | Chỉ khi mở style 3D mới | 🔵 |
| 14 | pmndrs/react-three-fiber | React renderer cho three.js | MIT | Tích cực | Đi kèm #13 | 🔵 |
| 15 | pmndrs/drei | Bộ helper cho R3F (camera, text, môi trường…) | MIT | Tích cực | Đi kèm #13 | 🔵 |
| 16 | pmndrs/react-three-rapier | Physics Rapier cho R3F | MIT | Tích cực | Va chạm/vật lý cho cảnh 3D — niche | 🔵 |
| 17 | pmndrs/react-postprocessing | Hiệu ứng hậu kỳ cho R3F (bloom, DOF…) | MIT | Tích cực | Đi kèm #13 | 🔵 |
| 18 | goldfire/howler.js | Thư viện audio playback web | MIT | Ổn định | **Preview player trong dashboard** (audition từng cảnh, fade/ducking) | 🟡 |
| 19 | Vanilagy/mediabunny | Toolkit media thuần TS (đọc/ghi/chuyển MP4/WebM…) | MPL-2.0 | Rất tích cực | Chỉ cần khi làm editor client-side / theo Remotion 5 | 🔵 |
| 20 | FFmpeg/FFmpeg | Bộ công cụ media huyền thoại | LGPL/GPL | Cực kỳ tích cực | **Đang dùng**; tối ưu thêm: ass subtitles, xfade, zoompan, filter_complex, hwaccel | ⭐ |

**Tổng đếm:** ⭐ 4 · 🟡 8 · 🔵 7 (+1 xếp 🔵/⚪) — nghĩa là **không nên "áp dụng đại trà" 20 repo**; chỉ có một nhóm nhỏ tạo giá trị thật ngay, phần còn lại là option tương lai.

---

## 2. ĐÁNH GIÁ CHI TIẾT TỪNG REPO

### NHÓM A — AGENT SKILLS (dành cho AI coding agent của bạn)

Đây là nhóm mới nổi 2026: thay vì bạn phải đọc docs lib, các lib tự phát hành "skill" (file hướng dẫn chuẩn) để Claude Code / Cursor / Codex / OpenCode viết code đúng ngay từ đầu. Với người vận hành tool **bằng AI agent** như bạn (repo đã có `.agent/rules/`, backup `*.backup_before_codex`), nhóm này là **đòn bẩy rẻ nhất**.

---

#### 1. remotion-dev/skills — "Remotion Agent Skills" ⭐5.3k · tạo 2026-01 · cập nhật 2026-10-16

- **Nội dung:** repo chứa các skill Markdown cho agent: `remotion-best-practices` (bọc mọi skill con), `remotion-create`, `remotion-markup`, `remotion-studio`, `remotion-render`, `remotion-maps` (Mapbox/MapLibre/Cesium), `remotion-captions` (**import SRT, hiển thị phụ đề, transcribe**), `remotion-saas`, `remotion-interactivity`, `remotion-docs` (search docs), `remotion-upgrade`, `remotion-multimedia` (hướng dẫn dùng Mediabunny).
- **Cài đặt:** `npx skills add remotion-dev/skills` (CLI đa agent — hỗ trợ Claude Code, Codex, Cursor, OpenCode...).
- **Áp dụng cho tool kịch bản:**
  - Nếu PoC Remotion (mục 11) thành công → cài skill này để agent của bạn tự viết composition doodle chuẩn (layout, timing, captions, fonts).
  - Skill `remotion-captions` đặc biệt hợp: bạn đã có timestamp Whisper từng mili-giây ở pipeline lõi — Remotion có API import caption chuẩn SRT/word-level để làm **phụ đề karaoke kiểu CapCut**.
  - Kể cả khi chưa dùng Remotion, đọc skill captions là tài liệu tham khảo tốt cho cách tổ chức caption data.
- **Rủi ro:** gần như bằng 0 (file hướng dẫn, không phải code chạy).
- **Kết luận:** 🟡 Cài ngay (miễn phí, 5 phút), giá trị thật bung ra khi có PoC Remotion.

#### 2. motiondivision/ai-kit — "Motion AI Kit" 🆕 43★ · tạo 2026-07

- **Nội dung:** plugin chính chủ cho Motion: (a) best-practices animation do tác giả Motion viết tay; (b) **MCP server** phục vụ full docs Motion cho agent (Motion+ mở thêm source 450+ example); (c) **sinh CSS spring** qua easing `linear()` — hữu ích cho UI mượt không cần JS; (d) **MotionScore** — audit hiệu năng animation theo điểm S→F (tính năng Motion+, trả phí).
- **Cài đặt:** `npx motion-ai` — cấu hình được cho **OpenCode** (đúng setup của bạn), Claude Code, Codex, Gemini CLI, Copilot...
- **Áp dụng:** nâng chất lượng animation cho dashboard `/kich-ban` (trang hiện là HTML/JS thuần nhưng Motion có bản JS framework-agnostic). Kèm đó docs MCP giúp agent trả lời "viết animation X thế nào cho đúng".
- **Rủi ro:** repo non (43★, mới 3 tháng) — nhưng là repo chính chủ motion.dev, và skill MIT.
- **Kết luận:** 🟡 Cài (rẻ), dùng cho UI; MotionScore không cần (phần trả phí).

#### 3. greensock/gsap-skills — "GSAP AI Skills" ⭐16.6k · MIT · cập nhật 2026-07-29

- **Nội dung:** 6+ skill chính chủ: `gsap-core` (tween/timeline/easing/stagger), `gsap-plugins` (ScrollTo, Flip, Draggable, Observer, **SplitText**, **MorphSVG**, **DrawSVG**, **MotionPath**, CustomEase...), `gsap-react`, `gsap-scrolltrigger`, `gsap-frameworks` (Vue/Nuxt/Svelte...), `gsap-performance`. Kèm example app hoàn chỉnh (react/vanilla/vue/nuxt) và `.github/instructions` cho Copilot.
- **Sự kiện quan trọng về license:** sau khi **Webflow mua GSAP**, **toàn bộ plugin (kể cả SplitText, MorphSVG từng là "Club GSAP") giờ miễn phí 100% kể cả thương mại** — không cần token/registry riêng. Điều này khác hoàn toàn hiểu biết cũ của nhiều người.
- **Cài đặt:** `npx skills add https://github.com/greensock/gsap-skills --agent opencode` hoặc copy `skills/` vào `~/.config/opencode/skills/`.
- **Áp dụng cho tool kịch bản:**
  - **Dashboard UI:** animation mượt cho Studio Kịch Bản (chuyển scene, hover card, progress bar, preview transitions).
  - **Trong video (nếu dùng Remotion):** GSAP chạy được trong composition theo chế độ "điều khiển bằng frame" (`tl.seek(frame/fps)`) — DrawSVG = hiệu ứng **vẽ nét** cho icon/line-art, MotionPath = đường di chuyển đối tượng (thay `camera_motion` code tay), SplitText = kinetic typography cho banner tiêu đề.
  - **Ngoài video:** dựng HTML preview "xem thử cảnh" ngay trong dashboard bằng GSAP timeline — không cần render.
- **Rủi ro:** GSAP chỉ chạy trong môi trường DOM/browser — không thay được renderer Python; đây là lib hỗ trợ, không phải engine video.
- **Kết luận:** ⭐ Áp dụng ngay (cài skill + dùng GSAP cho UI frontend; tiềm năng lớn cho video nếu chọn Remotion).

---

### NHÓM B — RENDER ENGINE (trái tim của quyết định lớn)

#### 11. remotion-dev/remotion — "Make videos programmatically with React" ⭐64.8k · v4.0.536 (2026-10-16)

- **Bản chất:** biến React component thành video. Render bằng Chrome headless (1 tiến trình render frame), encode bằng FFmpeg đi kèm. Có **Studio** (preview realtime, tua frame), **Player** (nhúng preview vào web), **SSR/render queue**, **Lambda** (render phân tán cloud), client-side rendering, và hệ sinh thái chính chủ cực rộng:
  - `@remotion/captions` (phụ đề chuẩn SRT, word-level, import từ Whisper),
  - `@remotion/paths` (MIT) — **thao tác SVG path: animate nét vẽ draw-on, morph path** ← đúng chất whiteboard/doodle,
  - `@remotion/lottie` (chạy Lottie trong video), `@remotion/rive`, `@remotion/three` (R3F trong video), `@remotion/skia`, `@remotion/shapes`, `@remotion/transitions`, `@remotion/media-utils`, fonts, SFX...
- **License (ĐỌC KỸ):** "Remotion License" — **miễn phí cho cá nhân, tổ chức ≤3 nhân sự, phi lợi nhuận** (kể cả video thương mại); tổ chức vì lợi nhuận >3 người phải mua Company License. **Remotion 5.0 sẽ chỉnh license** — theo dõi khi nâng cấp.
- **Áp dụng cho tool kịch bản (phân tích thẳng):**
  - Tool của bạn về bản chất đã là "script → video" nhưng phần hình ảnh bị giới hạn bởi PIL/cv2. Remotion mở khóa: motion graphics chuẩn design (typography đẹp, transition, particle, kinetic text), render frame-accurate, tái sử dụng **đúng** dữ liệu scene JSON bạn đã có (narration, layout, action_type...) bằng cách truyền props.
  - **Ghép khớp hoàn hảo với phần còn lại của Tool V1:** Whisper timestamps → captions; CapCut TTS mp3 → audio track; Timing Solver giữ nguyên; FFmpeg NVENC vẫn dùng để encode/ghép (Remotion tự encode được nhưng có thể xuất frame/raw rồi để pipeline cũ ghép).
  - **Kiến trúc đề xuất:** Node sidecar `tools/render_node/` (giống pattern `model_venv_separator` bạn đang dùng): `npx remotion render <Composition> --props=scenes.json out.mp4`, gọi từ FastAPI bằng subprocess có timeout/progress parse. Render **từng cảnh** để giới hạn RAM/VRAM rồi ghép bằng FFmpeg quen thuộc.
  - **Chi phí thật:** cần viết lại style doodle thành component React/TS (ước lượng 1–2 tuần cho 1 style "ngon"), Chrome headless ngốn RAM (theo dõi peak), thêm "chi nhánh" Node vào vận hành (pin version, offline node_modules). Đổi lại: tốc độ lặp style nhanh hơn nhiều và chất lượng trần cao hơn hẳn PIL.
- **Kết luận:** 🟡 **PoC 1 cảnh là quyết định bắt buộc** trước khi cam kết. Tiêu chí nghiệm thu PoC ở mục 4.

#### 12. motion-canvas/motion-canvas — "Visualize Your Ideas With Code" ⭐19.4k · MIT

- **Bản chất:** thư viện TS dùng **generator** để hoạt hình + editor realtime; tự mô tả là "công cụ chuyên dụng tạo **animation vector giải thích thông tin và đồng bộ với voice-over**" — nghe như sinh ra cho tool kịch bản của bạn.
- **Vấn đề sống-còn:** **dự án gần như đình trệ** — release gần nhất `v3.18.0-alpha.0` (2025-02-16), commit thật cuối cùng 2025-02 (một commit docs lẻ 2026-07), 175 issue mở. Không có lộ trình render farm, không có AI tooling.
- **Áp dụng:** ý tưởng hay để tham khảo (generator-based animation, signal system đồng bộ narration); **không nên** đưa vào production vì rủi ro bị bỏ rơi và không có cộng đồng tooling quanh nó thời điểm này.
- **Kết luận:** 🔵/⚪ Theo dõi, không triển khai. Nếu nó hồi sinh → xếp hạng lại.

---

### NHÓM C — THƯ VIỆN ANIMATION WEB (GSAP / anime.js / Motion)

Ba thư viện này **cùng giải một bài toán** ở tầng browser; chọn 1–2, đừng ôm cả 3.

#### 6. greensock/GSAP ⭐29.4k · chuẩn công nghiệp · miễn phí toàn bộ

- **Mạnh nhất về:** timeline tuần tự phức tạp (đúng kiểu kịch bản nhiều cảnh), độ chính xác thời gian, plugin đặc thù: **DrawSVG** (vẽ nét), **MorphSVG** (biến hình icon), **MotionPath** (đối tượng đi theo đường), **SplitText** (tách chữ để làm kinetic typography), **Flip** (transition layout), **ScrollTrigger** (không cần cho video nhưng tốt cho dashboard). Không phụ thuộc framework.
- **Áp dụng:** (1) UI dashboard; (2) bên trong Remotion để animate cảnh — dùng timeline + `seek()` theo frame để giữ tính xác định; (3) riêng DrawSVG/MotionPath chính là công cụ chuẩn để làm hiệu ứng "tay vẽ nét" cho vector mà không phải tự tính từng path.
- **Kết luận:** ⭐ Nền tảng chính cho animation web của bạn.

#### 7. juliangarnier/anime.js ⭐73.7k · MIT · v4.5.0 (2026-06)

- **Mạnh:** nhẹ (~vài chục KB), API đơn giản, animate CSS/SVG/DOM/JS object, v4 có timeline, stagger, draggable, scroll, WAAPI.
- **Áp dụng:** thay thế GSAP ở chỗ chỉ cần hiệu ứng đơn giản (nút bấm, icon nhấp nháy, số đếm). Với dashboard vanilla HTML của bạn thì nhúng anime.js dễ như GSAP.
- **Kết luận:** 🔵 Để dành. Nếu chỉ chọn một thì chọn GSAP vì plugin vẽ nét/morph là thứ bạn sẽ cần cho doodle + kinetic text.

#### 8. motiondivision/motion (ex-Framer Motion) ⭐34.3k · MIT · v14.3.0

- **Mạnh:** spring vật lý, layout animation tự động, gesture, `AnimatePresence`; bản JS framework-agnostic dùng được không cần React; có repo skill riêng (mục 2).
- **Áp dụng:** nếu sau này dashboard chuyển sang React (nhiều khả năng bạn sẽ làm cho trình preview scene), Motion là lựa chọn UI mặc định; còn hiện tại giá trị chính là UI polish.
- **Kết luận:** 🟡 Cho UI (tương lai gần), không dùng cho video.

---

### NHÓM D — CÔNG CỤ 2D / LAYOUT (SVGO, Satori)

Đây là nhóm **đáng giá nhất tính theo ROI cho engine hiện tại** vì không cần thay renderer — chỉ nâng cấp 2 khâu yếu (asset + chữ).

#### 5. vercel/satori — HTML/CSS → SVG (Node, KHÔNG cần browser) ⭐14.1k · MPL-2.0 · v0.62

- **Bản chất:** nhận JSX/React-element (hoặc object) + CSS (subset flexbox) + font buffers → trả **chuỗi SVG** y hệt layout HTML/CSS. Không có state/effect; là layout engine + renderer SVG thuần, chạy cực nhanh (được Vercel dùng cho ảnh OG). Raster hóa thì bạn đã có sẵn `resvg_py` trong Python!
- **Áp dụng cho tool kịch bản — 3 use case thật:**
  1. **Thay layout PIL cho Band A (banner) & Band C (phụ đề):** viết template HTML/CSS một lần (auto-fit font, gradient text, viền chữ, highlight từ khóa đỏ, icon cạnh chữ) → Satori sinh SVG → `resvg_py` raster trong pipeline Python. Chất lượng chữ tiếng Việt (dấu) tốt hơn hẳn khi layout tay, và bạn không phải dùng Chrome.
  2. **Sinh thumbnail/preview cảnh** cho Studio Kịch Bản (mỗi scene 1 ảnh preview 720×420) — nhanh, không cần PIL.
  3. Nếu chuyển Remotion, Satori vẫn hữu ích để render "still" phụ trợ.
- **Chi phí/rủi ro:** cần Node sidecar nhỏ (1 file script + font buffer); subset CSS (không phải CSS đầy đủ); **bắt buộc nạp font dạng ArrayBuffer** — test kỹ font tiếng Việt (Inter/Roboto hỗ trợ tốt dấu); MPL-2.0 (dùng dependency thoải mái, chỉ copyleft ở level file).
- **Kết luận:** 🟡 **PoC 1 ngày — tiềm năng cao**, có thể là quick win đẹp nhất trong danh sách.

#### 4. svg/svgo — SVG Optimizer ⭐22.8k · MIT · v4.1.0

- **Bản chất:** pipeline plugin làm sạch SVG: bỏ metadata editor, gộp path, rút gọn số, prefix id tránh trùng, chuyển shape... Chạy qua CLI hoặc API Node.
- **Áp dụng:** (1) chuẩn hóa SVG người dùng upload (file từ Illustrator/Figma/PowerPoint thường nặng + bẩn); (2) làm sạch icon trước khi `resvg_py` raster (Iconify đã tối ưu sẵn nên lợi ích chủ yếu ở asset ngoài); (3) nếu dùng Satori/Remotion thì SVG sạch giúp mọi thứ nhẹ hơn.
- **Kết luận:** 🟡 Bước pipeline tùy chọn, giá trị trung bình, cài đặt dễ (npm).

---

### NHÓM E — RUNTIME ANIMATION CÓ SẴN ASSET (Rive, dotLottie)

Khác các nhóm trên, đây là **nguồn asset animation dựng sẵn** — đúng thứ một engine doodle cần để đa dạng hóa hình minh họa mà không vẽ tay.

#### 10. LottieFiles/dotlottie-web ⭐919 · MIT · v0.81

- **Bản chất:** player chính chủ cho **Lottie (.json) và dotLottie (.lottie)** — nhân Rust+WASM (dotlottie-rs) + renderer **ThorVG**; 3 backend: Software (Canvas2D) / WebGL2 / WebGPU; dotLottie v2 có theming (đổi màu runtime!), state machine, audio. Wrapper React/Vue/Svelte/Solid/web component.
- **Vì sao hợp tool kịch bản:**
  - Lottie là **animation vector** — nhẹ, sắc nét mọi scale, **có sẵn tính năng "draw-on" (stroke offset) mô phỏng vẽ tay** — khớp 100% phong cách whiteboard/doodle của bạn.
  - Nguồn asset khổng lồ: LottieFiles marketplace (nhiều icon doodle/whiteboard miễn phí).
  - Với video: dùng `@remotion/lottie` (nền `lottie-web` 5.13 MIT) để render vào video frame-accurate; dotlottie-web dùng cho **preview trong dashboard**.
- **Chi phí:** chọn asset tốn thời gian lọc; cần test Lottie text tiếng Việt (hạn chế dùng text layer); không phải mọi Lottie render giống nhau giữa các engine.
- **Kết luận:** 🟡 Trial 3–5 icon draw-on → đo cảm giác so với icon Iconify tĩnh hiện tại.

#### 9. rive-app/rive-wasm ⭐1.0k · MIT (runtime)

- **Bản chất:** runtime WASM cho **Rive** — định dạng animation tương tác real-time với state machine (nhân vật phản ứng theo input). Editor Rive là SaaS (miễn phí hạn chế, trả phí tính năng cao). Runtime MIT, chạy trong browser (và `@remotion/rive` cho video).
- **Điểm chặn:** không có asset `.riv` sẵn như Lottie — bạn phải tự dựng trong editor hoặc mua marketplace. Ngoài ra Rive mạnh nhất ở tương tác (không phải video tuyến tính).
- **Áp dụng tương lai:** nếu làm "nhân vật linh vật (mascot)" cho series video hoặc animation tương tác trong app — lúc đó mới đáng.
- **Kết luận:** 🔵 Để sau; Lottie trước vì rẻ và nhiều asset hơn cho nhu cầu hiện tại.

---

### NHÓM F — STACK 3D (three.js + pmndrs family)

5 repo này là **một hệ sinh thái thống nhất**, không phải 5 lựa chọn rời: three.js (engine) → R3F (renderer React) → Drei (helper) → Rapier (physics) → react-postprocessing (hiệu ứng hình). **Áp dụng cả bộ hoặc không áp dụng gì.**

#### 13. mrdoob/three.js ⭐116.6k · MIT
#### 14. pmndrs/react-three-fiber ⭐33.0k · MIT
#### 15. pmndrs/drei ⭐10.0k · MIT
#### 16. pmndrs/react-three-rapier ⭐1.5k · MIT
#### 17. pmndrs/react-postprocessing ⭐1.4k · MIT

- **Chúng làm được gì cho video:** cảnh 3D (sản phẩm xoay, dữ liệu 3D, camera fly-through, "đồ vật rơi theo vật lý", ánh sáng điện ảnh + bloom/DOF). Remotion có gói chính chủ `@remotion/three` để render R3F **frame-accurate vào video** — đây là con đường hợp lệ duy nhất để "3D vào video" mà không tự viết engine.
- **Vì sao CHƯA nên áp dụng bây giờ:** style hiện tại của bạn là doodle/whiteboard 2D; 3D không giải bài toán nào đang tồn (P1–P5). Chi phí: học shader/3D + dựng asset + render WebGL headless nặng GPU/RAM + thời gian sản xuất mỗi cảnh lâu hơn hẳn. Rapier/postprocessing chỉ có nghĩa khi đã có cảnh 3D.
- **Khi nào mở:** khi bạn muốn một **style video thứ hai** ("3D explainer", "sản phẩm 360°", "mô phỏng vật lý giáo dục"). Khi đó: three.js + R3F + Drei là combo chuẩn; Rapier cho vật lý; postprocessing cho độ "cinematic"; và chỉ dùng qua Remotion.
- **Kết luận:** 🔵 Cả nhóm — đóng băng có chủ đích, mở khi có yêu cầu style 3D thật.

---

### NHÓM G — AUDIO & XUẤT BẢN (Howler, Mediabunny, FFmpeg)

#### 18. goldfire/howler.js ⭐25.4k · MIT · v2.2.4

- **Bản chất:** thư viện audio playback "đúng chuẩn web" đã 10+ năm: Web Audio API + fallback HTML5 Audio; **sprites** (1 file audio chứa nhiều đoạn), fade, rate, volume theo nhóm, cache, spatial. 7KB gzipped, không dependency.
- **Áp dụng cho tool kịch bản:** **nâng cấp preview trong Studio Kịch Bản** — bạn có nút "Chỉ lên kịch bản để xem trước": thay vì chơi tuần tự từng file mp3 bằng thẻ `<audio>` thô, dùng Howler để (1) phát narration từng cảnh có fade in/out chuẩn như video thành phẩm, (2) **mô phỏng ducking** (hạ BGM khi narration) ngay trên web để nghe trước khi render, (3) đếm thời lượng chính xác để hiển thị timeline cảnh.
- **Rủi ro:** thấp (lib nhỏ, ổn định). Lưu ý âm thanh trình duyệt vẫn cần thao tác người dùng để autoplay — bình thường với UI preview.
- **Kết luận:** 🟡 Quick win rẻ (nửa ngày–1 ngày), cải thiện UX rõ rệt.

#### 19. Vanilagy/mediabunny ⭐7.3k · MPL-2.0 · v1.61

- **Bản chất:** "FFmpeg cho browser" thuần TypeScript: đọc/ghi/chuyển MP4, WebM, MP3, WAV, HLS… trong trang web, hiệu năng cao, zero dependency, tree-shakable. **Remotion là sponsor vàng và tích hợp sâu** (mục 1 có skill `remotion-multimedia` dạy dùng nó).
- **Áp dụng:** chỉ thực sự cần khi bạn làm (a) editor/cắt ghép client-side trong dashboard không cần server, hoặc (b) theo Remotion 5+ (nơi Mediabunny là tầng media chính). Với pipeline hiện tại, **FFmpeg server-side vẫn tốt hơn** ở mọi bài toán bạn đang có.
- **Kết luận:** 🔵 Để dành; nằm trong "combo tương lai" nếu dashboard tiến hóa thành web editor.

#### 20. FFmpeg/FFmpeg ⭐65.3k · LGPL/GPL · máy bạn đang có bản 8.1.2-full

- **Hiện trạng dùng:** tách âm, ghép clip, NVENC H.264, burn phụ đề, mix ducking — nền tảng đang rất chắc.
- **Các "nâng cấp nhỏ, lợi ích thật" đáng xem cho tool kịch bản:**
  1. **Phụ đề `.ass` thay vì drawtext/burn thủ công:** ASS cho phép style nâng cao (karaoke `\k`, viền, bóng, auto-fit vị trí) — chuẩn cho Band C nếu chưa dùng.
  2. **`xfade` + `zoompan`:** transition giữa cảnh mượt cấp filter thay vì cắt cứng; zoompan = Ken Burns rẻ GPU hơn code tay.
  3. **`filter_complex` một lượt:** gộp bước nối cảnh + overlay bàn tay + mix audio vào 1 pass (giảm I/O, ít file tạm).
  4. **`-hwaccel cuda` cho decode** khi đọc lại video nguồn (đang decode CPU ở vài bước).
  5. **`overlay` với biểu thức `t`:** các hiệu ứng động kiểu rung/trượt có thể làm ở tầng FFmpeg thay vì sinh từng frame PIL.
- **Kết luận:** ⭐ Tiếp tục tối ưu dần; đây là "xương sống" không thay đổi kể cả khi thêm Remotion.

---

## 3. KIẾN TRÚC ĐỀ XUẤT NẾU ÁP DỤNG (HYBRID PYTHON + NODE)

Nguyên tắc: **KHÔNG viết lại pipeline. Thêm đúng 1 "Node sidecar" giống pattern `model_venv_separator` đang có**, để mọi thứ JS sống trong hộp cát riêng:

```
C:\tool v1\
├─ backend\ai\doodle_engine.py            ← GIỮ NGUYÊN (engine mặc định)
├─ backend\ai\script_api.py               ← giữ nguyên orchestration FastAPI
└─ tools\render_node\                     ← MỚI (Node sidecar, có package.json riêng)
   ├─ package.json                        ← pin version: remotion, satori, svgo, howler...
   ├─ satori\render_band.mjs              ← HTML template → SVG (Band A/C, thumbnail)
   ├─ svgo\optimize.mjs                   ← làm sạch SVG asset
   └─ remotion\ (PoC)                     ← composition doodle_v2 khi được duyệt
```

**Cách gọi:** subprocess từ Python (`node tools/render_node/satori/render_band.mjs --in props.json --out band.svg`), có timeout + parse progress như đang làm với các worker khác; output SVG được raster bằng `resvg_py` sẵn có.

**Luồng render đề xuất khi PoC Remotion đạt:**
1. Gemini sinh scenes (giữ nguyên).
2. TTS từng cảnh (giữ nguyên) → có duration thật từng cảnh.
3. Truyền `scenes.json + durations + audio paths` cho Remotion render **từng cảnh** (giới hạn RAM/VRAM peak) hoặc render cả video nếu máy chịu được.
4. FFmpeg (NVENC) ghép + ducking + burn Band C như hiện tại (hoặc dùng caption của Remotion).
5. Cơ chế chọn engine trong UI: "Doodle cổ điển (PIL)" / "Doodle nâng cao (Node)" — failover về PIL nếu Node lỗi, giống tinh thần fail-closed hiện có.

**Rủi ro vận hành cần xử lý trong sidecar:** pin `package-lock.json`, `npm ci` trong script setup máy mới (`setup_v1.ps1`), preflight check `node -v` + `npx remotion --version`, log riêng, không cho sidecar tự cập nhật mạng khi render.

---

## 4. TIÊU CHÍ NGHIỆM THU PoC REMOTION (nếu làm)

| Tiêu chí | Cách đo | Ngưỡng "đạt" gợi ý |
|---|---|---|
| Chất lượng hình | 1 cảnh đua xe/doodle so sánh cạnh nhau với `kinetic_scene_animator` | Nhìn "đẹp hơn rõ" khi đặt cạnh video hiện tại |
| Thời gian render | Cùng 1 cảnh 5s @720p30, đo wall-clock | ≤ 2× thời gian engine PIL (kỳ vọng chấp nhận được vì chất lượng cao hơn) |
| RAM/VRAM peak | Task Manager/log trong lúc render | Không OOM máy 16–32GB khi render từng cảnh |
| Công sức chuyển thể | Số ngày để có 3 scene template tái sử dụng | ≤ 3 ngày sau khi quen |
| License | Xác nhận quy mô tổ chức | Nếu >3 nhân sự vì lợi nhuận → cân nhắc chi phí Company License hoặc chỉ dùng cho nội bộ cá nhân |
| Tích hợp | Chạy được từ FastAPI subprocess + failover về PIL | Pass |

---

## 5. LỘ TRÌNH ĐỀ XUẤT (3 GIAI ĐOẠN)

### Giai đoạn A — Quick wins (0–2 ngày, rủi ro ~0)
1. **Cài 3 bộ Agent Skills** cho OpenCode/agent: remotion, gsap, motion (`npx skills add ... --agent opencode`).
2. **Howler vào trang preview** `/kich-ban`: audition từng cảnh + mô phỏng ducking.
3. **SVGO chạy thử** trên `workspace/icon_cache` + 5 SVG upload mẫu → đo dung lượng giảm.

### Giai đoạn B — PoC có đo lường (1–2 tuần)
4. **Satori PoC:** template Band A (title) + thumbnail cảnh → so sánh với PIL. Nếu đạt: chuyển dần Band A/C sang Satori+resvg.
5. **Remotion PoC 1 cảnh** theo tiêu chí mục 4 → quyết định đầu tư hay dừng (dừng cũng là kết quả tốt: tiết kiệm hàng tuần).
6. **Lottie PoC:** 3–5 icon draw-on, preview trong dashboard bằng dotlottie-web, thử render vào video qua đường Remotion PoC (hoặc lottie-web + chụp frame).

### Giai đoạn C — Dài hạn (chỉ sau khi PoC thắng)
7. Engine "Doodle nâng cao" song song, chọn được trong UI, failover về PIL.
8. Style thứ hai: 3D explainer (three.js + R3F + Drei + postprocessing; Rapier nếu cần vật lý) — chỉ khi có brief thật.
9. Nếu dashboard tiến hóa thành web editor client-side: Mediabunny + Mediabunny-based export.

---

## 6. CẠM BẪY & CẢNH BÁO (ĐỌC TRƯỚC KHI QUYẾT)

1. **Remotion license >3 nhân sự:** kiểm tra quy mô tổ chức trước khi đưa vào production; Remotion 5.0 sắp đổi license — đọc lại khi nâng.
2. **Motion Canvas gần như đình trệ** — tuyệt đối không làm production phụ thuộc nó.
3. **Đừng rewrite toàn pipeline sang JS** — pipeline Python/GPU hiện tại là tài sản (Whisper, RoFormer, OCR, TTS, NVENC). JS chỉ nên là lớp "vẽ" thay thế/mở rộng.
4. **Test tiếng Việt có dấu ở MỌI lib mới** (Satori cần font ArrayBuffer đúng; Lottie hạn chế text layer; troika text trong drei cần font riêng) — dấu tiếng Việt là nơi các lib phương Tây hay "vỡ chữ".
5. **RAM của Chrome headless (Remotion):** render từng cảnh, đóng tiến trình giữa các cảnh, theo dõi peak — đừng render cả video dài 1 lượt.
6. **Node sidecar phải pin version + offline được** — máy mới chạy `setup_v1.ps1` phải cài sẵn; đừng dựa vào mạng lúc runtime.
7. **3D stack = dự án con thật sự** (asset + ánh sáng + render choreography), không phải "thêm vài component".
8. **Rive cần asset `.riv`** — không có sẵn như Lottie; đừng chọn Rive vì "nghe hiện đại hơn".
9. **GSAP trong video phải chạy theo frame** (`seek` theo `useCurrentFrame`) — dùng kiểu `requestAnimationFrame` tự do sẽ ra video giật/sai.
10. **MPL-2.0 (Satori, Mediabunny)**: dùng làm dependency thoải mái; chỉ lưu ý nghĩa copyleft ở level file nếu bạn sửa và phát hành chính lib đó.

---

## 7. KẾT LUẬN

- Trong 20 repo, **chỉ ~6–8 cái có giá trị trực tiếp** cho tool kịch bản ở thời điểm hiện tại; phần còn lại là nền tảng cho giai đoạn sau hoặc để theo dõi.
- **Ba hướng áp dụng thật sự đáng tiền:**
  1. **Agent Skills (GSAP + Remotion + Motion)** — cải thiện cách AI của bạn viết code animation/video; chi phí gần 0, làm ngay.
  2. **Satori (+ resvg_py sẵn có)** — nâng cấp chữ/banner/thumbnail bằng HTML/CSS thay vì PIL; quick win lớn nhất về chất lượng hình mà không đổi kiến trúc.
  3. **Remotion (PoC có tiêu chí)** — con đường duy nhất để đưa engine doodle lên chuẩn design-grade; quyết định bằng đo lường, không bằng cảm hứng.
- **Các quyết định "không làm" cũng quan trọng:** không đụng Motion Canvas production, không mở 3D stack bây giờ, không thay FFmpeg/backend, không ôm song song GSAP + anime.js + Motion cho cùng một việc.

*Hết báo cáo — nguồn: README/API GitHub/npm registry tại 2026-10-18 + mã nguồn `C:\tool v1`.*
