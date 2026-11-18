// Kiểm thử toàn diện giao diện tối giản chuẩn CapCut & các thao tác cốt lõi trên Studio Quy Trình (/quy-trinh).
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert");

const WORKFLOW_HTML = path.join(__dirname, "../../backend/templates/workflow.html");
const TIME_CORE_JS = path.join(__dirname, "../../backend/static/js/timeline_time_core.js");
const TIMELINE_JS = path.join(__dirname, "../../backend/static/js/timeline.js");

const htmlSource = fs.readFileSync(WORKFLOW_HTML, "utf8");
const timeCoreSource = fs.readFileSync(TIME_CORE_JS, "utf8");
const timelineSource = fs.readFileSync(TIMELINE_JS, "utf8");

function extractInlineScripts(html) {
  const re = /<script(?:\s+[^>]*)?>([\s\S]*?)<\/script>/gi;
  const blocks = [];
  let m;
  while ((m = re.exec(html)) !== null) {
    if (m[1] && m[1].trim()) blocks.push(m[1]);
  }
  return blocks;
}

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add(...names) { names.forEach((n) => n && set.add(n)); },
    remove(...names) { names.forEach((n) => set.delete(n)); },
    toggle(name, force) {
      if (typeof force === "boolean") {
        if (force) set.add(name);
        else set.delete(name);
        return force;
      }
      if (set.has(name)) { set.delete(name); return false; }
      set.add(name);
      return true;
    },
    contains(name) { return set.has(name); },
    _items: set,
  };
}

function makeElement(id, opts = {}) {
  const listeners = {};
  const children = [];
  const attrs = new Map();
  let _innerHTML = "";
  const el = {
    id: id || "",
    tagName: (opts.tagName || "DIV").toUpperCase(),
    value: opts.value !== undefined ? opts.value : "",
    checked: Boolean(opts.checked),
    disabled: Boolean(opts.disabled),
    innerText: opts.innerText || "",
    textContent: opts.textContent || opts.innerText || "",
    get innerHTML() { return _innerHTML; },
    set innerHTML(v) { _innerHTML = String(v || ""); if (!_innerHTML) children.length = 0; },
    title: "",
    src: "",
    currentTime: 0,
    duration: opts.duration !== undefined ? opts.duration : 20,
    paused: opts.paused !== undefined ? opts.paused : true,
    muted: false,
    volume: 1,
    playbackRate: 1,
    defaultPlaybackRate: 1,
    offsetWidth: 320,
    clientWidth: opts.clientWidth || 600,
    clientHeight: opts.clientHeight || 240,
    scrollHeight: opts.scrollHeight || 240,
    offsetLeft: opts.offsetLeft || 0,
    buffered: { length: 0, end() { return 0; } },
    dataset: Object.assign({}, opts.dataset || {}),
    style: Object.assign({ display: opts.display || "" }, opts.style || {}),
    classList: makeClassList(opts.classes || []),
    children,
    parentElement: null,
    addEventListener(ev, fn) {
      listeners[ev] = listeners[ev] || [];
      listeners[ev].push(fn);
    },
    dispatch(ev, payload = {}) {
      (listeners[ev] || []).forEach((fn) => fn(payload));
    },
    appendChild(child) {
      if (child) child.parentElement = el;
      children.push(child);
      return child;
    },
    prepend(child) {
      if (child) child.parentElement = el;
      children.unshift(child);
      return child;
    },
    remove() {
      if (el.parentElement && el.parentElement.children) {
        const idx = el.parentElement.children.indexOf(el);
        if (idx >= 0) el.parentElement.children.splice(idx, 1);
      }
    },
    setAttribute(k, v) { attrs.set(k, String(v)); },
    getAttribute(k) { return attrs.get(k) || null; },
    removeAttribute(k) { attrs.delete(k); if (k === "src") el.src = ""; },
    hasAttribute(k) { return attrs.has(k); },
    contains(target) { return target === el || children.includes(target); },
    closest() { return null; },
    focus() {},
    blur() {},
    load() {},
    getContext() {
      return {
        clearRect() {},
        fillRect() {},
        beginPath() {},
        moveTo() {},
        lineTo() {},
        stroke() {},
        setTransform() {},
      };
    },
    play() { el.paused = false; el.dispatch("play"); return Promise.resolve(); },
    pause() { el.paused = true; el.dispatch("pause"); },
    getBoundingClientRect() {
      return { left: 0, top: 0, width: el.clientWidth || 600, height: el.clientHeight || 240 };
    },
    querySelector(sel) {
      if (sel === ".sub-color-dot" || sel.includes("sub-color-dot")) return children[0] || null;
      return null;
    },
    querySelectorAll(sel) {
      if (sel.includes("sub-color-dot") || sel.includes("utility-subtab") || sel.includes("export-pill")) {
        return children;
      }
      return [];
    },
  };
  return el;
}

