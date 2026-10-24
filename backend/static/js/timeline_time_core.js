    function clipSpeedOf(c) {
      return (typeof c.speed === 'number' && c.speed >= 0.25 && c.speed <= 4) ? c.speed : 1;
    }

    function clipDurOf(c) {
      // Thời lượng trên timeline = nguồn / speed + freeze-frame đầu đoạn (P2)
      return Math.max(0.01, (c.end - c.start) / clipSpeedOf(c) + (c.freeze || 0));
    }

    function sourceToTimelineT(clip, sourceT) {
      return (clip.at || 0) + (clip.freeze || 0) + (sourceT - clip.start) / clipSpeedOf(clip);
    }

    function timelineToSourceT(clip, tlT) {
      const local = Math.max(0, Math.min(clipDurOf(clip), tlT - (clip.at || 0) - (clip.freeze || 0)));
      return clip.start + local * clipSpeedOf(clip);
    }

    // Map một giây NGUỒN sang trục timeline đã ghép (qua clip chứa nó); dùng cho track phụ đề (P2)

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { clipSpeedOf, clipDurOf, sourceToTimelineT, timelineToSourceT };
}
