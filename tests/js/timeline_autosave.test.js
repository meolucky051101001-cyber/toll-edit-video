// Hồi quy quick-win Studio: autosave không keepalive + auto-cut vào undo stack.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert");

const TIMELINE_JS = path.join(__dirname, "../../backend/static/js/timeline.js");
const SOURCE = fs.readFileSync(TIMELINE_JS, "utf8");

const HOOKS = `
;globalThis.__tl = {
  saveNow: function (silent, useKeepalive) { return saveTimelineProjectNow(silent, useKeepalive); },
  startAutosave: function () { startTimelineAutosave(); },
  autoCut: function () { return autoCutTimelineSilences(); },
  setClips: function (clips) { videoClipsState = clips; },
  clips: function () { return JSON.parse(JSON.stringify(videoClipsState)); },
  markDirty: function () { timelineProjectDirty = true; },
  undoLen: function () { return timelineUndoStack.length; },
  undoTopClips: function () {
    var s = timelineUndoStack[timelineUndoStack.length - 1];
    return s ? JSON.parse(JSON.stringify(s.project.tracks[0].clips)) : null;
  }
};
`;

function makeSandbox() {
  const documentHandlers = {};
  const calls = [];
  let reply = { ok: true, json: async () => ({}) };
  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    document: {
      visibilityState: "visible",
      getElementById() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      addEventListener(ev, fn) { documentHandlers[ev] = fn; },
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
    setTimeout,
    clearTimeout,
    setInterval() { return 1; },
    clearInterval() {},
    requestAnimationFrame(fn) { return setTimeout(fn, 0); },
    cancelAnimationFrame: clearTimeout,
    navigator: { userAgent: "node" },
    currentVideo: null,
    currentVideoVersion: "final",
    showToast() {},
    __setReply(next) { reply = next; },
  };
  vm.createContext(sandbox);
  vm.runInContext("globalThis.window = globalThis;", sandbox);
  vm.runInContext(SOURCE + HOOKS, sandbox, { filename: "timeline.js" });
  return { sandbox, calls, documentHandlers, hooks: sandbox.__tl };
}

test("autosave định kỳ không dùng keepalive (body project có thể vượt 64KB)", async () => {
  const { sandbox, calls, hooks } = makeSandbox();
  sandbox.currentVideo = { stem: "vid-1" };
  hooks.setClips([{ id: "c1", start: 0, end: 2, at: 0 }]);

  assert.strictEqual(await hooks.saveNow(true), true);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].init.keepalive, false);
});

test("autosave khi ẩn tab vẫn keepalive best-effort", async () => {
  const { sandbox, calls, documentHandlers, hooks } = makeSandbox();
  sandbox.currentVideo = { stem: "vid-1" };
  hooks.setClips([{ id: "c1", start: 0, end: 2, at: 0 }]);
  hooks.startAutosave();
  hooks.markDirty();

  sandbox.document.visibilityState = "hidden";
  documentHandlers.visibilitychange();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].init.keepalive, true);
});

test("tự động cắt khoảng lặng đẩy snapshot trước khi cắt vào undo stack", async () => {
  const { sandbox, hooks } = makeSandbox();
  sandbox.currentVideo = { stem: "vid-1" };
  hooks.setClips([{ id: "c1", start: 0, end: 4, at: 0 }]);
  sandbox.__setReply({
    ok: true,
    json: async () => ({
      silence_count: 1,
      removed_seconds: 1,
      active_clips: [{ id: "new1", start: 1, end: 4, at: 0 }],
    }),
  });

  await hooks.autoCut();

  assert.deepStrictEqual(Array.from(hooks.clips(), (c) => c.id), ["new1"]);
  assert.strictEqual(hooks.undoLen(), 1);
  assert.deepStrictEqual(
    Array.from(hooks.undoTopClips(), (c) => [c.id, c.end]),
    [["c1", 4]]
  );
});