function createStudioHarness(opts = {}) {
  const scripts = extractInlineScripts(htmlSource);
  assert.strictEqual(scripts.length, 3, "workflow.html phải có đúng 3 inline script blocks hợp lệ");

  const elements = new Map();
  const reg = (id, elOpts) => {
    const el = makeElement(id, elOpts);
    elements.set(id, el);
    return el;
  };

  reg("mainPlayer", { tagName: "VIDEO", duration: 20, paused: true });
  reg("bgBlurVideo", { tagName: "VIDEO", duration: 20, paused: true });
  reg("bgMotionVideo", { tagName: "VIDEO", duration: 20, paused: true });
  reg("vocalIsolationPlayer", { tagName: "AUDIO", duration: 20, paused: true });
  reg("manualTtsPlayer", { tagName: "AUDIO", duration: 20, paused: true });
  reg("voicePreviewAudio", { tagName: "AUDIO", duration: 5, paused: true });
  reg("studioCanvas", { classes: ["studio-canvas", "canvas-9-16"] });
  reg("canvasBgLayer");
  reg("bgSolid");
  reg("fgContainer");
  reg("subtitleOverlay", { display: "flex" });
  reg("subtitleInteractiveBox", { display: "inline-flex" });
  reg("subVietnameseText", { innerText: "Phụ đề mẫu" });
  reg("subOriginalText", { display: "none" });
  reg("subSamplePreviewText", { display: "none" });
  reg("subTextInputPanel", { tagName: "TEXTAREA" });
  reg("subContentModeBadge");
  reg("btnRestoreSubFloating", { display: "none" });
  reg("btnToggleSampleSubPanel");
  reg("btnPlay", { tagName: "BUTTON", innerText: "▶" });
  reg("playbackSpeedSelect", { tagName: "SELECT", value: "1" });
  reg("timecodeDisplay", { innerText: "00:00.00 / 00:00.00" });
  reg("playhead");
  reg("snapGuide");
  reg("timelineTracks", { clientWidth: 700, clientHeight: 240, scrollHeight: 260 });
  reg("timelineRuler", { offsetLeft: 0 });
  reg("rulerMarksContainer", { clientWidth: 600, offsetLeft: 220 });
  reg("subtitlesTrack");
  reg("chineseSubtitlesTrack");
  const vcTrackContent = reg("videoClipsTrackContent", { clientWidth: 600, offsetLeft: 220 });
  vcTrackContent.appendChild(reg("videoClipsContainer"));
  const txtTrackContent = reg("textOverlayTrackContent", { clientWidth: 600 });
  txtTrackContent.appendChild(reg("textOverlayTrack"));
  reg("textOverlayPreviewLayer");
  const musTrackContent = reg("musicTrackContent", { clientWidth: 600 });
  musTrackContent.appendChild(reg("musicTrack"));
  reg("videoScrubberWrapper", { clientWidth: 600 });
  reg("videoScrubberTrack", { clientWidth: 600 });
  reg("scrubberProgressBar");
  reg("scrubberBufferedBar");
  reg("scrubberThumb");
  reg("scrubberHoverPreview");
  reg("scrubberHoverTime");
  reg("exportFrameSel", { tagName: "SELECT", value: "9:16" });
  reg("btnTimelineUndo", { tagName: "BUTTON" });
  reg("btnTimelineRedo", { tagName: "BUTTON" });
  reg("btnTimelineSplit", { tagName: "BUTTON" });
  reg("btnTimelineDelete", { tagName: "BUTTON" });
  reg("btnTimelineCopy", { tagName: "BUTTON" });
  reg("btnTimelinePaste", { tagName: "BUTTON" });
  reg("btnTimelineMoveToHook", { tagName: "BUTTON" });
  reg("btnApplyTimelineEdits", { tagName: "BUTTON", innerText: "✓ Áp dụng cắt ghép" });
  reg("timelineClipsSummary");
  reg("badgeVideoClipsCount");
  reg("badgeTextClipsCount");
  reg("badgeMusicClipsCount");
  reg("btnToolbarPreviewVoice", { tagName: "BUTTON" });
  reg("toolbarPreviewIcon", { tagName: "SPAN", innerText: "▶" });
  reg("toolbarVoiceSelect", { tagName: "SELECT", value: "capcut-BV562_streaming" });
  reg("toastMsg");

  // Tabs & Inspector panels
  const primaryTabs = ["subtitle-style", "clip-props", "text-props", "framing", "tools"].map((key, i) =>
    makeElement(`tabBtn_${key}`, { tagName: "BUTTON", dataset: { tab: key }, classes: i === 0 ? ["panel-tab", "active"] : ["panel-tab"] })
  );
  const ratioBtns = ["9:16", "16:9", "1:1", "4:5", "original"].map((r, i) =>
    makeElement(`ratioBtn_${r}`, { tagName: "BUTTON", dataset: { ratio: r }, classes: i === 0 ? ["ratio-btn", "active"] : ["ratio-btn"] })
  );
  const subBar = reg("utilitySubtabsBar");
  ["thumbnail", "watermark", "voice-picker", "batch-folder"].forEach((st, i) => {
    subBar.appendChild(makeElement(`subTab_${st}`, { tagName: "BUTTON", dataset: { subtab: st }, classes: i === 0 ? ["utility-subtab", "active"] : ["utility-subtab"] }));
  });

  const fontSwatches = reg("subFontColorSwatches");
  ["#000000", "#ffffff", "#facc15", "#34d399", "#38bdf8", "#ef4444"].forEach((hex, i) => {
    fontSwatches.appendChild(makeElement(`fontDot_${i}`, { tagName: "BUTTON", dataset: { color: hex }, classes: i === 0 ? ["sub-color-dot", "active"] : ["sub-color-dot"] }));
  });
  const bgSwatches = reg("subBgColorSwatches");
  ["#ffffff", "#000000", "#0f172a"].forEach((hex, i) => {
    bgSwatches.appendChild(makeElement(`bgDot_${i}`, { tagName: "BUTTON", dataset: { color: hex }, classes: i === 0 ? ["sub-color-dot", "active"] : ["sub-color-dot"] }));
  });

  reg("subFontFamilySelect", { tagName: "SELECT", value: "Arial" });
  reg("subFontColorPicker", { tagName: "INPUT", value: "#000000" });
  reg("subFontColorHex", { innerText: "#000000" });
  reg("subFontSizeSlider", { tagName: "INPUT", value: "18" });
  reg("subFontSizeVal", { innerText: "18px" });
  reg("chkSubBold", { tagName: "INPUT", checked: true });
  reg("subStrokeWidthSlider", { tagName: "INPUT", value: "0" });
  reg("subStrokeWidthVal", { innerText: "0px" });
  reg("subStrokeColorPicker", { tagName: "INPUT", value: "#000000" });
  reg("chkSubShadow", { tagName: "INPUT", checked: false });
  reg("subBgColorPicker", { tagName: "INPUT", value: "#ffffff" });
  reg("subBgColorHex", { innerText: "#FFFFFF" });
  reg("subBgOpacitySlider", { tagName: "INPUT", value: "100" });
  reg("subBgOpacityVal", { innerText: "100%" });
  reg("subPaddingXSlider", { tagName: "INPUT", value: "16" });
  reg("subPaddingXVal", { innerText: "16px" });
  reg("subPaddingYSlider", { tagName: "INPUT", value: "8" });
  reg("subPaddingYVal", { innerText: "8px" });
  reg("subRadiusSlider", { tagName: "INPUT", value: "8" });
  reg("subRadiusVal", { innerText: "8px" });
  reg("subPosYSlider", { tagName: "INPUT", value: "80" });
  reg("subPosYVal", { innerText: "80%" });

  reg("tabSubtitleStyle", { display: "flex" });
  reg("tabClipProps", { display: "none" });
  reg("tabTextProps", { display: "none" });
  reg("tabFraming", { display: "none" });
  reg("tabThumbnail", { display: "none" });
  reg("tabWatermark", { display: "none" });
  reg("tabVoicePicker", { display: "none" });
  reg("tabBatchFolder", { display: "none" });
  reg("tabAntiReup", { display: "none" });
  reg("sidebarStickyFooter", { display: "flex" });
  reg("cpEmptyMsg");
  reg("cpBody", { display: "none" });
  reg("tpEmptyMsg");
  reg("tpTextBody", { display: "none" });
  reg("tpOverlayBody", { display: "none" });
  reg("tpMusicBody", { display: "none" });

  const sampleTrackRow = makeElement("trackRowVideo", { offsetLeft: 0 });
  elements.set("trackRowVideo", sampleTrackRow);

  // Tự động đăng ký mọi id="..." thực có trong workflow.html để khớp 100% DOM thật
  const pureMarkup = htmlSource
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const idMatchRe = /\bid="([^"]+)"/g;
  let im;
  while ((im = idMatchRe.exec(pureMarkup)) !== null) {
    if (!elements.has(im[1])) reg(im[1]);
  }

  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    document: {
      visibilityState: "visible",
      activeElement: null,
      fullscreenElement: null,
      getElementById(id) { return elements.get(id) || null; },
      querySelector(sel) {
        if (sel === ".track-content") return elements.get("videoClipsTrackContent");
        if (sel === ".track-row") return sampleTrackRow;
        return null;
      },
      querySelectorAll(sel) {
        if (sel === ".panel-tab") return primaryTabs;
        if (sel === ".ratio-btn") return ratioBtns;
        return [];
      },
      addEventListener() {},
      createElement(tag) { return makeElement("", { tagName: tag }); },
      createRange() { return { selectNodeContents() {}, collapse() {} }; },
      body: makeElement("body", { tagName: "BODY" }),
      documentElement: makeElement("html", { tagName: "HTML" }),
    },
    window: null,
    fetch: opts.fetch || (async () => ({ ok: true, json: async () => ({}) })),
    setTimeout(fn, ms) {
      if (opts.immediatePoll && typeof fn === "function" && (ms === 1000 || ms === 500 || ms === 800)) {
        queueMicrotask(fn);
      }
      return 1;
    },
    clearTimeout() {},
    setInterval(fn) {
      if (opts.onInterval) return opts.onInterval(fn);
      return 1;
    },
    clearInterval() {},
    requestAnimationFrame() { return 1; },
    cancelAnimationFrame() {},
    getSelection() { return { removeAllRanges() {}, addRange() {}, toString() { return ""; } }; },
    URL: { createObjectURL() { return "blob:mock"; } },
    navigator: { userAgent: "node-test" },
  };
  sandbox.window = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.window.removeEventListener = () => {};

  vm.createContext(sandbox);
  vm.runInContext(scripts[0], sandbox, { filename: "workflow.html#script-1" });
  vm.runInContext(timeCoreSource, sandbox, { filename: "timeline_time_core.js" });
  vm.runInContext(scripts[1], sandbox, { filename: "workflow.html#script-2" });
  vm.runInContext(timelineSource, sandbox, { filename: "timeline.js" });
  vm.runInContext(scripts[2], sandbox, { filename: "workflow.html#script-3" });

  // Expose lexical globals for assertions
  vm.runInContext(`
    globalThis.__studio = {
      getCanvasSettings: () => canvasSettings,
      getTimelineProject: () => timelineProject,
      getVideoClips: () => videoClipsState,
      setVideoClips: (c) => { videoClipsState = c; timelineProject.tracks[0].clips = c; },
      getSelectedClipId: () => selectedClipId,
      setSelectedClipId: (id) => { selectedClipId = id; selectedClipIds = id ? [id] : []; },
      setSubtitles: (subs) => { currentSubtitles = subs; },
      setCurrentVideo: (v) => { currentVideo = v; },
      setVideos: (arr) => { videos = arr; },
      setFilterMode: (m) => { currentFilter = m; },
      setRatio,
      switchTab,
      updateSubBgOpacity,
      applySubtitlePreset,
      updateTimecode,
      updatePlayhead,
      seekTimelineToRatio,
      updateSubtitlesOverlay,
      handleCanvasStageClick,
      toggleToolbarVoicePreview,
      stopVoicePreview,
      initVideoClipsForCurrentVideo,
      splitCurrentClip,
      deleteSelectedClip,
      undoTimelineEdit,
      redoTimelineEdit,
      moveSelectedClipToHook,
      selectClip,
      handleSmartTimelinePlayback,
      buildTimelineRenderPayload,
      calcCueSpeechRateInfo,
      jumpToNextRushedCue,
      renderSubtitlesTimeline,
      renderMediaList,
      applyChannelProfile,
      renderChannelProfileSelect,
      getCurrentVideoData: () => currentVideoData,
      setCurrentVideoData: (d) => { currentVideoData = d; },
      getCurrentVideoVersion: () => currentVideoVersion,
      setCurrentVideoVersion: (v) => { currentVideoVersion = v; },
      getVocalMode: () => currentVocalIsolationMode,
      getVocalTracks: () => currentVocalTracks,
      setVocalTracks: (t) => { currentVocalTracks = Object.assign(currentVocalTracks, t); updateVocalUI(); },
      getManualTtsState: () => manualTtsState,
      selectVideo,
      switchVideoVersion,
      setVocalIsolationMode,
      triggerSeparateVocalsAI,
      syncVocalPlayer,
      runManualJob,
      manualGenerateTts,
      toggleManualTtsSync,
      openExportModal,
      handleApplyEditsSuccess,
    };
  `, sandbox);

  return { sandbox, elements, primaryTabs, ratioBtns, studio: sandbox.__studio };
}

