// P6.4: test JS thuần cho logic thời gian timeline (chạy: node --test tests/js/)
const test = require("node:test");
const assert = require("node:assert");
const core = require("../../backend/static/js/timeline_time_core.js");

test("clipSpeedOf: mặc định 1, kẹp 0.25–4", () => {
  assert.strictEqual(core.clipSpeedOf({}), 1);
  assert.strictEqual(core.clipSpeedOf({ speed: 2 }), 2);
  assert.strictEqual(core.clipSpeedOf({ speed: 99 }), 1);
  assert.strictEqual(core.clipSpeedOf({ speed: 0.1 }), 1);
  assert.strictEqual(core.clipSpeedOf({ speed: 0.5 }), 0.5);
});

test("clipDurOf: nguồn/speed + freeze", () => {
  assert.strictEqual(core.clipDurOf({ start: 0, end: 10 }), 10);
  assert.strictEqual(core.clipDurOf({ start: 0, end: 10, speed: 2 }), 5);
  assert.strictEqual(core.clipDurOf({ start: 0, end: 10, speed: 0.5 }), 20);
  assert.strictEqual(core.clipDurOf({ start: 0, end: 10, freeze: 2 }), 12);
  assert.strictEqual(core.clipDurOf({ start: 0, end: 10, speed: 4, freeze: 1 }), 3.5);
});

test("sourceToTimelineT: at + freeze + speed", () => {
  assert.strictEqual(core.sourceToTimelineT({ start: 0, end: 10, at: 0 }, 5), 5);
  assert.strictEqual(core.sourceToTimelineT({ start: 0, end: 10, at: 20 }, 5), 25);
  assert.strictEqual(
    core.sourceToTimelineT({ start: 0, end: 10, at: 0, speed: 2 }, 8),
    4
  );
  assert.strictEqual(
    core.sourceToTimelineT({ start: 0, end: 10, at: 0, freeze: 1 }, 2),
    3
  );
});

test("timelineToSourceT: nghịch đảo của sourceToTimelineT", () => {
  const clip = { start: 0, end: 10, at: 7, speed: 2, freeze: 1 };
  for (const src of [0, 2.5, 10]) {
    const tl = core.sourceToTimelineT(clip, src);
    assert.ok(Math.abs(core.timelineToSourceT(clip, tl) - src) < 1e-9, `src=${src}`);
  }
});

test("timelineToSourceT: kẹp trong khoảng clip khi timeline ngoài phạm vi", () => {
  const clip = { start: 5, end: 15, at: 0, speed: 1 };
  assert.strictEqual(core.timelineToSourceT(clip, -5), 5);
  assert.strictEqual(core.timelineToSourceT(clip, 50), 15);
});
