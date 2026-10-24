import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { evolvePath } from "@remotion/paths";
import { Lottie } from "@remotion/lottie";
import sparkleData from "./lottie/sparkle.json";

/**
 * DoodleScenePoC — 1 phân cảnh chuẩn 3 dải 720x1280 (Banner / Doodle / Phụ đề).
 * Engine "Remotion Draw-On" của Studio Kịch Bản:
 *  - Band A: typography HTML/CSS (badge + tiêu đề, tô màu cụm từ khóa)
 *  - Band B: bộ art theo actionType (race / chart_bar / chart_pie / brain_think / showcase)
 *            + hiệu ứng vẽ nét (draw-on), camera motion, Lottie sparkle (looping)
 *  - Band C: hộp phụ đề viền bo góc
 * Toàn bộ chuyển động điều khiển bằng frame (deterministic), sẵn sàng render.
 */

export type DoodleSceneProps = {
  title: string;
  keywordAccent?: string;
  accentColor?: string;
  badgeText?: string;
  narration: string;
  doodleLabel?: string;
  durationSec?: number;
  fps?: number;
  /** race | chart_bar | chart_pie | brain_think | showcase (do backend phát hiện từ ngữ nghĩa cảnh) */
  actionType?: string;
  /** zoom_in | zoom_out | pan_right | pan_left | shake | pulse */
  cameraMotion?: string;
  /** Số liệu cho biểu đồ (0-100 nếu là %) */
  numbers?: number[];
  /** Cưỡng bức bật/tắt sparkle Lottie (mặc định: bật khi showcase/brain_think) */
  showSparkles?: boolean;
};

const INK = "#111111";
const BG = "#FFFFFF";
const CHIP_BG = "#F5F5F5";

// ===========================================================================
// Helpers
// ===========================================================================
type PathSpec = { d: string; w?: number };

function drawWindow(i: number, count: number, durationInFrames: number, startFrac = 0.02, endFrac = 0.7) {
  const start = durationInFrames * startFrac;
  const end = durationInFrames * endFrac;
  const per = (end - start) / Math.max(1, count);
  const s = start + i * per * 0.75;
  return { start: s, end: Math.min(s + per * 1.6, end + per * 0.4) };
}

function cameraTransform(frame: number, durationInFrames: number, motion?: string): string {
  const t = durationInFrames > 1 ? frame / (durationInFrames - 1) : 0;
  const m = (motion || "zoom_in").toLowerCase();
  let scale = 1;
  let dx = 0;
  let dy = 0;
  if (m === "pan_right" || m === "pan_left") {
    dx = (m === "pan_left" ? 1 : -1) * (t * 40 - 20);
  } else if (m === "shake") {
    dx = Math.sin(frame * 1.1) * 5;
    dy = Math.cos(frame * 1.4) * 3;
  } else if (m === "pulse") {
    scale = 1 + 0.035 * Math.sin(frame / 6);
  } else if (m === "zoom_out") {
    scale = 1 - 0.09 * t;
  } else {
    scale = 1 + 0.09 * t; // zoom_in mặc định
  }
  return `translate(${360 + dx} ${210 + dy}) scale(${scale}) translate(-360 -210)`;
}