test("DOM tối giản chuẩn CapCut: bỏ Kịch Bản, bỏ link lặp, gộp 1 hàng công cụ và mở khoá đủ 6 track", () => {
  assert.ok(!htmlSource.includes("/kich-ban"), "Không còn đường dẫn /kich-ban đã xóa");
  assert.ok(!htmlSource.includes("Kịch Bản"), "Không còn nhãn nút Kịch Bản");
  assert.ok(!htmlSource.includes('href="http://127.0.0.1:8088/quy-trinh"'), "Header không còn link tự trỏ lại chính trang Quy Trình");
  assert.ok(!htmlSource.includes("timeline-toolbar-row1"), "Đã bỏ thanh công cụ 2 tầng rườm rà");
  assert.ok(!htmlSource.includes("timeline-toolbar-row2"), "Đã bỏ thanh công cụ tầng 2 trùng lặp");
  assert.ok(htmlSource.includes("timeline-capcut-toolbar"), "Có thanh công cụ CapCut 1 hàng gọn gàng");
  assert.ok(htmlSource.includes("/static/js/timeline.js?v=7"), "Cache-buster timeline.js?v=7 được nạp");
  assert.ok(/\.timeline-tracks\s*\{[^}]*overflow-y:\s*auto\s*!important/s.test(htmlSource), "Khu vực tracks cho phép cuộn dọc để không bao giờ che mất Chữ & Logo / Nhạc & SFX");
  assert.ok(htmlSource.includes('id="subBgOpacitySlider" min="0"'), "Thanh kéo độ đậm nền cho phép kéo về 0% (trong suốt)");
});

