// Hồi quy Studio: kết quả render chỉ áp cho đúng stem đã gửi + xoá track đã "nướng" sau render.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert");

const TIMELINE_JS = path.join(__dirname, "../../backend/static/js/timeline.js");
const SOURCE = fs.readFileSync(TIMELINE_JS, "utf8");

const HOOKS = `
;globalThis.__tl = {
  applySuccess: function (data, stem) { return handleApplyEditsSuccess(data, stem); },
  initFresh: function () { return initVideoClipsForCurrentVideo(true, { saveFresh: true }); },
  seed: function () {
    timelineProject.tracks = [
      { id: 'v1', type: 'video', clips: [{ id: 'vc1', start: 0, end: 10, at: 0, duration: 10 }] },
      { id: 't1', type: 'text', clips: [{ id: 'txt_1', text: 'Hello', at: 1, dur: 2 }] },
      { id: 'o1', type: 'overlay', clips: [{ id: 'ovl_1', src: '', at: 2, dur: 2 }] },
      { id: 'a1', type: 'audio', clips: [{ id: 'mus_1', at: 0, dur: 2 }] }
    ];
  },
  trackIds: function (type) {
    var t = timelineProject.tracks.find(function (x) { return x.type === type; });
    return t ? Array.from(t.clips, function (c) { return c.id; }) : null;
  },
  clips: function () { return Array.from(videoClipsState, function (c) { return c.id; }); }
};
`;

function makeSandbox() {
  const calls = [];
  const toasts = [];
  let mediaRenders = 0;
  let reply = { ok: true, json: async () => ({}) };
  const realSetTimeout = setTimeout;
  const longTimers = [];
  let captureTimers = false;
  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    document: {
      visibilityState: "visible",
      getElementById() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      addEventListener() {},
      createElement() {
        return {
          style: {},
          dataset: {},
          classList: { add() {}, remove() {}, toggle() {} },
          appendChild() {},
          addEventListener() {},
          setAttribute() {},
          removeAttribute() {},
        };
      },
      body: { appendChild() {}, classList: { add() {}, remove() {}, toggle() {} } },
      documentElement: { style: {} },
    },
    fetch: async (url, init) => {
      calls.push({ url: String(url), init });
      return reply;
    },
    setTimeout(fn, ms) {
      if (captureTimers && ms >= 100) {
        longTimers.push(fn);
        return longTimers.length;
      }
      return realSetTimeout(fn, ms);
    },
    clearTimeout,
    setInterval() { return 1; },
    clearInterval() {},
    requestAnimationFrame(fn) { return realSetTimeout(fn, 0); },
    cancelAnimationFrame: clearTimeout,
    navigator: { userAgent: "node" },
    currentVideo: null,
    currentVideoData: null,
    currentVideoVersion: "final",
    mainPlayer: { duration: 10, src: "/current.mp4", load() {}, play() { return Promise.resolve(); } },
    showToast(msg) { toasts.push(String(msg)); },
    renderMediaList() { mediaRenders += 1; },
    __setReply(next) { reply = next; },
  };
  vm.createContext(sandbox);
  vm.runInContext("globalThis.window = globalThis;", sandbox);
  vm.runInContext(SOURCE + HOOKS, sandbox, { filename: "timeline.js" });
  return {
    sandbox, calls, toasts, hooks: sandbox.__tl,
    captureTimers() { captureTimers = true; },
    flushLongTimers() {
      const queued = longTimers.splice(0);
      queued.forEach((fn) => fn());
    },
    mediaRenders: () => mediaRenders,
  };
}

test("kết quả render của video khác không đè player/project đang mở", () => {
  const { sandbox, hooks, toasts, captureTimers, flushLongTimers } = makeSandbox();
  captureTimers();
  hooks.seed();
  sandbox.currentVideo = { stem: "vid-B", has_final: false };
  sandbox.mainPlayer.src = "/current-B.mp4";

  hooks.applySuccess({ video_url: "/rendered-A.mp4", message: "Xong" }, "vid-A");
  flushLongTimers(); // timer 800ms cũng phải bỏ qua vì stem đã lệch

  assert.strictEqual(sandbox.mainPlayer.src, "/current-B.mp4");
  assert.strictEqual(sandbox.currentVideo.has_final, false);
  assert.deepStrictEqual(Array.from(hooks.trackIds("text")), ["txt_1"]);
  assert.ok(toasts.some((t) => t.includes("vid-A")));
});

test("render xong cho đúng stem: reset video + xoá chữ/logo/nhạc đã nướng", () => {
  const { sandbox, hooks, captureTimers, flushLongTimers } = makeSandbox();
  captureTimers();
  hooks.seed();
  sandbox.currentVideo = { stem: "vid-A", has_final: false };

  hooks.applySuccess({ video_url: "/rendered-A.mp4", message: "Xong" }, "vid-A");
  flushLongTimers();

  assert.strictEqual(sandbox.mainPlayer.src, "/rendered-A.mp4");
  assert.strictEqual(sandbox.currentVideo.has_final, true);
  const clipIds = Array.from(hooks.clips());
  assert.strictEqual(clipIds.length, 1);
  assert.ok(clipIds[0].startsWith("clip_"));
  assert.deepStrictEqual(Array.from(hooks.trackIds("text")), []);
  assert.deepStrictEqual(Array.from(hooks.trackIds("overlay")), []);
  assert.deepStrictEqual(Array.from(hooks.trackIds("audio")), []);
});
