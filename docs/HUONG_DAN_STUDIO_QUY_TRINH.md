# HƯỚNG DẪN SỬ DỤNG STUDIO QUY TRÌNH (TIMELINE CAPCUT)

Cập nhật: 2026-10-24 · Truy cập: `http://127.0.0.1:8088/quy-trinh` · Nhấn phím `?` trong trang để xem bảng phím tắt.

## 1. Chọn video và làm quen timeline

1. Bấm vào một video trong **Thư Viện Video** (bên trái). Studio tải bản **final** (đã việt hóa) hoặc **phôi** tùy nút đang chọn trên đầu trang.
2. Timeline nằm dưới trình phát, gồm 5 track:
   - ⏱️ **Thước đo** — có sẵn thanh zoom (−/slider/+/⤢) và nút snap 🧲.
   - 🇨🇳 **Phụ đề Trung** và 💬 **Phụ đề AI** — hiển thị từng câu, bấm để nhảy tới.
   - 🎬 **Video chính** — các đoạn cắt của bạn.
   - 📝 **Chữ & Logo** và 🎵 **Nhạc & SFX** — track thêm hiệu ứng (P3/P5).
   - 🎵 **Âm thanh mix** — waveform thật của audio video (P5).

## 2. Cắt ghép cơ bản

- **Cắt đoạn**: di chuyển playhead vào giữa đoạn (cách mép ≥ 0.15s) rồi bấm `C` hoặc nút ✂️.
- **Trim**: kéo mép vàng trái/phải của card (có xem trước khung hình). `Q` = trim đầu tới playhead, `W` = trim cuối.
- **Đổi thứ tự**: kéo thả card vào trước/sau đoạn khác; 🧲 trên card đưa lên đầu làm Hook.
- **Xóa**: `Del` xóa rip-nhịp (đoạn sau dồn về trái). `Shift+Del` xóa để trống khoảng gap.
- **Undo/Redo**: `Ctrl+Z` / `Ctrl+Y` — 40 bước, khôi phục cả selection.
- **Zoom**: `Ctrl` + lăn chuột (neo theo con trỏ), slider trong thước đo, hoặc `+` / `-`. Dải 10%–800%.

## 3. Chọn nhiều đoạn

- `Ctrl`+bấm: bật/tắt từng đoạn; `Shift`+bấm: chọn dải; **kéo chuột trên vùng trống** track video: quét chọn (rubber band).
- `Ctrl+A` chọn tất cả, `Esc` bỏ chọn. `Del` xóa cả nhóm (rip-nhịp).

## 4. Thuộc tính clip (tab ⚙️ Thuộc Tính Clip)

Bấm vào một đoạn trên timeline rồi mở tab bên phải:

- ⚡ **Tốc độ** 0.25x–4x (kèm giữ cao độ giọng) — timeline và phụ đề tự canh lại.
- 🔊 **Âm lượng** + nghe thử theo đoạn đang chọn.
- **Fade vào/ra** (hình + tiếng).
- 🖼️ **Cắt gọt khung hình**: bấm "✂️ Cắt gọt trên preview" rồi kéo khung xanh trên video.
- **Xoay 0/90/180/270°, lật ngang/dọc** (xem trước ngay trên preview).
- 🔄 **Chuyển cảnh sang đoạn kế**: fade / fade trắng-đen / dissolve / trượt / wipe / circle — chỉ áp dụng khi 2 đoạn liền kề; timeline tự co ngắn, sub/nhạc giữ đúng nội dung.
- 🎨 **Màu & hiệu ứng**: độ sáng, tương phản, bão hòa, nhiệt màu, làm mờ, vignette (preview CSS, render bằng FFmpeg).
- ◀️▶️ **Phát ngược** (đoạn ≤ 60s), 🧊 **đóng băng khung đầu đoạn**, 🎧 **tách âm thanh giữ tốc độ gốc**.

## 5. Chữ & Logo (track 📝)