test("Đồng bộ tỷ lệ khung hình Header -> #exportFrameSel -> payload xuất cắt ghép", () => {
  const { elements, studio } = createStudioHarness();
  studio.setCurrentVideo({ stem: "demo_video", filename: "demo_video.mp4" });
  studio.initVideoClipsForCurrentVideo(true);

  studio.setRatio("16:9", { silentSave: true });
  assert.strictEqual(elements.get("exportFrameSel").value, "16:9");
  let payload = JSON.parse(JSON.stringify(studio.buildTimelineRenderPayload()));
  assert.deepStrictEqual(payload.export_frame, { w: 1920, h: 1080 });

  studio.setRatio("1:1", { silentSave: true });
  assert.strictEqual(elements.get("exportFrameSel").value, "1:1");
  payload = JSON.parse(JSON.stringify(studio.buildTimelineRenderPayload()));
  assert.deepStrictEqual(payload.export_frame, { w: 1080, h: 1080 });

  studio.setRatio("9:16", { silentSave: true });
  assert.strictEqual(elements.get("exportFrameSel").value, "9:16");
  payload = JSON.parse(JSON.stringify(studio.buildTimelineRenderPayload()));
  assert.deepStrictEqual(payload.export_frame, { w: 1080, h: 1920 });
});

test("Phụ đề CapCut: độ đậm nền 0% trong suốt & nền đen #000000 không bị biến thành màu trắng", () => {
  const { elements, studio } = createStudioHarness();

  studio.updateSubBgOpacity("0");
  assert.strictEqual(studio.getCanvasSettings().sub_bg_opacity, 0);
  assert.strictEqual(elements.get("subBgOpacityVal").innerText, "0%");
  assert.ok(elements.get("subVietnameseText").style.backgroundColor.endsWith(", 0)"), "Nền 0% phải có alpha = 0");

  studio.applySubtitlePreset("white_black_box");
  assert.strictEqual(elements.get("subVietnameseText").style.backgroundColor, "rgba(0, 0, 0, 0.85)");

  studio.applySubtitlePreset("yellow_black_stroke");
  assert.strictEqual(studio.getCanvasSettings().sub_bg_opacity, 0);
  assert.strictEqual(elements.get("subVietnameseText").style.backgroundColor, "rgba(0, 0, 0, 0)");
});

test("Chỉnh 1 clip đơn (trim + speed): Timecode, Playhead, Seek và Smart Playback chính xác tuyệt đối", () => {
  const { elements, studio } = createStudioHarness();
  const player = elements.get("mainPlayer");
  player.duration = 20;

  // 1 clip cắt từ giây 4 -> 10 (6s nguồn) với tốc độ 2x => thời lượng timeline thực = 3.00s
  studio.setVideoClips([{ id: "clip_1", start: 4, end: 10, at: 0, speed: 2, volume: 100 }]);
  studio.setSelectedClipId("clip_1");
  player.currentTime = 7; // chính giữa [4..10] => 1.5s trên timeline 3.0s

  studio.updateTimecode();
  assert.strictEqual(elements.get("timecodeDisplay").innerText, "00:01.50 / 00:03.00");

  studio.seekTimelineToRatio(0.5);
  assert.ok(Math.abs(player.currentTime - 7.0) < 1e-6, "Click 50% timeline phải nhảy tới đúng giây 7.0 của nguồn");

  // Khi đang phát chạm điểm kết thúc clip (10.0s), player tự động dừng chuẩn CapCut và đồng bộ tốc độ 2x
  player.paused = false;
  player.currentTime = 2.0; // trước điểm trim đầu
  studio.handleSmartTimelinePlayback();
  assert.strictEqual(player.currentTime, 4.0, "Tự nhảy vào đầu vùng trim khi phát trước clip.start");
  assert.strictEqual(player.playbackRate, 2, "Đồng bộ tốc độ clip 2x khi phát");

  player.currentTime = 10.0; // chạm điểm cuối clip
  studio.handleSmartTimelinePlayback();
  assert.strictEqual(player.paused, true, "Tự dừng mượt tại điểm kết thúc timeline");
});

