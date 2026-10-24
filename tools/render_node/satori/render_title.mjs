/**
 * Satori Band A Renderer — Studio Kịch Bản (Tool V1)
 * ====================================================
 * Nhận JSON qua stdin (hoặc --in <file>), trả về SVG qua stdout.
 *
 * Input JSON:
 * {
 *   "title": "TẠI SAO MÈO LẠI THÍCH ĐUA XE?",     // bắt buộc
 *   "keywordAccent": "ĐUA XE",                      // tô màu nhấn (tuỳ chọn)
 *   "accentColor": "#FF1E1E",                       // màu nhấn (mặc định đen)
 *   "badgeText": "★ KHÁM PHÁ BÍ MẬT",              // nhãn chip trên cùng
 *   "width": 720, "height": 420,                    // kích thước khung SVG
 *   "titleFontSize": 48,                            // ép cỡ chữ (tuỳ chọn)
 *   "fonts": { "bold": "...arialbd.ttf", "regular": "...", "symbol": "...seguisym.ttf" }
 * }
 *
 * Dùng:  node render_title.mjs < props.json > band_a.svg
 *        node render_title.mjs --in props.json > band_a.svg
 */

import fs from "node:fs";
import satori from "satori";

// ---------------------------------------------------------------------------
// 1. Đọc đầu vào
// ---------------------------------------------------------------------------
function readAllStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    process.stdin.on("error", reject);
  });
}

const inIdx = process.argv.indexOf("--in");
const rawFromFile = inIdx !== -1 && process.argv[inIdx + 1]
  ? fs.readFileSync(process.argv[inIdx + 1], "utf-8")
  : null;
const raw = rawFromFile ?? (await readAllStdin());

if (!raw || !raw.trim()) {
  console.error("Không có JSON đầu vào");
  process.exit(2);
}

let props;
try {
  props = JSON.parse(raw);
} catch (e) {
  console.error("JSON đầu vào không hợp lệ: " + e.message);
  process.exit(2);
}

const width = Number(props.width) || 720;
const height = Number(props.height) || 420;
const title = String(props.title ?? "").trim();
const keywordAccent = String(props.keywordAccent ?? "").trim();
const accentColor = String(props.accentColor ?? "#000000");
const badgeText = String(props.badgeText ?? "★ KHÁM PHÁ BÍ MẬT");
const fonts = props.fonts ?? {};

// ---------------------------------------------------------------------------
// 2. Nạp font (bắt buộc với Satori). BandExtra đóng vai trò fallback ký tự đặc biệt (★...)
// ---------------------------------------------------------------------------
const fontEntries = [];
function addFont(filePath, name, weight) {
  if (filePath && fs.existsSync(filePath)) {
    fontEntries.push({ name, data: fs.readFileSync(filePath), weight, style: "normal" });
    return true;
  }
  return false;
}
const hasBold = addFont(fonts.bold, "BandFont", 700);
const hasRegular = addFont(fonts.regular, "BandFont", 400);
if (!hasBold && !hasRegular) {
  console.error("Không tìm thấy file font hợp lệ nào (fonts.bold / fonts.regular)");
  process.exit(3);
}
addFont(fonts.symbol, "BandExtra", 400);

// ---------------------------------------------------------------------------
// 3. Auto-fit cỡ chữ tiêu đề (heuristic: chữ in hoa có dấu, trung bình ~0.60 * fontSize/char)
// ---------------------------------------------------------------------------
const usableWidth = width - 96;
function estimateLines(text, fontSize) {
  const charsPerLine = Math.max(8, Math.floor(usableWidth / (fontSize * 0.60)));
  return Math.max(1, Math.ceil(text.length / charsPerLine));
}

let titleSize = Number(props.titleFontSize) || 0;
if (!titleSize) {
  titleSize = 36;
  for (const candidate of [52, 48, 44, 40, 36]) {
    titleSize = candidate;
    if (estimateLines(title, candidate) <= 3) break;
  }
}

// ---------------------------------------------------------------------------
// 4. Tách cụm từ khóa để tô màu nhấn (giữ nguyên văn bản, chỉ đổi màu)
// ---------------------------------------------------------------------------
function buildRuns(text, keyword, color) {
  if (!keyword) return null;
  const idx = text.toLowerCase().indexOf(keyword.toLowerCase());
  if (idx === -1) return null;
  const runs = [];
  if (idx > 0) runs.push({ text: text.slice(0, idx), color: null });
  runs.push({ text: text.slice(idx, idx + keyword.length), color });
  const rest = text.slice(idx + keyword.length);
  if (rest) runs.push({ text: rest, color: null });
  return runs;
}
const titleRuns = buildRuns(title, keywordAccent, accentColor);

// ---------------------------------------------------------------------------
// 5. Dựng cây phần tử (subset HTML/CSS mà Satori hỗ trợ: flexbox, text, border...)
// ---------------------------------------------------------------------------
function buildTree(mode) {
  const titleChildren = titleRuns && mode === "runs"
    ? titleRuns.map((run) => run.color
      ? { type: "span", props: { style: { color: run.color }, children: run.text } }
      : run.text)
    : title;

  const titleBox = {
    type: "div",
    props: {
      style: {
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        flexGrow: 1,
        width: "100%",
      },
      children: {
        type: "div",
        props: {
          style: {
            ...(Array.isArray(titleChildren) ? { display: "flex", flexWrap: "wrap", justifyContent: "center", whiteSpace: "pre-wrap" } : {}),
            textAlign: "center",
            width: "100%",
            fontSize: titleSize,
            fontWeight: 700,
            lineHeight: 1.16,
            letterSpacing: -0.4,
            color: "#000000",
          },
          children: titleChildren,
        },
      },
    },
  };

  const badge = {
    type: "div",
    props: {
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: "#F5F5F5",
        border: "2px solid #000000",
        borderRadius: 10,
        padding: "5px 18px",
        fontSize: 19,
        fontWeight: 700,
        letterSpacing: 1.2,
        color: "#000000",
        flexShrink: 0,
      },
      children: badgeText,
    },
  };

  return {
    type: "div",
    props: {
      style: {
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        width: "100%",
        height: "100%",
        boxSizing: "border-box",
        backgroundColor: "#FFFFFF",
        paddingTop: 24,
        paddingBottom: 26,
        paddingLeft: 48,
        paddingRight: 48,
        rowGap: 10,
        fontFamily: "BandFont",
      },
      children: [badge, titleBox],
    },
  };
}

// ---------------------------------------------------------------------------
// 6. Render — thử chế độ nhiều "run" (tô màu nhấn) trước, lỗi thì lùi về text thuần
// ---------------------------------------------------------------------------
let svg;
try {
  svg = await satori(buildTree(titleRuns ? "runs" : "plain"), {
    width,
    height,
    fonts: fontEntries,
  });
} catch (err) {
  if (titleRuns) {
    console.error("Chế độ tô màu nhấn thất bại (" + err.message + "), lùi về text thuần.");
    svg = await satori(buildTree("plain"), { width, height, fonts: fontEntries });
  } else {
    console.error("Satori render thất bại: " + err.message);
    process.exit(4);
  }
}

process.stdout.write(svg);