- Nút **＋A** thêm chữ tại playhead, **＋🖼️** thêm logo/ảnh (≤ 3MB).
- **Kéo trực tiếp trên video** để đặt vị trí; kéo trên timeline để đổi thời điểm; kéo mép card để đổi thời lượng.
- Tab **📝 Chữ & Logo**: nội dung, 4 preset kiểu chữ (Vàng viền đen / Trắng khung đen / Đen khung trắng / Xanh ngọc neon), cỡ chữ, màu, viền, nền mờ, hiệu ứng vào/ra (Fade / Trượt lên / Pop).

## 6. Nhạc & SFX (track 🎵)

- **Kéo thả file nhạc** (mp3/wav/ogg/m4a, ≤ 8MB) thẳng vào track, hoặc nút ＋🎵.
- Tab chỉnh: âm lượng, fade vào/ra, **🦆 Ducking** — tự hạ nhạc khi giọng trong video nổi lên.
- Waveform thật hiển thị trên track "Âm thanh mix", tự co giãn theo zoom và timeline đã ghép.

## 7. Xuất video

- Chọn **khung xuất** (▦ 9:16 / ▣ 1:1 / ▭ 16:9) cạnh nút Áp dụng.
- Bấm **💾 ÁP DỤNG & GHÉP VIDEO** — render chạy **nền GPU NVENC**, nút hiển thị tiến độ thật (VD `⏳ 45% — Đang render FFmpeg`). **Bấm lần nữa để huỷ**.
- Kết quả: video mới tự phát lại, phụ đề đã được căn lại theo timeline, thumbnail cập nhật.

## 8. Preview mượt

- Với bản final, Studio tự dùng **proxy 540p** cho preview (khi sẵn sàng) — xuất video vẫn dùng bản gốc full. Xem bản phôi thì luôn là file gốc.
- Khi phát nối qua các đoạn, thẻ preload tự "hâm" vùng clip kế tiếp để hạn chế giật.

## 9. Lưu tự động & Concat

- Mọi thao tác được **autosave mỗi 10 giây** vào `workspace/<tên-video>/project.json`; đóng/mở lại trình duyệt là timeline trở về như cũ.
- **🧩 Concat**: mở thư mục project của video trong app Concat (tiến trình ngoài). **📥 Nhập Concat**: nhập file xuất từ Concat vào thư viện như một video phôi mới để chạy lại pipeline.

## 10. Phím tắt (nhấn `?` để mở bảng trong trang)

| Phím | Chức năng |
|---|---|
| `Space` | Phát / dừng |
| `←` `→` | Lùi / tiến 2s (`Shift` = 0.1s) |
| `,` `.` | Lùi / tiến 1 frame |
| `J` `K` `L` | Tua lùi / dừng-phát / tốc độ 1x→2x→4x |
| `C` hoặc `S` | Cắt tại playhead |
| `Q` `W` | Trim đầu / cuối tới playhead |
| `Del` | Xóa rip-nhịp (`Shift+Del` = để trống) |
| `Ctrl+Z` / `Ctrl+Y` | Hoàn tác / làm lại |
| `Ctrl+C` `Ctrl+V` `Ctrl+D` | Copy / dán / nhân bản |
| `Ctrl+A` | Chọn tất cả |
| `N` | Bật/tắt snap |
| `+` `-` | Zoom timeline |
| `?` | Bảng phím tắt này |

## 11. Tự kiểm tra nhanh (DevTools)

Mở Console (F12) và gõ `timelineSelfTest()` — chạy qua các thao tác chính (reset, split, transition, speed, gap, seek, snap, ripple delete) và báo `X/X đạt`. Trạng thái timeline của bạn được khôi phục nguyên vẹn sau khi chạy.

---

* tài liệu này đi kèm `docs/CHANGELOG_CAPCUT_UPGRADE.md` (lịch sử nâng cấp chi tiết theo commit).