test("Thao tác cắt ghép CapCut cốt lõi: Tách (C), Xóa dồn nhịp, Hook, Undo/Redo & tự mở tab Clip", () => {
  const { elements, studio } = createStudioHarness();
  const player = elements.get("mainPlayer");
  player.duration = 12;
  studio.setCurrentVideo({ stem: "vid_capcut", filename: "vid_capcut.mp4" });
  studio.initVideoClipsForCurrentVideo(true);
  assert.strictEqual(studio.getVideoClips().length, 1);

  // Tách đôi tại giây 5.0
  player.currentTime = 5.0;
  studio.splitCurrentClip();
  assert.strictEqual(studio.getVideoClips().length, 2);
  assert.strictEqual(studio.getVideoClips()[0].end, 5.0);
  assert.strictEqual(studio.getVideoClips()[1].start, 5.0);

  // Tách tiếp tại giây 8.0 -> 3 đoạn: [0..5], [5..8], [8..12]
  player.currentTime = 8.0;
  studio.splitCurrentClip();
  assert.strictEqual(studio.getVideoClips().length, 3);

  // Đưa đoạn cuối [8..12] lên đầu làm Hook
  const thirdId = studio.getVideoClips()[2].id;
  studio.selectClip(thirdId);
  studio.moveSelectedClipToHook();
  assert.strictEqual(studio.getVideoClips()[0].id, thirdId);
  assert.strictEqual(studio.getVideoClips()[0].at, 0);

  // Xóa đoạn đầu (ripple delete) -> còn 2 đoạn và đoạn kế dồn về 00:00
  studio.deleteSelectedClip(false);
  assert.strictEqual(studio.getVideoClips().length, 2);
  assert.strictEqual(studio.getVideoClips()[0].at, 0);

  // Undo & Redo đồng bộ trạng thái nút trên thanh công cụ
  studio.undoTimelineEdit();
  assert.strictEqual(studio.getVideoClips().length, 3);
  assert.strictEqual(elements.get("btnTimelineRedo").style.opacity, "1");

  studio.redoTimelineEdit();
  assert.strictEqual(studio.getVideoClips().length, 2);

  // Đang ở tab Lớp phủ (text-props), bấm chọn clip video tự chuyển sang tab Clip (clip-props)
  studio.switchTab("text-props");
  assert.strictEqual(elements.get("tabTextProps").style.display, "flex");
  studio.selectClip(studio.getVideoClips()[0].id);
  assert.strictEqual(elements.get("tabClipProps").style.display, "flex");
});

test("5 Tab chính + Subtab Tiện ích + Ẩn/hiện chân trang lưu cấu hình & Nút nghe thử giọng icon-only", () => {
  const { elements, studio } = createStudioHarness();

  studio.switchTab("tools");
  assert.strictEqual(elements.get("utilitySubtabsBar").classList.contains("visible"), true);
  assert.strictEqual(elements.get("tabThumbnail").style.display, "flex");
  assert.strictEqual(elements.get("sidebarStickyFooter").style.display, "none");

  studio.switchTab("watermark");
  assert.strictEqual(elements.get("utilitySubtabsBar").classList.contains("visible"), true);
  assert.strictEqual(elements.get("tabWatermark").style.display, "flex");
  assert.strictEqual(elements.get("sidebarStickyFooter").style.display, "flex");

  studio.switchTab("framing");
  assert.strictEqual(elements.get("utilitySubtabsBar").classList.contains("visible"), false);
  assert.strictEqual(elements.get("tabFraming").style.display, "flex");
  assert.strictEqual(elements.get("sidebarStickyFooter").style.display, "flex");

  // Nút nghe thử giọng đọc chỉ đổi ▶ <-> ⏹, không ghi đè thành chữ "Thử"/"Dừng" làm vỡ khung
  studio.toggleToolbarVoicePreview();
  assert.strictEqual(elements.get("toolbarPreviewIcon").innerText, "⏹");
  studio.stopVoicePreview();
  assert.strictEqual(elements.get("toolbarPreviewIcon").innerText, "▶");
});

test("Khi phát video qua khoảng lặng giữa 2 câu phụ đề, không bị nhấp nháy lại câu #1 hoặc chữ mẫu", () => {
  const { elements, studio } = createStudioHarness();
  const player = elements.get("mainPlayer");
  studio.setSubtitles([
    { index: 1, start: 1.0, end: 3.0, text: "Câu một" },
    { index: 2, start: 5.0, end: 7.0, text: "Câu hai" },
  ]);

  player.paused = false;
  player.currentTime = 4.0; // khoảng lặng giữa câu 1 và câu 2
  studio.updateSubtitlesOverlay();
  assert.strictEqual(elements.get("subtitleInteractiveBox").style.display, "none");
  assert.strictEqual(elements.get("subVietnameseText").innerText, "");

  player.currentTime = 5.5; // vào câu 2
  studio.updateSubtitlesOverlay();
  assert.strictEqual(elements.get("subtitleInteractiveBox").style.display, "inline-flex");
  assert.strictEqual(elements.get("subVietnameseText").innerText, "Câu hai");

  // Bấm vùng trống trên khung canvas khi đang phát -> tạm dừng mượt như CapCut
  studio.handleCanvasStageClick({ target: elements.get("fgContainer") });
  assert.strictEqual(player.paused, true);
});