function polar(cx: number, cy: number, r: number, deg: number) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function wedgePath(cx: number, cy: number, r: number, a0: number, a1: number) {
  const s = polar(cx, cy, r, a0);
  const e = polar(cx, cy, r, a1);
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M ${cx} ${cy} L ${s.x} ${s.y} A ${r} ${r} 0 ${large} 1 ${e.x} ${e.y} Z`;
}

function DrawPath({
  d,
  start,
  end,
  strokeWidth = 7,
  color = INK,
}: {
  d: string;
  start: number;
  end: number;
  strokeWidth?: number;
  color?: string;
}) {
  const frame = useCurrentFrame();
  const progress = interpolate(frame, [start, end], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const { strokeDasharray, strokeDashoffset } = evolvePath(progress, d);
  return (
    <path
      d={d}
      stroke={color}
      strokeWidth={strokeWidth}
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeDasharray={strokeDasharray}
      strokeDashoffset={strokeDashoffset}
    />
  );
}

// ===========================================================================
// Art: RACE — mèo lái xe đua
// ===========================================================================
const RACE_PATHS: PathSpec[] = [
  { d: "M 56 396 L 664 396", w: 9 },
  { d: "M 180 180 A 70 70 0 1 1 320 180 A 70 70 0 1 1 180 180", w: 8 },
  { d: "M 212 122 L 216 66 L 254 108", w: 8 },
  { d: "M 286 108 L 324 66 L 328 122", w: 8 },
  { d: "M 216 176 Q 227 164 238 176", w: 7 },
  { d: "M 262 176 Q 273 164 284 176", w: 7 },
  { d: "M 243 196 L 257 196 L 250 205 Z", w: 6 },
  { d: "M 250 205 Q 238 217 226 209", w: 6 },
  { d: "M 250 205 Q 262 217 274 209", w: 6 },
  { d: "M 196 190 L 148 182", w: 5 },
  { d: "M 196 204 L 148 204", w: 5 },
  { d: "M 304 190 L 352 182", w: 5 },
  { d: "M 304 204 L 352 204", w: 5 },
  { d: "M 402 262 L 430 220 L 542 220 L 566 262", w: 8 },
  { d: "M 454 222 L 454 262", w: 6 },
  { d: "M 362 316 L 362 286 Q 362 262 388 262 L 582 262 Q 608 262 608 286 L 608 316 Z", w: 8 },
  { d: "M 398 320 A 23 23 0 1 1 444 320 A 23 23 0 1 1 398 320", w: 8 },
  { d: "M 538 320 A 23 23 0 1 1 584 320 A 23 23 0 1 1 538 320", w: 8 },
  { d: "M 44 290 L 150 290", w: 6 },
  { d: "M 74 314 L 196 314", w: 6 },
  { d: "M 44 338 L 132 338", w: 6 },
];

const RaceArt: React.FC<{ durationInFrames: number }> = ({ durationInFrames }) => (
  <g>
    {RACE_PATHS.map((p, i) => {
      const win = drawWindow(i, RACE_PATHS.length, durationInFrames);
      return <DrawPath key={i} d={p.d} start={win.start} end={win.end} strokeWidth={p.w ?? 7} />;
    })}
  </g>
);

// ===========================================================================
// Art: CHART BAR — biểu đồ cột tăng trưởng + mũi tên xu hướng
// ===========================================================================
const BarArt: React.FC<{ durationInFrames: number; numbers?: number[] }> = ({ durationInFrames, numbers }) => {
  const frame = useCurrentFrame();
  const raw = numbers && numbers.length >= 2 ? numbers.slice(0, 5) : [20, 45, 70, 95];
  const maxRaw = Math.max(...raw, 1);
  const percentMode = maxRaw <= 100;
  const fracs = raw.map((v) => Math.min(1, v / (percentMode ? 100 : maxRaw)));
  const chartTop = 80;
  const chartBottom = 350;
  const chartLeft = 120;
  const chartRight = 648;
  const chartH = chartBottom - chartTop;
  const n = raw.length;
  const slot = (chartRight - chartLeft) / n;
  const barW = Math.min(86, slot * 0.6);

  const axisWin = drawWindow(0, 1, durationInFrames, 0.02, 0.16);

  const x0 = chartLeft + slot / 2;
  const xN = chartLeft + (n - 1) * slot + slot / 2;
  const y0 = chartBottom - chartH * fracs[0] - 40;
  const yN = chartBottom - chartH * fracs[n - 1] - 40;
  const trendStart = durationInFrames * 0.66;
  const trendEnd = durationInFrames * 0.82;

  return (
    <g>
      <DrawPath d={`M ${chartLeft} ${chartTop - 4} L ${chartLeft} ${chartBottom}`} start={axisWin.start} end={axisWin.end} strokeWidth={8} />
      <DrawPath d={`M ${chartLeft} ${chartBottom} L ${chartRight + 4} ${chartBottom}`} start={axisWin.start} end={axisWin.end} strokeWidth={8} />
      {raw.map((v, i) => {
        const start = durationInFrames * (0.17 + i * 0.075);
        const p = interpolate(frame, [start, start + durationInFrames * 0.15], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
        const h = chartH * fracs[i] * p;
        const x = chartLeft + i * slot + (slot - barW) / 2;
        const y = chartBottom - h;
        const label = percentMode ? `${Math.round(v)}%` : `${v}`;
        const isLast = i === raw.length - 1;
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={Math.max(0, h)}
              rx={8}
              fill={isLast ? INK : "#D9D9D9"}
              stroke={INK}
              strokeWidth={5}
            />
            {p > 0.96 ? (
              <text
                x={x + barW / 2}
                y={y - 14}
                textAnchor="middle"
                fontFamily="Arial, Helvetica, sans-serif"
                fontSize={30}
                fontWeight={700}
                fill={INK}
              >
                {label}
              </text>
            ) : null}
          </g>
        );
      })}
      {n >= 2 ? (
        <g>
          <DrawPath d={`M ${x0} ${y0} L ${xN} ${yN}`} start={trendStart} end={trendEnd} strokeWidth={7} color="#FF1E1E" />
          <DrawPath
            d={`M ${xN - 20} ${yN - 16} L ${xN} ${yN} L ${xN - 4} ${yN - 24}`}
            start={trendEnd}
            end={trendEnd + durationInFrames * 0.06}
            strokeWidth={7}
            color="#FF1E1E"
          />
        </g>
      ) : null}
    </g>
  );
};

// ===========================================================================
// Art: CHART PIE — biểu đồ tròn với các phần xuất hiện tuần tự
// ===========================================================================
const PIE_COLORS = ["#111111", "#7C7C7C", "#C9C9C9", "#EFEFEF"];

const PieArt: React.FC<{ durationInFrames: number; numbers?: number[] }> = ({ durationInFrames, numbers }) => {
  const frame = useCurrentFrame();
  const vals = numbers && numbers.length >= 2 ? numbers.slice(0, 4) : [50, 30, 20];
  const total = vals.reduce((a, b) => a + b, 0) || 100;
  const cx = 215;
  const cy = 230;
  const r = 132;

  const circleD = `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx} ${cy + r} A ${r} ${r} 0 1 1 ${cx} ${cy - r}`;
  const circleWin = drawWindow(0, 1, durationInFrames, 0.02, 0.26);

  let acc = 0;
  const wedges = vals.map((v, i) => {
    const a0 = (acc / total) * 360;
    acc += v;
    const a1 = (acc / total) * 360;
    const start = durationInFrames * (0.24 + i * 0.09);
    const p = interpolate(frame, [start, start + durationInFrames * 0.13], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    return { v, a0, a1, i, p, pct: Math.round(((a1 - a0) / 360) * 100) };
  });

  return (
    <g>
      {wedges.map((w) => {
        const mid = (w.a0 + w.a1) / 2;
        const lp = polar(cx, cy, r * 0.62, mid);
        const sc = 0.55 + 0.45 * w.p;
        return (
          <g key={w.i} opacity={w.p}>
            <g transform={`translate(${cx} ${cy}) scale(${sc}) translate(${-cx} ${-cy})`}>
              <path
                d={wedgePath(cx, cy, r, w.a0, w.a1)}
                fill={PIE_COLORS[w.i % PIE_COLORS.length]}
                stroke={INK}
                strokeWidth={5}
                strokeLinejoin="round"
              />
            </g>
            {w.p > 0.9 ? (
              <text
                x={lp.x}
                y={lp.y + 10}
                textAnchor="middle"
                fontFamily="Arial, Helvetica, sans-serif"
                fontSize={30}
                fontWeight={700}
                fill={w.i === 0 ? "#FFFFFF" : INK}
              >
                {w.pct}%
              </text>
            ) : null}
          </g>
        );
      })}
      <DrawPath d={circleD} start={circleWin.start} end={circleWin.end} strokeWidth={8} />
    </g>
  );
};

// ===========================================================================
// Art: BRAIN — bộ não + tia sáng ý tưởng (pulse)
// ===========================================================================
const BRAIN_PATHS: PathSpec[] = [
  {
    d: "M 208 246 C 172 240 152 206 166 176 C 146 148 166 112 200 108 C 212 80 254 68 280 84 C 310 66 348 84 352 116 C 382 128 388 164 368 188 C 380 218 358 250 326 250 C 314 268 280 278 256 264 C 238 274 214 268 208 246 Z",
    w: 9,
  },
  { d: "M 214 152 C 234 136 260 140 266 160", w: 7 },
  { d: "M 280 122 C 300 112 322 124 320 144", w: 7 },
  { d: "M 300 192 C 320 184 340 196 336 216", w: 7 },
  { d: "M 236 216 C 250 206 270 208 278 222", w: 6 },
];

const BRAIN_BULB_PATHS: PathSpec[] = [
  { d: "M 486 122 A 46 46 0 1 1 486 214 A 46 46 0 1 1 486 122", w: 7 },
  { d: "M 468 228 L 504 228", w: 6 },
  { d: "M 472 242 L 500 242", w: 6 },
  { d: "M 472 176 Q 486 158 500 176", w: 5 },
  { d: "M 486 92 L 486 74", w: 5 },
  { d: "M 540 168 L 558 168", w: 5 },
  { d: "M 432 168 L 414 168", w: 5 },
];

const BrainArt: React.FC<{ durationInFrames: number }> = ({ durationInFrames }) => {
  const frame = useCurrentFrame();
  const pulse = 1 + 0.04 * Math.sin(frame / 5);
  const total = BRAIN_PATHS.length + BRAIN_BULB_PATHS.length;
  let idx = 0;
  return (
    <g>
      {BRAIN_PATHS.map((p) => {
        const win = drawWindow(idx++, total, durationInFrames);
        return <DrawPath key={p.d} d={p.d} start={win.start} end={win.end} strokeWidth={p.w ?? 7} />;
      })}
      <g transform={`translate(486 168) scale(${pulse}) translate(-486 -168)`}>
        {BRAIN_BULB_PATHS.map((p) => {
          const win = drawWindow(idx++, total, durationInFrames);
          return <DrawPath key={p.d} d={p.d} start={win.start} end={win.end} strokeWidth={p.w ?? 6} />;
        })}
      </g>
    </g>
  );
};

// ===========================================================================
// Art: SHOWCASE — bóng đèn ý tưởng trung tâm (mặc định)
// ===========================================================================
const BULB_PATHS: PathSpec[] = [
  { d: "M 288 196 A 74 74 0 1 1 432 196 A 74 74 0 1 1 288 196", w: 9 },
  { d: "M 330 258 L 390 258", w: 7 },
  { d: "M 336 276 L 384 276", w: 7 },
  { d: "M 344 206 L 352 192 L 360 206 L 368 192 L 376 206", w: 6 },
  { d: "M 360 92 L 360 58", w: 6 },
  { d: "M 262 196 L 226 196", w: 6 },
  { d: "M 458 196 L 494 196", w: 6 },
  { d: "M 292 128 L 264 100", w: 6 },
  { d: "M 428 128 L 456 100", w: 6 },
  { d: "M 292 264 L 266 290", w: 6 },
  { d: "M 428 264 L 454 290", w: 6 },
];

const BulbArt: React.FC<{ durationInFrames: number }> = ({ durationInFrames }) => {
  const frame = useCurrentFrame();
  const pulse = 1 + 0.03 * Math.sin(frame / 6);
  return (
    <g transform={`translate(360 196) scale(${pulse}) translate(-360 -196)`}>
      {BULB_PATHS.map((p, i) => {
        const win = drawWindow(i, BULB_PATHS.length, durationInFrames);
        return <DrawPath key={i} d={p.d} start={win.start} end={win.end} strokeWidth={p.w ?? 7} />;
      })}
    </g>
  );
};

// ===========================================================================
// Sparkle Lottie (looping) — trang trí cho showcase / brain_think
// ===========================================================================
const Sparkle: React.FC<{
  size?: number;
  delayFrames: number;
  top?: number | string;
  left?: number | string;
  right?: number | string;
  bottom?: number | string;
}> = ({ size = 120, delayFrames, top, left, right, bottom }) => {
  const frame = useCurrentFrame();
  const appear = interpolate(frame, [delayFrames, delayFrames + 10], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  if (appear <= 0) return null;
  return (
    <div
      style={{
        position: "absolute",
        top,
        left,
        right,
        bottom,
        width: size,
        height: size,
        opacity: appear,
      }}
    >
      <Lottie animationData={sparkleData as unknown as Parameters<typeof Lottie>[0]["animationData"]} loop style={{ width: "100%", height: "100%" }} />
    </div>
  );
};

// ===========================================================================
// Tách tiêu đề để tô màu cụm từ khóa (giữ nguyên văn bản)
// ===========================================================================
function splitTitle(title: string, keyword?: string, accentColor = "#FF1E1E") {
  if (!keyword) return [title];
  const idx = title.toLowerCase().indexOf(keyword.toLowerCase());
  if (idx === -1) return [title];
  const parts: Array<string | React.ReactElement> = [];
  if (idx > 0) parts.push(title.slice(0, idx));
  parts.push(
    <span key="accent" style={{ color: accentColor }}>
      {title.slice(idx, idx + keyword.length)}
    </span>,
  );
  const rest = title.slice(idx + keyword.length);
  if (rest) parts.push(rest);
  return parts;
}

// ===========================================================================
// Composition chính
// ===========================================================================
export const DoodleScenePoC: React.FC<DoodleSceneProps> = ({
  title,
  keywordAccent,
  accentColor = "#FF1E1E",
  badgeText = "★ KHÁM PHÁ BÍ MẬT",
  narration,
  doodleLabel,
  actionType = "showcase",
  cameraMotion = "zoom_in",
  numbers,
  showSparkles,
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  const action = (actionType || "showcase").toLowerCase();
  const cam = cameraTransform(frame, durationInFrames, cameraMotion);

  const sparkles = showSparkles ?? ["showcase", "brain_think"].includes(action);
  const sparkleDelay = Math.round(durationInFrames * 0.4);

  const chipAppear = durationInFrames * 0.42;
  const chipOpacity = interpolate(frame, [chipAppear, chipAppear + 8], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const chipPulse = 1 + 0.05 * Math.sin(Math.max(0, frame - chipAppear) / 7);

  let art: React.ReactElement;
  if (action === "race") {
    art = <RaceArt durationInFrames={durationInFrames} />;
  } else if (action === "chart_bar") {
    art = <BarArt durationInFrames={durationInFrames} numbers={numbers} />;
  } else if (action === "chart_pie") {
    art = <PieArt durationInFrames={durationInFrames} numbers={numbers} />;
  } else if (action === "brain_think") {
    art = <BrainArt durationInFrames={durationInFrames} />;
  } else {
    art = <BulbArt durationInFrames={durationInFrames} />;
  }

  return (
    <AbsoluteFill style={{ backgroundColor: BG, fontFamily: "Arial, Helvetica, sans-serif" }}>
      {/* ===== BAND A: BANNER TIÊU ĐỀ ===== */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: 720,
          height: 420,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 18,
          padding: "24px 48px",
          boxSizing: "border-box",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            backgroundColor: CHIP_BG,
            border: `3px solid ${INK}`,
            borderRadius: 10,
            padding: "6px 18px",
            fontSize: 20,
            fontWeight: 700,
            letterSpacing: 1.2,
            color: INK,
          }}
        >
          {badgeText}
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            textAlign: "center",
            fontSize: 50,
            fontWeight: 700,
            lineHeight: 1.14,
            letterSpacing: -1,
            color: INK,
            whiteSpace: "pre-wrap",
          }}
        >
          {splitTitle(title, keywordAccent, accentColor)}
        </div>
      </div>

      {/* Vạch phân cách A/B */}
      <div style={{ position: "absolute", top: 418, left: 0, width: 720, height: 4, backgroundColor: INK }} />

      {/* ===== BAND B: DOODLE + CAMERA ===== */}
      <div style={{ position: "absolute", top: 420, left: 0, width: 720, height: 420, overflow: "hidden" }}>
        <svg width={720} height={420} viewBox="0 0 720 420">
          <g transform={cam}>{art}</g>
        </svg>

        {/* Sparkle Lottie trang trí (looping) */}
        {sparkles ? (
          <>
            <Sparkle size={120} delayFrames={sparkleDelay} top={6} right={18} />
            <Sparkle size={84} delayFrames={sparkleDelay + 14} bottom={8} left={16} />
          </>
        ) : null}

        {doodleLabel ? (
          <div
            style={{
              position: "absolute",
              top: 14,
              left: 0,
              width: 720,
              display: "flex",
              justifyContent: "center",
              opacity: chipOpacity,
              transform: `scale(${chipPulse})`,
            }}
          >
            <div
              style={{
                backgroundColor: INK,
                color: "#FFFFFF",
                borderRadius: 10,
                padding: "4px 16px",
                fontSize: 24,
                fontWeight: 700,
                letterSpacing: 1,
              }}
            >
              {doodleLabel}
            </div>
          </div>
        ) : null}
      </div>

      {/* Vạch phân cách B/C */}
      <div style={{ position: "absolute", top: 838, left: 0, width: 720, height: 4, backgroundColor: INK }} />

      {/* ===== BAND C: PHỤ ĐỀ ===== */}
      <div
        style={{
          position: "absolute",
          top: 842,
          left: 0,
          width: 720,
          height: 438,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "30px 44px",
          boxSizing: "border-box",
        }}
      >
        <div
          style={{
            border: `4px solid ${INK}`,
            borderRadius: 18,
            padding: "26px 34px",
            fontSize: 40,
            fontWeight: 700,
            lineHeight: 1.32,
            textAlign: "center",
            color: INK,
          }}
        >
          {narration}
        </div>
      </div>

      {/* Viền ngoài toàn khung */}
      <div style={{ position: "absolute", inset: 0, border: `5px solid ${INK}`, boxSizing: "border-box" }} />
    </AbsoluteFill>
  );
};