test("Cảnh báo thông minh câu thoại quá dài / đọc gấp trên Track Phụ đề AI + Nhảy nhanh tới câu dài", () => {
  const { elements, studio } = createStudioHarness();
  const player = elements.get("mainPlayer");
  player.duration = 15;

  const okInfo = studio.calcCueSpeechRateInfo("Xin chào các bạn", 0, 2.0);
  assert.strictEqual(okInfo.level, "ok");

  const severeInfo = studio.calcCueSpeechRateInfo("Hôm nay mình sẽ khui thử nguyên một thùng giấy bọc xốp nổ siêu to khổng lồ này", 3.0, 4.5);
  assert.strictEqual(severeInfo.level, "severe");

  studio.setSubtitles([
    { index: 1, start: 0.5, end: 2.5, text: "Câu ngắn vừa nhịp" },
    { index: 2, start: 4.0, end: 5.2, text: "Câu tiếng Việt dịch ra quá dài khiến giọng đọc phải tua cực gấp mới kịp khung hình" },
  ]);
  studio.renderSubtitlesTimeline();

  assert.ok(elements.get("aiTrackStatus").innerText.includes("⚠️ 1 dài"), "Pill trạng thái track AI phải báo số câu bị dài");
  const subTrackChildren = elements.get("subtitlesTrack").children;
  assert.strictEqual(subTrackChildren.length, 2);
  assert.ok(subTrackChildren[1].className.includes("sub-clip-rush-severe"), "Thẻ câu quá dài phải được tô đỏ cảnh báo");

  player.currentTime = 0;
  studio.jumpToNextRushedCue();
  assert.ok(Math.abs(player.currentTime - 4.04) < 0.02, "Nhảy thẳng tới đúng vị trí câu bị dài để sửa");
});

test("Thư viện 800+ video: hiện câu tiếng Việt đầu tiên, tìm kiếm không dấu, lọc Hôm nay và phân trang 50 thẻ", () => {
  const { elements, studio } = createStudioHarness();
  const nowSec = Date.now() / 1000;
  const oldSec = nowSec - 5 * 86400;

  const mockVideos = [];
  for (let i = 0; i < 120; i++) {
    mockVideos.push({
      stem: `telegram_${i}`,
      filename: `telegram_${i}_中文标题.mp4`,
      size_mb: 12.5,
      created_at: i < 5 ? nowSec : oldSec,
      steps: { step1: true, step2: true, step3: true, step3_5: true, step4: true, step5: true, step6: i % 2 === 0 },
      preview_text: i === 3 ? "Hộp giấy nắp gài đóng hàng siêu cứng cáp" : `Nội dung mẫu số ${i}`,
      preview_lang: "vi",
    });
  }
  studio.setVideos(mockVideos);

  // Mặc định chỉ render tối đa 50 thẻ đầu tiên thay vì dồn 120 thẻ gây giật DOM
  studio.renderMediaList(true);
  assert.strictEqual(elements.get("mediaList").children.length, 50);

  // Tìm kiếm không dấu bằng lời thoại tiếng Việt ("hop giay nap gai") dù tên file toàn tiếng Trung
  elements.get("searchInput").value = "hop giay nap gai";
  studio.renderMediaList(true);
  assert.strictEqual(elements.get("mediaList").children.length, 1);
  assert.strictEqual(elements.get("mediaList").children[0].dataset.stem, "telegram_3");
  assert.ok(elements.get("mediaList").children[0].innerHTML.includes("Hộp giấy nắp gài"));

  // Lọc Hôm nay
  elements.get("searchInput").value = "";
  studio.setFilterMode("today");
  studio.renderMediaList(true);
  assert.strictEqual(elements.get("mediaList").children.length, 5);
});

test("Hồ sơ Kênh 1-Click: đổi trọn gói Tỷ lệ khung + Màu chữ + Watermark trong 1 cú nhấp", () => {
  const { studio } = createStudioHarness();
  const cfg = studio.getCanvasSettings();
  cfg.channel_profiles = [
    {
      id: "ch_giadung",
      name: "Kênh Gia Dụng",
      voice_id: "",
      settings: {
        aspect_ratio: "9:16",
        sub_font_color: "#facc15",
        sub_bg_opacity: 0,
        watermark_enabled: true,
        watermark_text: "@GiaDungThongMinh",
      },
    },
  ];
  studio.renderChannelProfileSelect();
  studio.applyChannelProfile("ch_giadung");

  assert.strictEqual(cfg.sub_font_color, "#facc15");
  assert.strictEqual(cfg.sub_bg_opacity, 0);
  assert.strictEqual(cfg.watermark_enabled, true);
  assert.strictEqual(cfg.watermark_text, "@GiaDungThongMinh");
});

test("Quy trình Tạo Phụ Đề (Sub Trung -> Dịch): nạp thẳng lên Track 1 & Track 2, ẩn sub Trung đè canvas và đo chuẩn âm tiết Việt", async () => {
  const cnCues = [
    { index: 1, start: 0.0, end: 2.2, start_str: "00:00:00,000", end_str: "00:00:02,200", text: "打包量大人的真的无脑冲了" },
    { index: 2, start: 2.3, end: 5.0, start_str: "00:00:02,300", end_str: "00:00:05,000", text: "质量超级结实" },
  ];
  const viCues = [
    { index: 1, start: 0.0, end: 2.2, start_str: "00:00:00,000", end_str: "00:00:02,200", text: "Ai cần đóng hàng nhiều thì chốt ngay nhé", original_text: "打包量大人的真的无脑冲了" },
    { index: 2, start: 2.3, end: 5.0, start_str: "00:00:02,300", end_str: "00:00:05,000", text: "Chất lượng siêu cứng cáp luôn", original_text: "质量超级结实" },
  ];

  const calls = [];
  const { elements, studio } = createStudioHarness({
    immediatePoll: true,
    fetch: async (url, init) => {
      calls.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      if (url.includes("/api/workflow/manual/generate-cn")) {
        return { ok: true, json: async () => ({ status: "ok", job_id: "job_cn_1" }) };
      }
      if (url.includes("job_id=job_cn_1")) {
        return { ok: true, json: async () => ({ status: "done", progress: 100, stage: "Đã tạo 2 câu sub Trung", result: { count: 2, subtitles: cnCues } }) };
      }
      if (url.includes("/api/workflow/video-data")) {
        return {
          ok: true,
          json: async () => ({
            stem: "vid_sub_flow",
            raw_video_url: "/stream/raw.mp4",
            final_video_url: "/stream/final.mp4",
            has_final: true,
            has_chinese_sub: true,
            chinese_subtitles: cnCues,
            vietnamese_subtitles: [],
            subtitles: cnCues,
            steps: { step3: { ready: true, count: 2 }, step4: { ready: false, count: 0 } },
          }),
        };
      }
      if (url.includes("/api/workflow/manual/translate")) {
        return { ok: true, json: async () => ({ status: "ok", job_id: "job_tr_1" }) };
      }
      if (url.includes("job_id=job_tr_1")) {
        return { ok: true, json: async () => ({ status: "done", progress: 100, stage: "Đã dịch 2 câu", result: { count: 2, subtitles: viCues } }) };
      }
      return { ok: true, json: async () => ({}) };
    },
  });

  const vObj = { stem: "vid_sub_flow", filename: "vid_sub_flow.mp4", has_final: true };
  studio.setVideos([vObj]);
  studio.setCurrentVideo(vObj);
  studio.setCurrentVideoData({
    stem: "vid_sub_flow",
    raw_video_url: "/stream/raw.mp4",
    final_video_url: "/stream/final.mp4",
    has_final: true,
    chinese_subtitles: [],
    vietnamese_subtitles: [],
  });
  studio.setCurrentVideoVersion("final");

  // 1. Chạy Sub Trung -> Track 1 hiển thị ngay 2 câu tiếng Trung mà không bị rỗng
  await studio.runManualJob("cn", elements.get("btnManualCn"));
  assert.strictEqual(elements.get("chineseSubtitlesTrack").children.length, 2, "Track 1 phải nạp ngay 2 câu Sub Trung");
  assert.ok(elements.get("chineseTrackStatus").innerText.includes("2 câu"));

  // 2. Chạy Dịch -> gửi kèm chinese_subtitles, chuyển sang Video gốc an toàn (không crash #videoStripLabel) và hiện 2 câu Việt trên Track 2
  await studio.runManualJob("translate", elements.get("btnManualTranslate"));
  const trCall = calls.find((c) => c.url.includes("/api/workflow/manual/translate"));
  assert.strictEqual(trCall.body.chinese_subtitles.length, 2);
  assert.strictEqual(studio.getCurrentVideoVersion(), "raw", "Sau khi dịch tự chuyển sang phôi gốc để thấy chữ mới ngay");
  assert.strictEqual(elements.get("subtitlesTrack").children.length, 2, "Track 2 hiển thị đủ 2 câu tiếng Việt");

  // 3. Kiểm tra câu 9 âm tiết trong 2.2s ("Ai cần đóng hàng nhiều thì chốt ngay nhé") KHÔNG bị báo nhầm là đọc gấp
  const rate9Words = studio.calcCueSpeechRateInfo("Ai cần đóng hàng nhiều thì chốt ngay nhé", 0.0, 2.2);
  assert.strictEqual(rate9Words.wordCount, 9);
  assert.strictEqual(rate9Words.level, "ok", "9 âm tiết / 2.2s (~4.1 từ/s) là tốc độ tự nhiên, không được báo lỗi DÀI");

  // 4. Khi hiển thị câu phụ đề trên màn hình 9:16, #subOriginalText luôn ẩn để không đè chồng chữ Trung lên video
  elements.get("mainPlayer").currentTime = 1.0;
  studio.updateSubtitlesOverlay();
  assert.strictEqual(elements.get("subVietnameseText").innerText, "Ai cần đóng hàng nhiều thì chốt ngay nhé");
  assert.strictEqual(elements.get("subOriginalText").style.display, "none", "Không hiển thị chồng dòng chữ Trung dưới hộp sub trên canvas");
});

test("Quy trình Tách Giọng Nói BS-RoFormer: đồng bộ nhạc nền, tự phục hồi khi lỗi và reset sạch khi đổi video", async () => {
  let pollFn = null;
  let shouldFailTrigger = false;
  const { elements, studio } = createStudioHarness({
    onInterval(fn) { pollFn = fn; return 77; },
    fetch: async (url) => {
      if (url.includes("/api/workflow/audio/separate-vocals")) {
        if (shouldFailTrigger) {
          return { ok: false, json: async () => ({ detail: "GPU đang bận" }) };
        }
        return { ok: true, json: async () => ({ status: "started", progress: 15 }) };
      }
      if (url.includes("/api/workflow/audio/separate-status")) {
        return {
          ok: true,
          json: async () => ({
            status: "completed",
            progress: 100,
            has_separated: true,
            instrumental_url: "/stream/no_vocals.wav",
            vocals_url: "/stream/vocals.wav",
          }),
        };
      }
      if (url.includes("/api/workflow/video-data")) {
        return {
          ok: true,
          json: async () => ({
            stem: "vid_b",
            raw_video_url: "/stream/vid_b.mp4",
            video_url: "/stream/vid_b.mp4",
            has_final: false,
            subtitles: [],
            vocal_separation: { has_separated: false, instrumental_url: "", vocals_url: "", status: "none" },
            steps: {},
          }),
        };
      }
      if (url.includes("/api/workflow/audio/vocal-status")) {
        return { ok: true, json: async () => ({ has_separated: false, status: "none", progress: 0 }) };
      }
      return { ok: true, json: async () => ({}) };
    },
  });

  const mainPlayer = elements.get("mainPlayer");
  const vocalPlayer = elements.get("vocalIsolationPlayer");
  studio.setCurrentVideo({ stem: "vid_a", filename: "vid_a.mp4" });

  // 1. Khi lỗi kết nối/GPU lúc kích hoạt tách âm ở chế độ no_vocals -> tự quay về 'full' và không bị câm tiếng mainPlayer
  shouldFailTrigger = true;
  await studio.setVocalIsolationMode("no_vocals");
  assert.strictEqual(studio.getVocalMode(), "full", "Lỗi tách âm phải tự trả chế độ về 'full'");
  assert.strictEqual(mainPlayer.muted, false, "mainPlayer không bị kẹt trạng thái tắt tiếng khi tách âm lỗi");

  // 2. Khi tách âm thành công -> tự kích hoạt phát đồng bộ no_vocals trên vocalIsolationPlayer
  shouldFailTrigger = false;
  await studio.setVocalIsolationMode("no_vocals");
  assert.ok(typeof pollFn === "function", "Bắt đầu vòng poll tiến độ BS-RoFormer");
  mainPlayer.currentTime = 6.5;
  mainPlayer.paused = false;
  await pollFn();

  assert.strictEqual(studio.getVocalTracks().has_separated, true);
  assert.strictEqual(elements.get("vocalAiStatusText").innerText, "Đã tách");
  assert.strictEqual(mainPlayer.muted, true, "Tắt tiếng gốc trên video để phát kênh nhạc nền đã tách");
  assert.strictEqual(vocalPlayer.src, "/stream/no_vocals.wav");
  assert.strictEqual(vocalPlayer.paused, false);
  assert.ok(Math.abs(vocalPlayer.currentTime - 6.5) < 0.05);

  // 3. Khi chuyển sang video khác (vid_b) -> reset sạch vocalIsolationPlayer & khôi phục mainPlayer.muted = false
  await studio.selectVideo({ stem: "vid_b", filename: "vid_b.mp4" });
  assert.strictEqual(studio.getVocalMode(), "full");
  assert.strictEqual(mainPlayer.muted, false);
  assert.strictEqual(vocalPlayer.paused, true);
  assert.strictEqual(vocalPlayer.src, "");
});

test("Quy trình Lồng Tiếng Studio (Tạo Giọng & Đọc Lại 1 Câu): phát đồng bộ trên video, tự bật chkTtsMix và truyền vào cắt ghép", async () => {
  const { elements, studio } = createStudioHarness({
    immediatePoll: true,
    fetch: async (url) => {
      if (url.includes("/api/workflow/manual/generate-tts")) {
        return { ok: true, json: async () => ({ status: "ok", job_id: "job_tts_99" }) };
      }
      if (url.includes("job_id=job_tts_99")) {
        return {
          ok: true,
          json: async () => ({
            status: "done",
            progress: 100,
            stage: "Đã tạo giọng đọc 2/2 câu",
            result: {
              count: 2,
              failed: [],
              track_url: "/stream/manual_tts_track.wav?v=123",
              cue_audio_url: "/stream/cue_1.mp3?v=123",
              voice: "Mai (Nữ chuẩn)",
            },
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    },
  });

  const mainPlayer = elements.get("mainPlayer");
  const vocalPlayer = elements.get("vocalIsolationPlayer");
  const ttsPlayer = elements.get("manualTtsPlayer");

  studio.setCurrentVideo({ stem: "vid_dub", filename: "vid_dub.mp4", has_final: true });
  studio.setCurrentVideoData({
    stem: "vid_dub",
    raw_video_url: "/stream/raw_dub.mp4",
    final_video_url: "/stream/final_dub.mp4",
    has_final: true,
    steps: { step5: { ready: false } },
  });
  studio.setCurrentVideoVersion("final");
  studio.setVocalTracks({
    has_separated: true,
    has_instrumental: true,
    instrumental_url: "/stream/inst_bgm.wav",
    vocals_url: "/stream/orig_voc.wav",
  });
  studio.setSubtitles([
    { index: 1, start: 0.5, end: 2.5, text: "Xin chào mọi người" },
    { index: 2, start: 3.0, end: 5.5, text: "Hôm nay khui kiện hàng mới" },
  ]);
  studio.initVideoClipsForCurrentVideo(true);

  // Gọi Tạo Giọng -> chạy job ngầm & tự bật đồng bộ giọng mới
  studio.manualGenerateTts();
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  const ttsState = studio.getManualTtsState();
  assert.strictEqual(ttsState.has_track, true);
  assert.strictEqual(ttsState.active, true, "Sau khi tạo giọng xong phải tự BẬT chế độ phát đồng bộ giọng mới");
  assert.strictEqual(studio.getCurrentVideoVersion(), "raw", "Tự chuyển sang Video phôi gốc để không phát chồng giọng cũ của bản final");
  assert.strictEqual(elements.get("btnToggleManualTtsSync").style.display, "inline-flex");
  assert.strictEqual(elements.get("chkTtsMix").checked, true, "Tự đánh dấu sẵn 'Trộn lồng tiếng AI' cho hộp thoại Xuất Video");

  // Khi bấm Play trên video -> phát đồng thời manualTtsPlayer (100% âm lượng) + vocalIsolationPlayer nhạc nền (82% âm lượng), tắt tiếng gốc mainPlayer!
  mainPlayer.currentTime = 3.2;
  mainPlayer.play();
  assert.strictEqual(mainPlayer.muted, true, "Tắt tiếng gốc video để không bị chồng 2 giọng");
  assert.strictEqual(ttsPlayer.src, "/stream/manual_tts_track.wav?v=123");
  assert.strictEqual(ttsPlayer.paused, false);
  assert.ok(Math.abs(ttsPlayer.currentTime - 3.2) < 0.05);
  assert.strictEqual(vocalPlayer.src, "/stream/inst_bgm.wav", "Tự lấy nhạc nền BS-RoFormer làm nền cho giọng AI mới");
  assert.strictEqual(vocalPlayer.paused, false);

  // Mở hộp thoại Xuất Video -> giữ nguyên tích chọn chkTtsMix
  studio.openExportModal();
  assert.strictEqual(elements.get("chkTtsMix").checked, true);

  // Bấm Áp dụng cắt ghép -> payload gửi kèm tts_mix: true; khi hoàn tất thành phẩm mới thì tắt lớp phủ manualTtsState.active để tránh phát đúp
  const renderPayload = studio.buildTimelineRenderPayload();
  assert.strictEqual(renderPayload.tts_mix, true, "Payload cắt ghép phải gửi kèm tts_mix=true");

  studio.handleApplyEditsSuccess({
    message: "Đã ghép xong",
    video_url: "/stream/final_dub_new.mp4",
    final_video_url: "/stream/final_dub_new.mp4",
    subtitles: [{ index: 1, start: 0.5, end: 2.5, text: "Xin chào mọi người" }],
  }, "vid_dub");
  assert.strictEqual(studio.getManualTtsState().active, false, "Sau khi giọng mới đã nướng vào final.mp4, tự tắt phát rời để không bị vang đôi");
  assert.strictEqual(mainPlayer.muted, false);
});
