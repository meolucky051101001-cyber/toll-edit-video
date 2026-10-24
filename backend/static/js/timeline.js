    // ========================================================
    // CAPCUT-STYLE INTERACTIVE TIMELINE — PROJECT JSON MULTI-TRACK CORE (P1)
    // videoClipsState giờ là ALIAS SỐNG tới track video đầu tiên của project JSON,
    // nên toàn bộ code cũ đọc/ghi mảng clip phẳng vẫn hoạt động (tương thích ngược).
    // Mỗi clip có thêm `at` = vị trí giây trên timeline (cho phép gap).
    // ========================================================
    let timelineProject = {
      version: 1,
      fps: 30,
      canvas: { w: 1080, h: 1920 },
      tracks: [
        { id: 'v1', type: 'video', name: 'Video chính', clips: [] },
        { id: 't1', type: 'text', name: 'Chữ', clips: [] },
        { id: 'o1', type: 'overlay', name: 'Logo/Sticker', clips: [] },
        { id: 'a1', type: 'audio', name: 'Nhạc & SFX', clips: [] }
      ]
    };
    let selectedClipId = null;
    let isHandlingSmartPlay = false;
    let timelineUndoStack = [];
    let timelineRedoStack = [];
    let copiedClipBuffer = null;
    let selectedClipIds = [];        // multi-select (Ctrl/Shift click, rubber band)
    let lastMultiSelectAnchorId = null;
    let justRubberBanded = false;

    // Zoom: số pixel trên mỗi giây (100% = 60px/s; dải 10% = 6px/s → 800% = 480px/s)
    const TIMELINE_ZOOM_BASE = 60;
    const TIMELINE_ZOOM_MIN = 6;
    const TIMELINE_ZOOM_MAX = 480;
    let timelinePxPerSec = parseFloat(localStorage.getItem('v1_timeline_pxpersec') || '') || TIMELINE_ZOOM_BASE;
    let timelineSnapEnabled = localStorage.getItem('v1_timeline_snap') !== '0';

    // Migrate mảng clip kiểu cũ (không có `at`) → bố cục liền kề tương đương
    function migrateClipsToAt(clips) {
      const arr = Array.isArray(clips) ? clips : [];
      if (arr.some(c => typeof c.at !== 'number')) {
        let acc = 0;
        arr.forEach((c, i) => {
          c.id = c.id || ('clip_legacy_' + Date.now() + '_' + i);
          c.at = acc;
          acc += Math.max(0.01, c.end - c.start);
        });
      }
      arr.forEach((c, i) => {
        c.id = c.id || ('clip_' + Date.now() + '_' + i);
        c.duration = c.end - c.start;
      });
      return arr;
    }

    Object.defineProperty(window, 'videoClipsState', {
      configurable: true,
      get() { return timelineProject.tracks[0].clips; },
      set(v) { timelineProject.tracks[0].clips = migrateClipsToAt(Array.isArray(v) ? v : []); }
    });

    // Project JSON đầy đủ (dùng cho autosave P6.1 + render ở P6.3 — hiện xuất ra console khi cần debug)
    function getTimelineProjectSnapshot() {
      return JSON.parse(JSON.stringify(timelineProject));
    }

    // ===== P6.1: AUTOSAVE + KHÔI PHỤC PROJECT TIMELINE =====
    let timelineProjectDirty = false;
    let _projectSaveTimer = null;

    function markTimelineProjectDirty() {
      timelineProjectDirty = true;
    }

    async function saveTimelineProjectNow(silent = true) {
      try {
        if (!currentVideo || !currentVideo.stem || !videoClipsState.length) return false;
        const payload = { stem: currentVideo.stem, project: getTimelineProjectSnapshot() };
        const res = await fetch('/api/workflow/timeline/project', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Local-Control-Token': (typeof CONTROL_PLANE_TOKEN !== 'undefined' ? CONTROL_PLANE_TOKEN : '__REPLACE_TOKEN__'),
            'X-Dashboard-Input': '1'
          },
          body: JSON.stringify(payload),
          keepalive: true
        });
        if (res.ok) {
          timelineProjectDirty = false;
          if (!silent) showToast('💾 Đã lưu project timeline!');
          return true;
        }
      } catch (err) {
        console.warn('Lưu project timeline lỗi:', err);
      }
      return false;
    }

    function startTimelineAutosave() {
      if (_projectSaveTimer) return;
      _projectSaveTimer = setInterval(() => {
        if (timelineProjectDirty) saveTimelineProjectNow(true);
      }, 10000);
      // Rời tab / tắt trang: cố gắng lưu nốt những thay đổi chưa ghi (best-effort)
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden' && timelineProjectDirty) saveTimelineProjectNow(true);
      });
    }

    async function maybeRestoreTimelineProject(stem) {
      try {
        if (!stem) return false;
        const res = await fetch(`/api/workflow/timeline/project?stem=${encodeURIComponent(stem)}`);
        if (!res.ok) return false;
        const data = await res.json();
        const proj = data && data.exists ? data.project : null;
        const track = proj && Array.isArray(proj.tracks) ? proj.tracks[0] : null;
        const clips = track && Array.isArray(track.clips) ? track.clips : [];
        if (!clips.length) return false;

        // Chống khôi phục nhầm khi video nguồn đã thay đổi (pipeline chạy lại / tải lại video)
        const pdur = mainPlayer && isFinite(mainPlayer.duration) ? mainPlayer.duration : 0;
        const maxSrcEnd = clips.reduce((m, c) => Math.max(m, +c.end || 0), 0);
        if (pdur > 0 && maxSrcEnd > pdur + 0.75) {
          console.warn('Bỏ khôi phục project: video nguồn đã khác khi lưu', { maxSrcEnd, pdur });
          return false;
        }

        // Chỉ migrate `at` cho track VIDEO (text/overlay đã dùng at/dur riêng)
        (proj.tracks || []).forEach(t => { if (t && t.type === 'video' && Array.isArray(t.clips)) migrateClipsToAt(t.clips); });
        // Project cũ (lưu trước P3) có thể thiếu track text/overlay → tự bổ sung để không mất tính năng
        const mergedTracks = (proj.tracks || []).filter(t => t && t.type === 'video');
        if (!mergedTracks.some(t => t.type === 'text')) mergedTracks.push({ id: 't1', type: 'text', name: 'Chữ', clips: [] });
        if (!mergedTracks.some(t => t.type === 'overlay')) mergedTracks.push({ id: 'o1', type: 'overlay', name: 'Logo/Sticker', clips: [] });
        if (!mergedTracks.some(t => t.type === 'audio')) mergedTracks.push({ id: 'a1', type: 'audio', name: 'Nhạc & SFX', clips: [] });
        timelineProject = {
          version: proj.version || 1,
          fps: proj.fps || 30,
          canvas: proj.canvas || { w: 1080, h: 1920 },
          tracks: mergedTracks
        };
        selectedClipId = clips[0].id;
        selectedClipIds = [selectedClipId];
        timelineUndoStack = [];
        timelineRedoStack = [];
        timelineProjectDirty = false;
        renderVideoClipsTrack();
        updateTimelineToolbarStates();
        const nSeg = clips.length;
        showToast(`🔄 Đã khôi phục project timeline đã lưu (${nSeg} đoạn, lưu lúc ${data.saved_at || 'không rõ'})!`);
        return true;
      } catch (err) {
        console.warn('Khôi phục project timeline lỗi:', err);
        return false;
      }
    }

    function sortClipsByAt(arr) { return arr.sort((a, b) => (a.at || 0) - (b.at || 0)); }

    function setSingleSelection(clipId) {
      selectedClipId = clipId;
      selectedClipIds = [clipId];
      lastMultiSelectAnchorId = clipId;
    }

    function clearMultiSelect() {
      selectedClipIds = [];
      lastMultiSelectAnchorId = null;
    }

    function selectAllTimelineClips() {
      if (!videoClipsState.length) return;
      selectedClipIds = videoClipsState.map(c => c.id);
      selectedClipId = selectedClipIds[selectedClipIds.length - 1];
      renderVideoClipsTrack();
      showToast(`🔖 Đã chọn tất cả ${selectedClipIds.length} đoạn (Ctrl+A)`);
    }

    function toggleMultiSelect(clipId, rangeMode) {
      const anchorIdx = videoClipsState.findIndex(c => c.id === lastMultiSelectAnchorId);
      const targetIdx = videoClipsState.findIndex(c => c.id === clipId);
      if (rangeMode && anchorIdx > -1 && targetIdx > -1) {
        const a = Math.min(anchorIdx, targetIdx);
        const b = Math.max(anchorIdx, targetIdx);
        selectedClipIds = videoClipsState.slice(a, b + 1).map(c => c.id);
        selectedClipId = clipId;
        renderVideoClipsTrack();
        return;
      }
      const i = selectedClipIds.indexOf(clipId);
      if (i > -1) selectedClipIds.splice(i, 1);
      else selectedClipIds.push(clipId);
      lastMultiSelectAnchorId = clipId;
      if (selectedClipIds.length) selectedClipId = selectedClipIds[selectedClipIds.length - 1];
      renderVideoClipsTrack();
    }

    // Xóa clip khỏi mảng và rip-nhịp: mọi đoạn bên phải được kéo về trái lấp chỗ trống
    function rippleRemoveClipIdx(clips, idx) {
      const c = clips[idx];
      if (!c) return null;
      clips.splice(idx, 1);
      const d = clipDurOf(c);
      clips.forEach(k => { if ((k.at || 0) > (c.at || 0) + 1e-6) k.at = (k.at || 0) - d; });
      return c;
    }

    // Chèn clip tại vị trí timeline atTime, đẩy các đoạn chồng lấn sang phải
    function rippleInsertClip(clips, atTime, clip) {
      clip.at = Math.max(0, atTime);
      const d = clipDurOf(clip);
      clips.forEach(k => { if ((k.at || 0) >= clip.at - 1e-6) k.at = (k.at || 0) + d; });
      clips.push(clip);
      sortClipsByAt(clips);
    }

    function pushTimelineHistory(actionLabel = '') {
      if (!videoClipsState || !videoClipsState.length) return;
      timelineProjectDirty = true; // P6.1: mọi thao tác sửa timeline đều đánh dấu chờ autosave
      timelineUndoStack.push({
        project: JSON.parse(JSON.stringify(timelineProject)),
        selectedClipId: selectedClipId,
        selectedClipIds: [...selectedClipIds],
        actionLabel: actionLabel
      });
      if (timelineUndoStack.length > 40) {
        timelineUndoStack.shift();
      }
      timelineRedoStack = [];
      updateTimelineToolbarStates();
    }

    function updateTimelineToolbarStates() {
      const undoBtn = document.getElementById('btnTimelineUndo');
      if (undoBtn) {
        undoBtn.style.opacity = timelineUndoStack.length > 0 ? '1' : '0.55';
        undoBtn.title = timelineUndoStack.length > 0
          ? `Hoàn tác thao tác vừa làm (${timelineUndoStack.length} bước lưu - Phím tắt: Ctrl+Z)`
          : 'Chưa có thao tác nào để hoàn tác (Phím tắt: Ctrl+Z)';
      }
      const pasteBtn = document.getElementById('btnTimelinePaste');
      if (pasteBtn) {
        pasteBtn.style.opacity = copiedClipBuffer ? '1' : '0.65';
        if (copiedClipBuffer) {
          const clipDur = Math.max(0.1, copiedClipBuffer.end - copiedClipBuffer.start).toFixed(1);
          pasteBtn.title = `Dán đoạn đã copy (${formatTime(copiedClipBuffer.start)} - ${formatTime(copiedClipBuffer.end)}, ${clipDur}s) - Phím tắt: Ctrl+V`;
        }
      }
      const delBtn = document.getElementById('btnTimelineDelete');
      if (delBtn) {
        const n = selectedClipIds.length;
        delBtn.title = n > 1
          ? `Xóa ${n} đoạn đang chọn (Del: rip-nhịp dồn về trái, Shift+Del: xóa để trống)`
          : 'Xóa đoạn đang chọn (Del: rip-nhịp dồn về trái, Shift+Del: xóa để trống)';
      }
      updateSnapToggleUi();
    }

    function restoreTimelineSnapshot(prevState) {
      timelineProject = prevState.project;
      selectedClipId = prevState.selectedClipId;
      selectedClipIds = Array.isArray(prevState.selectedClipIds) ? [...prevState.selectedClipIds] : [];
      if ((!selectedClipId || !videoClipsState.some(c => c.id === selectedClipId)) && videoClipsState.length) {
        selectedClipId = videoClipsState[0].id;
      }
      if (!selectedClipIds.includes(selectedClipId)) selectedClipIds = [selectedClipId];
      renderVideoClipsTrack();
      updateTimelineToolbarStates();
    }

    function undoTimelineEdit() {
      if (!timelineUndoStack.length) {
        showToast('ℹ️ Không còn thao tác nào trước đó để hoàn tác (Ctrl+Z)!');
        return;
      }
      const prevState = timelineUndoStack.pop();
      timelineRedoStack.push({
        project: JSON.parse(JSON.stringify(timelineProject)),
        selectedClipId: selectedClipId,
        selectedClipIds: [...selectedClipIds],
        actionLabel: prevState.actionLabel
      });
      restoreTimelineSnapshot(prevState);
      showToast(`↩️ Đã hoàn tác (Ctrl+Z)! Còn ${videoClipsState.length} đoạn video.`);
    }

    function redoTimelineEdit() {
      if (!timelineRedoStack.length) {
        showToast('ℹ️ Không có thao tác nào để làm lại (Ctrl+Y)!');
        return;
      }
      const nextState = timelineRedoStack.pop();
      timelineUndoStack.push({
        project: JSON.parse(JSON.stringify(timelineProject)),
        selectedClipId: selectedClipId,
        selectedClipIds: [...selectedClipIds],
        actionLabel: nextState.actionLabel
      });
      restoreTimelineSnapshot(nextState);
      showToast(`↪️ Đã làm lại thao tác (Ctrl+Y)! Hiện có ${videoClipsState.length} đoạn video.`);
    }

    // ===== ZOOM ENGINE =====
    function applyZoomLayout() {
      const span = getTimelineSpan();
      const widthPx = span > 0 ? Math.max(240, Math.round(span * timelinePxPerSec)) : 0;
      document.querySelectorAll('.timeline-tracks .track-content').forEach(el => {
        el.style.width = widthPx ? widthPx + 'px' : '';
      });
      document.querySelectorAll('.timeline-tracks .track-row').forEach(el => {
        el.style.minWidth = widthPx ? (widthPx + 228) + 'px' : '';
      });
      const rulerEl = document.getElementById('timelineRuler');
      if (rulerEl) rulerEl.style.minWidth = widthPx ? (widthPx + 228) + 'px' : '';
      const clipsContainer = document.getElementById('videoClipsContainer');
      if (clipsContainer) clipsContainer.style.width = widthPx ? widthPx + 'px' : '';
      const textTrackContainer = document.getElementById('textOverlayTrack');
      if (textTrackContainer) textTrackContainer.style.width = widthPx ? widthPx + 'px' : '';
      const musicTrackContainer = document.getElementById('musicTrack');
      if (musicTrackContainer) musicTrackContainer.style.width = widthPx ? widthPx + 'px' : '';
      drawAudioWaveform(); // P5: vẽ lại waveform theo zoom mới
      const audioStrip = document.getElementById('audioStripLabel');
      if (audioStrip) audioStrip.style.width = widthPx ? (widthPx - 8) + 'px' : '';
      const slider = document.getElementById('timelineZoomSlider');
      if (slider) slider.value = String(Math.round(timelinePxPerSec));
      const pct = document.getElementById('timelineZoomPct');
      if (pct) pct.innerText = Math.round((timelinePxPerSec / TIMELINE_ZOOM_BASE) * 100) + '%';
      localStorage.setItem('v1_timeline_pxpersec', String(Math.round(timelinePxPerSec * 100) / 100));
    }

    function zoomTimelineTo(newPx, anchorClientX = null) {
      newPx = Math.max(TIMELINE_ZOOM_MIN, Math.min(TIMELINE_ZOOM_MAX, newPx));
      if (Math.abs(newPx - timelinePxPerSec) < 0.01) return;
      const scrollEl = document.getElementById('timelineTracks');
      const content = document.getElementById('videoClipsTrackContent');
      let anchorT = null;
      if (scrollEl && content && anchorClientX != null) {
        const rect = content.getBoundingClientRect();
        if (rect.width > 0) anchorT = (anchorClientX - rect.left) / timelinePxPerSec;
      }
      timelinePxPerSec = newPx;
      renderVideoClipsTrack();
      if (anchorT != null && scrollEl && content) {
        const rect2 = content.getBoundingClientRect();
        const desiredLeft = anchorClientX - anchorT * timelinePxPerSec;
        scrollEl.scrollLeft += (rect2.left - desiredLeft);
      }
    }

    function zoomFitTimeline() {
      const scrollEl = document.getElementById('timelineTracks');
      const span = getTimelineSpan();
      if (!scrollEl || span <= 0) return;
      const avail = Math.max(120, scrollEl.clientWidth - 244);
      zoomTimelineTo(avail / span);
    }

    // ===== SNAP ENGINE (ngưỡng 8px, bật/tắt phím N) =====
    function getSnapTargets(excludeClipId) {
      const targets = [0];
      videoClipsState.forEach(c => {
        if (c.id === excludeClipId) return;
        targets.push(c.at || 0);
        targets.push((c.at || 0) + clipDurOf(c));
      });
      targets.push(getEditedTimelineTime()); // playhead
      (currentSubtitles || []).forEach(s => {
        if (typeof s.start === 'number') targets.push(s.start);
        if (typeof s.end === 'number') targets.push(s.end);
      });
      return targets;
    }

    function showSnapGuide(tlT) {
      const g = document.getElementById('snapGuide');
      if (!g) return;
      const content = document.querySelector('.track-content');
      const row = document.querySelector('.track-row');
      if (!content || !row) return;
      g.style.display = 'block';
      g.style.left = (row.offsetLeft + content.offsetLeft + tlT * timelinePxPerSec) + 'px';
    }

    function hideSnapGuide() {
      const g = document.getElementById('snapGuide');
      if (g) g.style.display = 'none';
    }

    function snapTimelineTime(t, excludeClipId = null) {
      hideSnapGuide();
      if (!timelineSnapEnabled) return t;
      const threshold = 8 / timelinePxPerSec;
      let best = null;
      let bestD = threshold;
      getSnapTargets(excludeClipId).forEach(tt => {
        const d = Math.abs(tt - t);
        if (d < bestD) { bestD = d; best = tt; }
      });
      if (best != null) {
        showSnapGuide(best);
        return best;
      }
      return t;
    }

    function setSnapEnabled(v) {
      timelineSnapEnabled = !!v;
      localStorage.setItem('v1_timeline_snap', v ? '1' : '0');
      updateSnapToggleUi();
      showToast(v
        ? '🧲 Đã BẬT snap (N): mép kéo dính vào playhead / mép clip / mép phụ đề'
        : '🧲 Đã TẮT snap (N): mép kéo di chuyển tự do');
    }

    function updateSnapToggleUi() {
      const b = document.getElementById('btnSnapToggle');
      if (b) {
        b.classList.toggle('active-snap', timelineSnapEnabled);
        b.style.opacity = timelineSnapEnabled ? '1' : '0.5';
      }
    }

    // ===== Tiện ích phát / trim bằng phím =====
    function cyclePlayRate() {
      if (!mainPlayer) return;
      const rates = [1, 2, 4];
      const cur = mainPlayer.playbackRate || 1;
      const idx = rates.findIndex(r => Math.abs(r - cur) < 0.01);
      const next = rates[(idx + 1) % rates.length];
      mainPlayer.playbackRate = next;
      if (mainPlayer.paused) mainPlayer.play().catch(() => {});
      showToast(`⏩ Tốc độ phát: ${next}x (phím L để xoay vòng)`);
    }

    function trimSelectedClipToPlayhead(mode) {
      if (!mainPlayer || !videoClipsState.length) return;
      const clip = videoClipsState.find(c => c.id === selectedClipId) || videoClipsState[0];
      const t = mainPlayer.currentTime;
      if (t < clip.start + 0.12 || t > clip.end - 0.12) {
        showToast('⚠️ Đặt playhead BÊN TRONG đoạn cần trim (cách mép ≥ 0.12s) rồi bấm Q (đầu) / W (cuối)!');
        return;
      }
      pushTimelineHistory(mode === 'head' ? 'Trim đầu tới playhead (Q)' : 'Trim cuối tới playhead (W)');
      if (mode === 'head') {
        const deltaTl = (t - clip.start) / clipSpeedOf(clip);
        clip.start = t;
        clip.at = (clip.at || 0) + deltaTl;
      } else {
        clip.end = t;
      }
      clip.duration = clip.end - clip.start;
      renderVideoClipsTrack();
      showToast(mode === 'head'
        ? `✂️ Đã trim ĐẦU đoạn tới ${formatTime(t)} (Q) — mép trái dính playhead`
        : `✂️ Đã trim CUỐI đoạn tới ${formatTime(t)} (W) — mép phải dính playhead`);
    }

    // ========================================================
    // P2: PANEL THUỘC TÍNH CLIP + CROP EDITOR TRÊN PREVIEW
    // ========================================================
    let cropOverlayEl = null;
    let cropEditorActive = false;
    let cropEditorClipId = null;
    const _cpEditPushed = {};

    function ensurePropHistory(key, label) {
      if (!_cpEditPushed[key]) { pushTimelineHistory(label); _cpEditPushed[key] = true; }
    }

    function commitPropEdit(key) { _cpEditPushed[key] = false; }

    function getSelectedClip() {
      return videoClipsState.find(c => c.id === selectedClipId) || null;
    }

    function previewClipAudio(clip) {
      if (!mainPlayer || !clip) return;
      const vol = (typeof clip.volume === 'number') ? clip.volume : 1;
      mainPlayer.volume = Math.max(0, Math.min(1, vol));
      mainPlayer.muted = !!clip.muted || vol <= 0.001;
    }

    function updateClipPropsPanel() {
      const emptyMsg = document.getElementById('cpEmptyMsg');
      const body = document.getElementById('cpBody');
      if (!emptyMsg || !body) return;
      const clip = getSelectedClip();
      if (cropEditorActive && (!clip || clip.id !== cropEditorClipId)) removeCropOverlay();
      if (!clip) {
        emptyMsg.style.display = 'block';
        body.style.display = 'none';
        return;
      }
      emptyMsg.style.display = 'none';
      body.style.display = 'flex';

      const idx = videoClipsState.indexOf(clip) + 1;
      document.getElementById('cpClipName').innerText = `Đoạn ${idx}`;
      document.getElementById('cpClipInfo').innerText =
        `${formatTime(clip.start)}-${formatTime(clip.end)} nguồn → ${clipDurOf(clip).toFixed(2)}s trên timeline`;

      const spd = clipSpeedOf(clip);
      document.getElementById('cpSpeed').value = spd;
      document.getElementById('cpSpeedVal').innerText = spd.toFixed(2) + 'x';
      document.getElementById('cpKeepPitch').checked = clip.keepPitch !== false;
      const vol = (typeof clip.volume === 'number') ? clip.volume : 1;
      document.getElementById('cpVolume').value = Math.round(vol * 100);
      document.getElementById('cpVolumeVal').innerText = Math.round(vol * 100) + '%' + (clip.muted ? ' (im lặng)' : '');
      document.getElementById('cpFadeIn').value = clip.fadeIn || 0;
      document.getElementById('cpFadeOut').value = clip.fadeOut || 0;
      document.getElementById('cpRotate').value = String(clip.rotate || 0);
      document.getElementById('cpFlipH').classList.toggle('on', !!clip.flipH);
      document.getElementById('cpFlipV').classList.toggle('on', !!clip.flipV);
      document.getElementById('cpReverse').checked = !!clip.reverse;
      document.getElementById('cpDetach').checked = !!clip.detachAudio;
      document.getElementById('cpFreeze').value = clip.freeze || 0;
      const badge = document.getElementById('cpCropBadge');
      if (clip.crop) {
        badge.style.display = 'block';
        badge.innerText = `crop: x=${clip.crop.x.toFixed(2)} y=${clip.crop.y.toFixed(2)} w=${clip.crop.w.toFixed(2)} h=${clip.crop.h.toFixed(2)}`;
      } else {
        badge.style.display = 'none';
      }

      // P4: transition + fx
      const isLast = videoClipsState.indexOf(clip) === videoClipsState.length - 1;
      const transSec = document.getElementById('cpTransSection');
      if (transSec) transSec.style.opacity = isLast ? '0.5' : '1';
      const tr = clip.trans || null;
      document.getElementById('cpTransType').value = tr ? tr.type : 'none';
      document.getElementById('cpTransDur').value = tr ? tr.dur : 0.5;
      document.getElementById('cpTransDur').disabled = !tr || isLast;
      document.getElementById('cpTransHint').innerText = isLast
        ? 'Đoạn cuối — không có đoạn kế để nối chuyển cảnh.'
        : 'Chỉ áp dụng giữa 2 đoạn liền kề; timeline tự co ngắn, phụ đề/nhạc tự canh lại.';
      const fx = clip.fx || {};
      document.getElementById('cpFxBrightness').value = Math.round((+fx.brightness || 0) * 100);
      document.getElementById('cpFxBrightnessVal').innerText = Math.round((+fx.brightness || 0) * 100) + '%';
      document.getElementById('cpFxContrast').value = Math.round((+fx.contrast === undefined ? 100 : (+fx.contrast) * 100));
      document.getElementById('cpFxContrastVal').innerText = Math.round((+fx.contrast === undefined ? 1 : +fx.contrast) * 100) + '%';
      document.getElementById('cpFxSaturation').value = Math.round((+fx.saturation === undefined ? 100 : (+fx.saturation) * 100));
      document.getElementById('cpFxSaturationVal').innerText = Math.round((+fx.saturation === undefined ? 1 : +fx.saturation) * 100) + '%';
      document.getElementById('cpFxTemperature').value = Math.round((+fx.temperature || 0) * 100);
      document.getElementById('cpFxTemperatureVal').innerText = Math.round((+fx.temperature || 0) * 100) + '%';
      document.getElementById('cpFxBlur').value = Math.round(+fx.blur || 0);
      document.getElementById('cpFxBlurVal').innerText = String(Math.round(+fx.blur || 0));
      document.getElementById('cpFxVignette').checked = !!fx.vignette;
    }

    function applyClipVisualPreview() {
      const v = mainPlayer;
      if (!v) return;
      const clip = getSelectedClip();
      const rot = clip ? (clip.rotate || 0) : 0;
      const fh = clip ? !!clip.flipH : false;
      const fv = clip ? !!clip.flipV : false;
      if (!clip) {
        v.style.transform = '';
        v.style.filter = '';
        v.style.boxShadow = '';
        return;
      }
      // P4: preview filter màu bằng CSS (gần đúng — render chính xác bằng FFmpeg eq/boxblur/vignette)
      const fx = clip.fx;
      if (fx) {
        const parts = [];
        if (absNz(fx.brightness)) parts.push(`brightness(${1 + (+fx.brightness || 0)})`);
        if (Math.abs((+fx.contrast || 1) - 1) > 0.01) parts.push(`contrast(${(+fx.contrast).toFixed(2)})`);
        if (Math.abs((+fx.saturation || 1) - 1) > 0.01) parts.push(`saturate(${(+fx.saturation).toFixed(2)})`);
        if ((+fx.blur || 0) > 0) parts.push(`blur(${(+fx.blur * 1.2).toFixed(1)}px)`);
        v.style.filter = parts.join(' ');
        v.style.boxShadow = fx.vignette ? 'inset 0 0 90px rgba(0,0,0,0.7)' : '';
      } else {
        v.style.filter = '';
        v.style.boxShadow = '';
      }
      if (!rot && !fh && !fv) {
        v.style.transform = '';
        return;
      }
      // Xoay 90/270 phải thu nhỏ vừa khung để không tràn canvas
      const r = v.getBoundingClientRect();
      const swap = (rot === 90 || rot === 270);
      const k = (swap && r.width > 0) ? r.height / r.width : 1;
      v.style.transformOrigin = 'center center';
      v.style.transform = `rotate(${rot}deg) scale(${fh ? -k : k}, ${fv ? -k : k})`;
    }

    function absNz(v) { return Math.abs(+v || 0) > 0.01; }

    // ===== P4: TRANSITION giữa 2 đoạn liền kề (xfade) =====
    function setClipTransition(clipId, type, dur) {
      const idx = videoClipsState.findIndex(c => c.id === clipId);
      if (idx === -1) return false;
      if (idx >= videoClipsState.length - 1) {
        showToast('⚠️ Chuyển cảnh chỉ áp dụng cho đoạn có đoạn kế liền sau!');
        return false;
      }
      const cur = videoClipsState[idx];
      const next = videoClipsState[idx + 1];
      const D0 = (cur.trans && cur.trans.dur) || 0;
      // khoảng hở giữa 2 đoạn (bù trừ phần chồng lấn của transition đang có)
      const gap = (next.at || 0) - ((cur.at || 0) + clipDurOf(cur)) + D0;
      if (Math.abs(gap) > 0.05) {
        showToast('⚠️ Chuyển cảnh chỉ áp dụng khi 2 đoạn LIỀN NHAU. Hãy xóa khoảng trống trước (Del = rip-nhịp)!');
        return false;
      }
      pushTimelineHistory(type ? 'Thêm/sửa chuyển cảnh' : 'Xóa chuyển cảnh');
      let D1 = 0;
      if (type) {
        D1 = Math.max(0.2, Math.min(1.0, parseFloat(dur) || 0.5, 0.9 * Math.min(clipDurOf(cur), clipDurOf(next))));
        cur.trans = { type: type, dur: +D1.toFixed(3) };
      } else {
        delete cur.trans;
      }
      const delta = D0 - D1;
      if (Math.abs(delta) > 1e-6) {
        for (let k = idx + 1; k < videoClipsState.length; k++) {
          videoClipsState[k].at = (videoClipsState[k].at || 0) + delta;
        }
      }
      renderVideoClipsTrack();
      updateClipPropsPanel();
      return true;
    }

    function openClipPropsTab() {
      const btn = [...document.querySelectorAll('.panel-tab')].find(b => b.textContent.includes('Thuộc Tính'));
      if (btn && document.getElementById('tabClipProps')?.style.display === 'none') btn.click();
    }

    function getVideoDisplayRect() {
      const v = mainPlayer;
      if (!v) return null;
      const r = v.getBoundingClientRect();
      const vw = v.videoWidth, vh = v.videoHeight;
      if (!vw || !vh || r.width <= 0) return null;
      const scale = Math.min(r.width / vw, r.height / vh);
      const w = vw * scale, h = vh * scale;
      return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
    }

    function removeCropOverlay() {
      if (cropOverlayEl) { cropOverlayEl.remove(); cropOverlayEl = null; }
      cropEditorActive = false;
      const btn = document.getElementById('cpCropToggle');
      if (btn) btn.classList.remove('on');
    }

    function buildCropOverlay() {
      const clip = getSelectedClip();
      const disp = getVideoDisplayRect();
      if (!clip || !disp) {
        showToast('⚠️ Chưa có video hoặc chưa chọn đoạn để cắt gọt!');
        return;
      }
      if (!clip.crop) clip.crop = { x: 0.1, y: 0.1, w: 0.8, h: 0.8 };
      cropEditorClipId = clip.id;
      cropEditorActive = true;
      renderCropOverlay();
      const btn = document.getElementById('cpCropToggle');
      if (btn) btn.classList.add('on');
      showToast('✂️ Kéo khung xanh để chọn vùng giữ, kéo 4 góc co giãn, lăn chuột trên khung để zoom. Bấm nút lần nữa để tắt.');
    }

    function renderCropOverlay() {
      const clip = getSelectedClip();
      const disp = getVideoDisplayRect();
      if (!clip || !clip.crop || !disp) { removeCropOverlay(); return; }
      const c = clip.crop;
      if (cropOverlayEl) cropOverlayEl.remove();
      const wrap = document.createElement('div');
      wrap.id = 'clipCropOverlay';
      wrap.style.cssText = `position:fixed; left:${disp.left}px; top:${disp.top}px; width:${disp.width}px; height:${disp.height}px; z-index:9998; pointer-events:none;`;
      const shade = (s) => `<div style="position:absolute; background:rgba(2,6,23,0.72); pointer-events:none; ${s}"></div>`;
      const handleCss = 'position:absolute; width:12px; height:12px; background:#38bdf8; border:2px solid #fff; border-radius:3px; pointer-events:auto; z-index:2;';
      wrap.innerHTML =
        shade(`left:0;top:0;width:100%;height:${(c.y * 100).toFixed(3)}%;`) +
        shade(`left:0;bottom:0;width:100%;height:${((1 - c.y - c.h) * 100).toFixed(3)}%;`) +
        shade(`left:0;top:${(c.y * 100).toFixed(3)}%;width:${(c.x * 100).toFixed(3)}%;height:${(c.h * 100).toFixed(3)}%;`) +
        shade(`right:0;top:${(c.y * 100).toFixed(3)}%;width:${((1 - c.x - c.w) * 100).toFixed(3)}%;height:${(c.h * 100).toFixed(3)}%;`) +
        `<div id="cropSelRect" style="position:absolute; left:${(c.x * 100).toFixed(3)}%; top:${(c.y * 100).toFixed(3)}%; width:${(c.w * 100).toFixed(3)}%; height:${(c.h * 100).toFixed(3)}%; border:2px solid #38bdf8; cursor:move; pointer-events:auto; box-shadow:0 0 0 1px rgba(0,0,0,0.6);"></div>` +
        `<div data-handle="nw" style="${handleCss} cursor:nwse-resize; left:${(c.x * disp.width - 6).toFixed(1)}px; top:${(c.y * disp.height - 6).toFixed(1)}px;"></div>` +
        `<div data-handle="ne" style="${handleCss} cursor:nesw-resize; left:${((c.x + c.w) * disp.width - 6).toFixed(1)}px; top:${(c.y * disp.height - 6).toFixed(1)}px;"></div>` +
        `<div data-handle="sw" style="${handleCss} cursor:nesw-resize; left:${(c.x * disp.width - 6).toFixed(1)}px; top:${((c.y + c.h) * disp.height - 6).toFixed(1)}px;"></div>` +
        `<div data-handle="se" style="${handleCss} cursor:nwse-resize; left:${((c.x + c.w) * disp.width - 6).toFixed(1)}px; top:${((c.y + c.h) * disp.height - 6).toFixed(1)}px;"></div>`;
      document.body.appendChild(wrap);
      cropOverlayEl = wrap;

      wrap.addEventListener('wheel', (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 0.92 : 1.08;
        const cx = c.x + c.w / 2, cy = c.y + c.h / 2;
        c.w = Math.max(0.1, Math.min(1, c.w * factor));
        c.h = Math.max(0.1, Math.min(1, c.h * factor));
        c.x = Math.max(0, Math.min(1 - c.w, cx - c.w / 2));
        c.y = Math.max(0, Math.min(1 - c.h, cy - c.h / 2));
        renderCropOverlay();
      }, { passive: false });

      const rectEl = wrap.querySelector('#cropSelRect');
      rectEl.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        pushTimelineHistory('Di chuyển vùng cắt gọt');
        const startX = e.clientX, startY = e.clientY;
        const c0 = { x: c.x, y: c.y };
        const onMove = (ev) => {
          c.x = Math.max(0, Math.min(1 - c.w, c0.x + (ev.clientX - startX) / disp.width));
          c.y = Math.max(0, Math.min(1 - c.h, c0.y + (ev.clientY - startY) / disp.height));
          renderCropOverlay();
        };
        const onUp = () => {
          window.removeEventListener('mousemove', onMove);
          window.removeEventListener('mouseup', onUp);
          renderVideoClipsTrack();
          updateClipPropsPanel();
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      });

      wrap.querySelectorAll('[data-handle]').forEach(hEl => {
        hEl.addEventListener('mousedown', (e) => {
          e.preventDefault();
          e.stopPropagation();
          pushTimelineHistory('Đổi kích thước vùng cắt gọt');
          const hName = hEl.dataset.handle;
          const startX = e.clientX, startY = e.clientY;
          const c0 = { x: c.x, y: c.y, w: c.w, h: c.h };
          const onMove = (ev) => {
            const dx = (ev.clientX - startX) / disp.width;
            const dy = (ev.clientY - startY) / disp.height;
            if (hName.includes('e')) c.w = Math.max(0.1, Math.min(1 - c.x, c0.w + dx));
            if (hName.includes('s')) c.h = Math.max(0.1, Math.min(1 - c.y, c0.h + dy));
            if (hName.includes('w')) {
              c.x = Math.max(0, Math.min(c0.x + c0.w - 0.1, c0.x + dx));
              c.w = c0.x + c0.w - c.x;
            }
            if (hName.includes('n')) {
              c.y = Math.max(0, Math.min(c0.y + c0.h - 0.1, c0.y + dy));
              c.h = c0.y + c0.h - c.y;
            }
            renderCropOverlay();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            renderVideoClipsTrack();
            updateClipPropsPanel();
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
      });
    }

    function initClipPropsPanel() {
      const $ = (id) => document.getElementById(id);
      if (!$('cpSpeed') || $('cpSpeed').dataset.cpWired) return;
      $('cpSpeed').dataset.cpWired = '1';

      $('cpSpeed').addEventListener('input', () => {
        const clip = getSelectedClip();
        if (!clip) return;
        ensurePropHistory('speed', 'Đổi tốc độ đoạn');
        clip.speed = parseFloat($('cpSpeed').value) || 1;
        $('cpSpeedVal').innerText = clip.speed.toFixed(2) + 'x';
        if (mainPlayer && !mainPlayer.paused && mainPlayer.currentTime >= clip.start && mainPlayer.currentTime <= clip.end) {
          mainPlayer.playbackRate = clip.speed; // nghe thử đúng nhịp ngay
        }
        renderVideoClipsTrack();
      });
      $('cpSpeed').addEventListener('change', () => {
        commitPropEdit('speed');
        const clip = getSelectedClip();
        if (clip) showToast(`⚡ Tốc độ Đoạn ${videoClipsState.indexOf(clip) + 1}: ${clipSpeedOf(clip).toFixed(2)}x — timeline & phụ đề tự canh lại (Ctrl+Z hoàn tác)`);
      });

      document.querySelectorAll('#tabClipProps .cp-chip[data-speed]').forEach(btn => {
        btn.addEventListener('click', () => {
          const clip = getSelectedClip();
          if (!clip) return;
          pushTimelineHistory('Đổi tốc độ đoạn (preset)');
          clip.speed = parseFloat(btn.dataset.speed) || 1;
          renderVideoClipsTrack();
          updateClipPropsPanel();
          showToast(`⚡ Tốc độ Đoạn ${videoClipsState.indexOf(clip) + 1}: ${clip.speed}x`);
        });
      });

      $('cpKeepPitch').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory(e.target.checked ? 'Bật giữ cao độ' : 'Tắt giữ cao độ');
        clip.keepPitch = e.target.checked;
      });

      $('cpVolume').addEventListener('input', () => {
        const clip = getSelectedClip();
        if (!clip) return;
        ensurePropHistory('volume', 'Đổi âm lượng đoạn');
        clip.volume = parseFloat($('cpVolume').value) / 100;
        $('cpVolumeVal').innerText = Math.round(clip.volume * 100) + '%' + (clip.muted ? ' (im lặng)' : '');
        previewClipAudio(clip);
      });
      $('cpVolume').addEventListener('change', () => commitPropEdit('volume'));

      $('cpFadeIn').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory('Đổi fade vào');
        clip.fadeIn = Math.max(0, parseFloat(e.target.value) || 0);
      });
      $('cpFadeOut').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory('Đổi fade ra');
        clip.fadeOut = Math.max(0, parseFloat(e.target.value) || 0);
      });

      $('cpRotate').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory('Xoay khung hình');
        clip.rotate = parseInt(e.target.value, 10) || 0;
        applyClipVisualPreview();
      });
      $('cpFlipH').addEventListener('click', () => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory(clip.flipH ? 'Bỏ lật ngang' : 'Lật ngang');
        clip.flipH = !clip.flipH;
        $('cpFlipH').classList.toggle('on', clip.flipH);
        applyClipVisualPreview();
      });
      $('cpFlipV').addEventListener('click', () => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory(clip.flipV ? 'Bỏ lật dọc' : 'Lật dọc');
        clip.flipV = !clip.flipV;
        $('cpFlipV').classList.toggle('on', clip.flipV);
        applyClipVisualPreview();
      });

      $('cpReverse').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory(e.target.checked ? 'Bật phát ngược' : 'Tắt phát ngược');
        clip.reverse = e.target.checked;
        if (clip.reverse && (clip.end - clip.start) > 60) {
          showToast('⚠️ Đoạn dài hơn 60s nên render sẽ TỰ BỎ qua phát ngược (giới hạn bộ nhớ).');
        }
      });

      $('cpDetach').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory(e.target.checked ? 'Tách âm thanh' : 'Ghép lại âm thanh');
        clip.detachAudio = e.target.checked;
      });

      $('cpFreeze').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        pushTimelineHistory('Đóng băng khung đầu đoạn');
        clip.freeze = Math.max(0, parseFloat(e.target.value) || 0);
        renderVideoClipsTrack();
        updateClipPropsPanel();
      });

      $('cpCropToggle').addEventListener('click', () => {
        if (cropEditorActive) removeCropOverlay();
        else buildCropOverlay();
      });
      $('cpCropReset').addEventListener('click', () => {
        const clip = getSelectedClip();
        if (!clip || !clip.crop) return;
        pushTimelineHistory('Bỏ cắt gọt khung hình');
        clip.crop = null;
        removeCropOverlay();
        renderVideoClipsTrack();
        updateClipPropsPanel();
        showToast('↺ Đã bỏ cắt gọt khung hình của đoạn này');
      });

      // Preview di chuyển khi đổi kích thước cửa sổ → dựng lại overlay theo đúng vị trí
      window.addEventListener('resize', () => { if (cropEditorActive) renderCropOverlay(); });

      // ===== P4: chuyển cảnh + filter màu =====
      $('cpTransType').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip) return;
        const t = e.target.value;
        setClipTransition(clip.id, t === 'none' ? null : t, document.getElementById('cpTransDur').value);
      });
      $('cpTransDur').addEventListener('change', (e) => {
        const clip = getSelectedClip();
        if (!clip || !clip.trans) return;
        setClipTransition(clip.id, clip.trans.type, e.target.value);
      });

      const fxApply = (key, val) => {
        const clip = getSelectedClip();
        if (!clip) return;
        ensurePropHistory('fx' + key, 'Đổi hiệu ứng màu');
        clip.fx = { brightness: 0, contrast: 1, saturation: 1, temperature: 0, blur: 0, vignette: false, ...(clip.fx || {}) };
        clip.fx[key] = val;
        if (Math.abs(clip.fx.brightness) < 1e-3 && Math.abs(clip.fx.contrast - 1) < 1e-3
            && Math.abs(clip.fx.saturation - 1) < 1e-3 && Math.abs(clip.fx.temperature) < 1e-3
            && (clip.fx.blur || 0) < 0.5 && !clip.fx.vignette) {
          delete clip.fx; // về trung tính thì gỡ hẳn cho payload gọn
        }
        applyClipVisualPreview();
      };
      $('cpFxBrightness').addEventListener('input', (e) => {
        const v = (parseFloat(e.target.value) || 0) / 100;
        fxApply('brightness', v);
        document.getElementById('cpFxBrightnessVal').innerText = e.target.value + '%';
      });
      $('cpFxBrightness').addEventListener('change', () => commitPropEdit('fxbrightness'));
      $('cpFxContrast').addEventListener('input', (e) => {
        const v = (parseFloat(e.target.value) || 100) / 100;
        fxApply('contrast', v);
        document.getElementById('cpFxContrastVal').innerText = e.target.value + '%';
      });
      $('cpFxContrast').addEventListener('change', () => commitPropEdit('fxcontrast'));
      $('cpFxSaturation').addEventListener('input', (e) => {
        const v = (parseFloat(e.target.value) || 100) / 100;
        fxApply('saturation', v);
        document.getElementById('cpFxSaturationVal').innerText = e.target.value + '%';
      });
      $('cpFxSaturation').addEventListener('change', () => commitPropEdit('fxsaturation'));
      $('cpFxTemperature').addEventListener('input', (e) => {
        const v = (parseFloat(e.target.value) || 0) / 100;
        fxApply('temperature', v);
        document.getElementById('cpFxTemperatureVal').innerText = e.target.value + '%';
      });
      $('cpFxTemperature').addEventListener('change', () => commitPropEdit('fxtemperature'));
      $('cpFxBlur').addEventListener('input', (e) => {
        const v = parseFloat(e.target.value) || 0;
        fxApply('blur', v);
        document.getElementById('cpFxBlurVal').innerText = e.target.value;
      });
      $('cpFxBlur').addEventListener('change', () => commitPropEdit('fxblur'));
      $('cpFxVignette').addEventListener('change', (e) => {
        fxApply('vignette', e.target.checked);
      });
      $('cpFxReset').addEventListener('click', () => {
        const clip = getSelectedClip();
        if (!clip || !clip.fx) return;
        pushTimelineHistory('Đặt lại hiệu ứng màu');
        delete clip.fx;
        renderVideoClipsTrack();
        updateClipPropsPanel();
        applyClipVisualPreview();
        showToast('↺ Đã đặt lại hiệu ứng màu của đoạn này');
      });
    }

    // ========================================================
    // P3: TRACK CHỮ & LOGO (text/overlay clips) + PREVIEW + PANEL
    // ========================================================
    let selectedTextClipId = null;

    function getTextTrack() { return timelineProject.tracks.find(t => t.type === 'text') || null; }
    function getOverlayTrack() { return timelineProject.tracks.find(t => t.type === 'overlay') || null; }
    function getMusicTrack() { return timelineProject.tracks.find(t => t.type === 'audio') || null; }
    function getAllTextClips() { return [...(getTextTrack()?.clips || []), ...(getOverlayTrack()?.clips || []), ...(getMusicTrack()?.clips || [])]; }
    function getSelectedTextClip() { return getAllTextClips().find(c => c.id === selectedTextClipId) || null; }

    const TEXT_PRESETS = {
      vang: { fontSize: 64, color: '#FFEB3B', outlineColor: '#000000', outlineWidth: 3, bgOpacity: 0, bold: true },
      trang: { fontSize: 56, color: '#FFFFFF', outlineColor: '#000000', outlineWidth: 2, bgOpacity: 0.75, bold: false },
      den: { fontSize: 56, color: '#0B1220', outlineColor: '#FFFFFF', outlineWidth: 3, bgOpacity: 0, bold: true },
      neon: { fontSize: 64, color: '#00E5CC', outlineColor: '#00E5CC', outlineWidth: 4, bgOpacity: 0, bold: true }
    };

    function newTextClip(atTl) {
      return {
        id: 'txt_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
        text: 'Chữ mới',
        at: Math.max(0, atTl || 0),
        dur: 3,
        pos: { x: 0.5, y: 0.8 },
        style: { ...TEXT_PRESETS.vang },
        animIn: 'fade',
        animOut: 'none'
      };
    }

    function addTextClipAtPlayhead() {
      const track = getTextTrack();
      if (!track) return;
      pushTimelineHistory('Thêm chữ');
      const clip = newTextClip(getEditedTimelineTime());
      track.clips.push(clip);
      selectTextClip(clip.id);
      renderTextOverlayTrack();
      renderTextOverlayPreview();
      showToast('📝 Đã thêm chữ — kéo trực tiếp trên video để đặt vị trí, sửa trong panel Chữ & Logo');
    }

    function addOverlayClipFromFile(file) {
      const track = getOverlayTrack();
      if (!track || !file) return;
      if (file.size > 3 * 1024 * 1024) {
        showToast('⚠️ Ảnh quá lớn (>3MB). Hãy dùng logo/ảnh nhỏ hơn!');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        pushTimelineHistory('Thêm logo/ảnh overlay');
        const clip = {
          id: 'ovl_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
          src: String(reader.result || ''),
          name: file.name,
          at: Math.max(0, getEditedTimelineTime()),
          dur: 3,
          pos: { x: 0.8, y: 0.88 },
          scale: 1,
          opacity: 1
        };
        track.clips.push(clip);
        selectTextClip(clip.id);
        renderTextOverlayTrack();
        renderTextOverlayPreview();
        showToast('🖼️ Đã thêm ảnh overlay — kéo trực tiếp trên video để đặt vị trí');
      };
      reader.readAsDataURL(file);
    }

    function deleteTextClipById(clipId) {
      const clip = getAllTextClips().find(c => c.id === clipId);
      if (!clip) return;
      pushTimelineHistory('Xóa chữ/logo');
      const t = getTextTrack();
      const o = getOverlayTrack();
      if (t) t.clips = t.clips.filter(c => c.id !== clipId);
      if (o) o.clips = o.clips.filter(c => c.id !== clipId);
      if (selectedTextClipId === clipId) selectedTextClipId = null;
      renderTextOverlayTrack();
      renderTextOverlayPreview();
      updateTextPropsPanel();
    }

    function selectTextClip(clipId) {
      selectedTextClipId = clipId;
      clearMultiSelect();
      document.querySelectorAll('#textOverlayTrack .timeline-clip-card').forEach(c => {
        c.classList.toggle('active-clip', c.dataset.textClipId === clipId);
      });
      updateTextPropsPanel();
      openTextPropsTab();
    }

    function openTextPropsTab() {
      const btn = [...document.querySelectorAll('.panel-tab')].find(b => b.textContent.includes('Chữ & Logo'));
      if (btn && document.getElementById('tabTextProps')?.style.display === 'none') btn.click();
    }

    function renderTextOverlayTrack() {
      const container = document.getElementById('textOverlayTrack');
      if (!container) return;
      container.innerHTML = '';
      const all = getAllTextClips();
      const badge = document.getElementById('badgeTextClipsCount');
      if (badge) badge.innerText = `${all.length} clip`;

      all.forEach((clip) => {
        const isText = String(clip.id).startsWith('txt_');
        const dur = Math.max(0.1, clip.dur || 3);
        const widthPx = Math.max(3, Math.round(dur * timelinePxPerSec) - 2);
        const leftPx = Math.round((clip.at || 0) * timelinePxPerSec) + 1;
        const card = document.createElement('div');
        card.className = 'timeline-clip-card ' + (isText ? 'text-clip-card' : 'overlay-clip-card') + (clip.id === selectedTextClipId ? ' active-clip' : '');
        card.style.left = leftPx + 'px';
        card.style.width = widthPx + 'px';
        card.dataset.textClipId = clip.id;
        const label = isText
          ? `📝 ${(clip.text || '').slice(0, 22) || '(trống)'}`
          : `🖼️ ${clip.name || 'ảnh'}`;
        card.innerHTML = `
          <div class="clip-trim-handle handle-left" data-thandle="left" data-text-clip-id="${clip.id}" title="Kéo đổi thời lượng"></div>
          <div class="clip-card-inner"><span class="clip-num-badge">${isText ? '📝' : '🖼️'}</span><span class="clip-dur-text" style="overflow:hidden;">${label} (${dur.toFixed(1)}s)</span></div>
          <button type="button" class="btn-clip-mini" style="color:#f87171;" title="Xóa" onclick="event.stopPropagation(); deleteTextClipById('${clip.id}')">🗑️</button>
          <div class="clip-trim-handle handle-right" data-thandle="right" data-text-clip-id="${clip.id}" title="Kéo đổi thời lượng"></div>
        `;

        card.addEventListener('mousedown', (e) => {
          if (e.target.closest('.clip-trim-handle') || e.target.closest('.btn-clip-mini')) return;
          e.stopPropagation();
          e.preventDefault();
          selectTextClip(clip.id);
          pushTimelineHistory('Di chuyển chữ/logo trên timeline');
          const startX = e.clientX;
          const at0 = clip.at || 0;
          const onMove = (ev) => {
            clip.at = Math.max(0, snapTimelineTime(at0 + (ev.clientX - startX) / timelinePxPerSec, null));
            renderTextOverlayTrack();
            renderTextOverlayPreview();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            hideSnapGuide();
            updateTextPropsPanel();
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });

        card.addEventListener('click', (e) => {
          if (e.target.closest('.clip-trim-handle') || e.target.closest('.btn-clip-mini')) return;
          e.stopPropagation();
          selectTextClip(clip.id);
        });

        container.appendChild(card);
      });

      container.querySelectorAll('[data-thandle]').forEach(h => {
        h.addEventListener('mousedown', (e) => {
          e.stopPropagation();
          e.preventDefault();
          const clip = getAllTextClips().find(c => c.id === h.dataset.textClipId);
          if (!clip) return;
          selectTextClip(clip.id);
          pushTimelineHistory('Đổi thời lượng chữ/logo');
          const startX = e.clientX;
          const dur0 = Math.max(0.1, clip.dur || 3);
          const at0 = clip.at || 0;
          const isLeft = h.dataset.thandle === 'left';
          const onMove = (ev) => {
            const d = (ev.clientX - startX) / timelinePxPerSec;
            if (isLeft) {
              const newAt = Math.max(0, at0 + d);
              clip.dur = Math.max(0.3, dur0 - (newAt - at0));
              clip.at = newAt;
            } else {
              clip.dur = Math.max(0.3, dur0 + d);
            }
            renderTextOverlayTrack();
            renderTextOverlayPreview();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            updateTextPropsPanel();
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
      });
    }

    function renderTextOverlayPreview() {
      const layer = document.getElementById('textOverlayPreviewLayer');
      if (!layer) return;
      layer.innerHTML = '';
      const disp = getVideoDisplayRect();
      if (!disp) return;
      const layerRect = layer.getBoundingClientRect();
      const tlT = getEditedTimelineTime();
      const scaleF = disp.width / 1080; // fontSize trong style đặt theo canvas 1080px
      getAllTextClips().forEach(clip => {
        const at = clip.at || 0;
        const dur = Math.max(0.1, clip.dur || 3);
        if (tlT < at - 0.01 || tlT > at + dur + 0.01) return; // chỉ hiện khi playhead đang trong clip
        const isText = String(clip.id).startsWith('txt_');
        let el = null;
        if (isText) {
          const st = clip.style || {};
          el = document.createElement('div');
          el.className = 'top-text-box';
          el.innerText = clip.text || '';
          el.style.fontSize = Math.max(8, (st.fontSize || 64) * scaleF) + 'px';
          el.style.color = st.color || '#FFFFFF';
          el.style.fontWeight = st.bold ? '800' : '600';
          const ow = st.outlineWidth === undefined ? 3 : st.outlineWidth;
          el.style.webkitTextStroke = `${Math.max(0.4, ow * scaleF * 0.6)}px ${st.outlineColor || '#000000'}`;
          el.style.textShadow = `0 0 ${2 * scaleF}px ${st.outlineColor || '#000000'}`;
          if ((st.bgOpacity || 0) > 0.01) {
            el.style.background = `rgba(0,0,0,${st.bgOpacity})`;
            el.style.padding = `${4 * scaleF}px ${10 * scaleF}px`;
            el.style.borderRadius = `${4 * scaleF}px`;
          }
        } else {
          el = document.createElement('img');
          el.className = 'top-overlay-img';
          el.src = clip.src || '';
          el.style.opacity = (clip.opacity === undefined ? 1 : clip.opacity);
          el.onload = () => {
            el.style.width = Math.max(4, el.naturalWidth * (clip.scale || 1) * scaleF) + 'px';
            el.style.left = (disp.left - layerRect.left + (clip.pos?.x ?? 0.5) * disp.width) + 'px';
          };
        }
        if (!el) return;
        el.style.left = (disp.left - layerRect.left + (clip.pos?.x ?? 0.5) * disp.width) + 'px';
        el.style.top = (disp.top - layerRect.top + (clip.pos?.y ?? 0.8) * disp.height) + 'px';
        el.addEventListener('mousedown', (e) => {
          e.preventDefault();
          e.stopPropagation();
          selectTextClip(clip.id);
          pushTimelineHistory('Kéo vị trí chữ/logo trên video');
          const startX = e.clientX, startY = e.clientY;
          const p0 = { x: clip.pos?.x ?? 0.5, y: clip.pos?.y ?? 0.8 };
          const onMove = (ev) => {
            clip.pos = {
              x: Math.max(0, Math.min(1, p0.x + (ev.clientX - startX) / disp.width)),
              y: Math.max(0, Math.min(1, p0.y + (ev.clientY - startY) / disp.height))
            };
            renderTextOverlayPreview();
            updateTextPropsPanel();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
        layer.appendChild(el);
      });
    }

    const _tpEditPushed = {};
    function ensureTextPropHistory(key, label) {
      if (!_tpEditPushed[key]) { pushTimelineHistory(label); _tpEditPushed[key] = true; }
    }
    function commitTextPropEdit(key) { _tpEditPushed[key] = false; }

    // ========================================================
    // P5: TRACK NHẠC & SFX (multi-layer audio) + WAVEFORM
    // ========================================================
    function addMusicClipFromFile(file) {
      const track = getMusicTrack();
      if (!track || !file) return;
      if (file.size > 8 * 1024 * 1024) {
        showToast('⚠️ File nhạc quá lớn (>8MB). Hãy dùng bản nén/ngắn hơn!');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const src = String(reader.result || '');
        // Đọc thời lượng file nhạc qua Audio element (fallback 10s nếu đọc không được)
        const probe = new Audio();
        let settled = false;
        const create = (dur) => {
          if (settled) return;
          settled = true;
          pushTimelineHistory('Thêm nhạc/SFX');
          const clip = {
            id: 'mus_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
            src: src,
            name: file.name,
            at: Math.max(0, getEditedTimelineTime()),
            dur: Math.max(0.3, Math.min(dur, 600)),
            volume: 1,
            fadeIn: 0,
            fadeOut: 0,
            duck: false,
            muted: false
          };
          track.clips.push(clip);
          selectTextClip(clip.id);
          renderMusicTrack();
          showToast(`🎵 Đã thêm nhạc "${file.name}" (${clip.dur.toFixed(1)}s) — kéo trên timeline để đặt vị trí`);
        };
        probe.addEventListener('loadedmetadata', () => create(isFinite(probe.duration) && probe.duration > 0 ? probe.duration : 10));
        probe.addEventListener('error', () => create(10));
        setTimeout(() => create(10), 2500);
        probe.src = src;
      };
      reader.readAsDataURL(file);
    }

    function deleteMusicClipById(clipId) {
      const track = getMusicTrack();
      if (!track || !track.clips.some(c => c.id === clipId)) return;
      pushTimelineHistory('Xóa nhạc/SFX');
      track.clips = track.clips.filter(c => c.id !== clipId);
      if (selectedTextClipId === clipId) selectedTextClipId = null;
      renderMusicTrack();
      updateTextPropsPanel();
    }

    function renderMusicTrack() {
      const container = document.getElementById('musicTrack');
      if (!container) return;
      container.innerHTML = '';
      const clips = getMusicTrack()?.clips || [];
      const badge = document.getElementById('badgeMusicClipsCount');
      if (badge) badge.innerText = `${clips.length} clip`;
      clips.forEach((clip) => {
        const dur = Math.max(0.1, clip.dur || 3);
        const widthPx = Math.max(3, Math.round(dur * timelinePxPerSec) - 2);
        const leftPx = Math.round((clip.at || 0) * timelinePxPerSec) + 1;
        const card = document.createElement('div');
        card.className = 'timeline-clip-card music-clip-card' + (clip.id === selectedTextClipId ? ' active-clip' : '');
        card.style.left = leftPx + 'px';
        card.style.width = widthPx + 'px';
        card.dataset.musicClipId = clip.id;
        const duckTag = clip.duck ? ' 🦆' : '';
        card.innerHTML = `
          <div class="clip-trim-handle handle-left" data-mhandle="left" data-music-clip-id="${clip.id}" title="Kéo đổi thời lượng"></div>
          <div class="clip-card-inner"><span class="clip-num-badge">${clip.muted ? '🔇' : '🎵'}</span><span class="clip-dur-text" style="overflow:hidden;">${(clip.name || 'nhạc').slice(0, 20)}${duckTag} (${dur.toFixed(1)}s)</span></div>
          <button type="button" class="btn-clip-mini" style="color:#f87171;" title="Xóa" onclick="event.stopPropagation(); deleteMusicClipById('${clip.id}')">🗑️</button>
          <div class="clip-trim-handle handle-right" data-mhandle="right" data-music-clip-id="${clip.id}" title="Kéo đổi thời lượng"></div>
        `;
        card.addEventListener('mousedown', (e) => {
          if (e.target.closest('.clip-trim-handle') || e.target.closest('.btn-clip-mini')) return;
          e.stopPropagation();
          e.preventDefault();
          selectTextClip(clip.id);
          pushTimelineHistory('Di chuyển nhạc trên timeline');
          const startX = e.clientX;
          const at0 = clip.at || 0;
          const onMove = (ev) => {
            clip.at = Math.max(0, snapTimelineTime(at0 + (ev.clientX - startX) / timelinePxPerSec, null));
            renderMusicTrack();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            hideSnapGuide();
            updateTextPropsPanel();
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
        card.addEventListener('click', (e) => {
          if (e.target.closest('.clip-trim-handle') || e.target.closest('.btn-clip-mini')) return;
          e.stopPropagation();
          selectTextClip(clip.id);
        });
        container.appendChild(card);
      });
      container.querySelectorAll('[data-mhandle]').forEach(h => {
        h.addEventListener('mousedown', (e) => {
          e.stopPropagation();
          e.preventDefault();
          const clip = (getMusicTrack()?.clips || []).find(c => c.id === h.dataset.musicClipId);
          if (!clip) return;
          selectTextClip(clip.id);
          pushTimelineHistory('Đổi thời lượng nhạc');
          const startX = e.clientX;
          const dur0 = Math.max(0.1, clip.dur || 3);
          const at0 = clip.at || 0;
          const isLeft = h.dataset.mhandle === 'left';
          const onMove = (ev) => {
            const d = (ev.clientX - startX) / timelinePxPerSec;
            if (isLeft) {
              const newAt = Math.max(0, at0 + d);
              clip.dur = Math.max(0.3, dur0 - (newAt - at0));
              clip.at = newAt;
            } else {
              clip.dur = Math.max(0.3, dur0 + d);
            }
            renderMusicTrack();
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            updateTextPropsPanel();
          };
          window.addEventListener('mousemove', onMove);
          window.addEventListener('mouseup', onUp);
        });
      });
    }

    // ===== WAVEFORM (P5): peaks cache từ backend, vẽ theo zoom + timeline đã ghép =====
    let _waveformCache = null; // {stem, version, duration, peaks}

    async function loadAudioWaveform() {
      try {
        if (!currentVideo || !currentVideo.stem) return;
        const version = currentVideoVersion || 'final';
        const res = await fetch(`/api/workflow/audio/waveform?stem=${encodeURIComponent(currentVideo.stem)}&version=${version}&buckets=1600`);
        const data = await res.json();
        if (data && data.exists) {
          _waveformCache = { stem: currentVideo.stem, version, duration: data.duration || 0, peaks: data.peaks || [] };
        } else {
          _waveformCache = null;
        }
      } catch (err) {
        _waveformCache = null;
      }
      drawAudioWaveform();
    }

    function drawAudioWaveform() {
      const canvas = document.getElementById('audioWaveformCanvas');
      if (!canvas) return;
      const content = canvas.parentElement;
      const width = content ? content.clientWidth : 0;
      const height = content ? content.clientHeight : 30;
      if (width <= 0) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      if (!_waveformCache || !_waveformCache.peaks.length) return;
      const peaks = _waveformCache.peaks;
      const n = peaks.length;
      const span = getTimelineSpan() || _waveformCache.duration || 1;
      ctx.fillStyle = 'rgba(56, 189, 248, 0.55)';
      const barW = Math.max(1, width / n);
      for (let i = 0; i < n; i++) {
        const srcT = (i / n) * _waveformCache.duration; // peak thứ i ứng với giây NGUỒN
        const tlT = sourceTToTimelineTGlobal(srcT);      // map qua timeline đã ghép (speed/gap-aware)
        const x = (tlT / span) * width;
        const h = Math.max(1, peaks[i] * (height - 4));
        ctx.fillRect(x, (height - h) / 2, Math.max(1, barW * 0.85), h);
      }
    }

    function initMusicTrackUI() {
      const $ = (id) => document.getElementById(id);
      if (!$('btnAddMusicClip') || $('btnAddMusicClip').dataset.mWired) return;
      $('btnAddMusicClip').dataset.mWired = '1';
      $('btnAddMusicClip').addEventListener('click', () => $('tpMusicFileInput').click());
      $('tpMusicFileInput').addEventListener('change', (e) => {
        const f = e.target.files && e.target.files[0];
        addMusicClipFromFile(f);
        e.target.value = '';
      });
      // Kéo thả file nhạc thẳng vào track
      const zone = $('musicTrackContent');
      if (zone) {
        ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files')) {
            zone.classList.add('drag-over-zone');
          }
        }));
        ['dragleave', 'dragend'].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove('drag-over-zone')));
        zone.addEventListener('drop', (e) => {
          e.preventDefault();
          e.stopPropagation();
          zone.classList.remove('drag-over-zone');
          const files = [...((e.dataTransfer && e.dataTransfer.files) || [])].filter(f => /^audio\//.test(f.type) || /\.(mp3|wav|ogg|m4a|aac)$/i.test(f.name));
          files.slice(0, 5).forEach(f => addMusicClipFromFile(f));
          if (files.length > 5) showToast(`⚠️ Chỉ thêm 5 file đầu trong ${files.length} file`);
        });
      }
      // Panel nhạc
      $('tpVolume').addEventListener('input', () => {
        const clip = getSelectedTextClip();
        if (!clip || !String(clip.id).startsWith('mus_')) return;
        ensureTextPropHistory('tpVolume', 'Đổi âm lượng nhạc');
        clip.volume = (parseFloat($('tpVolume').value) || 100) / 100;
        $('tpVolumeVal').innerText = Math.round(clip.volume * 100) + '%';
      });
      $('tpVolume').addEventListener('change', () => commitTextPropEdit('tpVolume'));
      $('tpMFadeIn').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip || !String(clip.id).startsWith('mus_')) return;
        pushTimelineHistory('Đổi fade vào nhạc');
        clip.fadeIn = Math.max(0, parseFloat(e.target.value) || 0);
      });
      $('tpMFadeOut').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip || !String(clip.id).startsWith('mus_')) return;
        pushTimelineHistory('Đổi fade ra nhạc');
        clip.fadeOut = Math.max(0, parseFloat(e.target.value) || 0);
      });
      $('tpDuck').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip || !String(clip.id).startsWith('mus_')) return;
        pushTimelineHistory(e.target.checked ? 'Bật ducking nhạc' : 'Tắt ducking nhạc');
        clip.duck = e.target.checked;
        renderMusicTrack();
      });
      window.addEventListener('resize', () => drawAudioWaveform());
    }

    function updateTextPropsPanel() {
      const empty = document.getElementById('tpEmptyMsg');
      const body = document.getElementById('tpBody');
      if (!empty || !body) return;
      const clip = getSelectedTextClip();
      if (!clip) { empty.style.display = 'block'; body.style.display = 'none'; return; }
      empty.style.display = 'none';
      body.style.display = 'flex';
      const isText = String(clip.id).startsWith('txt_');
      const isMusic = String(clip.id).startsWith('mus_');
      document.getElementById('tpClipTitle').innerText = isText ? '📝 Chữ' : (isMusic ? '🎵 Nhạc/SFX' : '🖼️ Logo/Ảnh');
      document.getElementById('tpClipInfo').innerText = `tại ${formatTime(clip.at || 0)} · ${(clip.dur || 3).toFixed(1)}s` + (isMusic && clip.name ? ` · ${clip.name}` : '');
      document.getElementById('tpTextSection').style.display = isText ? 'block' : 'none';
      document.getElementById('tpStyleSection').style.display = isText ? 'block' : 'none';
      document.getElementById('tpAnimSection').style.display = isMusic ? 'none' : 'flex';
      document.getElementById('tpMusicSection').style.display = isMusic ? 'flex' : 'none';
      document.getElementById('tpScaleWrap').style.display = (!isText && !isMusic) ? 'inline-flex' : 'none';
      document.getElementById('tpOpacityWrap').style.display = (!isText && !isMusic) ? 'inline-flex' : 'none';
      if (isMusic) {
        const vol = (typeof clip.volume === 'number') ? clip.volume : 1;
        document.getElementById('tpVolume').value = Math.round(vol * 100);
        document.getElementById('tpVolumeVal').innerText = Math.round(vol * 100) + '%';
        document.getElementById('tpMFadeIn').value = clip.fadeIn || 0;
        document.getElementById('tpMFadeOut').value = clip.fadeOut || 0;
        document.getElementById('tpDuck').checked = !!clip.duck;
      } else if (isText) {
        const st = clip.style || {};
        document.getElementById('tpText').value = clip.text || '';
        document.getElementById('tpFontSize').value = st.fontSize || 64;
        document.getElementById('tpFontSizeVal').innerText = String(Math.round(st.fontSize || 64));
        document.getElementById('tpColor').value = st.color || '#FFEB3B';
        document.getElementById('tpOutlineColor').value = st.outlineColor || '#000000';
        document.getElementById('tpOutlineWidth').value = st.outlineWidth === undefined ? 3 : st.outlineWidth;
        document.getElementById('tpBold').checked = !!st.bold;
        document.getElementById('tpBgOpacity').value = Math.round((st.bgOpacity || 0) * 100);
        document.getElementById('tpBgOpacityVal').innerText = Math.round((st.bgOpacity || 0) * 100) + '%';
      } else {
        document.getElementById('tpScale').value = clip.scale === undefined ? 1 : clip.scale;
        document.getElementById('tpOpacity').value = clip.opacity === undefined ? 1 : clip.opacity;
      }
      document.getElementById('tpAnimIn').value = clip.animIn || 'none';
      document.getElementById('tpAnimOut').value = clip.animOut || 'none';
      document.getElementById('tpAt').value = +(clip.at || 0).toFixed(2);
      document.getElementById('tpDur').value = +(clip.dur || 3).toFixed(2);
    }

    function initTextTrackUI() {
      const $ = (id) => document.getElementById(id);
      if (!$('btnAddTextClip') || $('btnAddTextClip').dataset.tpWired) return;
      $('btnAddTextClip').dataset.tpWired = '1';
      $('btnAddTextClip').addEventListener('click', addTextClipAtPlayhead);
      $('btnAddOverlayClip').addEventListener('click', () => $('tpFileInput').click());
      $('tpFileInput').addEventListener('change', (e) => {
        const f = e.target.files && e.target.files[0];
        addOverlayClipFromFile(f);
        e.target.value = '';
      });

      $('tpText').addEventListener('input', () => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        ensureTextPropHistory('tpText', 'Sửa chữ');
        clip.text = $('tpText').value;
        renderTextOverlayTrack();
        renderTextOverlayPreview();
      });
      $('tpText').addEventListener('change', () => commitTextPropEdit('tpText'));

      document.querySelectorAll('#tabTextProps [data-tpreset]').forEach(b => {
        b.addEventListener('click', () => {
          const clip = getSelectedTextClip();
          if (!clip) return;
          pushTimelineHistory('Đổi preset kiểu chữ');
          clip.style = { ...(clip.style || {}), ...TEXT_PRESETS[b.dataset.tpreset] };
          renderTextOverlayTrack();
          renderTextOverlayPreview();
          updateTextPropsPanel();
        });
      });

      $('tpFontSize').addEventListener('input', () => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        ensureTextPropHistory('tpFs', 'Đổi cỡ chữ');
        clip.style = { ...(clip.style || {}), fontSize: parseFloat($('tpFontSize').value) || 64 };
        $('tpFontSizeVal').innerText = String(Math.round(clip.style.fontSize));
        renderTextOverlayPreview();
      });
      $('tpFontSize').addEventListener('change', () => commitTextPropEdit('tpFs'));

      ['tpColor', 'tpOutlineColor'].forEach(id => {
        $(id).addEventListener('input', () => {
          const clip = getSelectedTextClip();
          if (!clip) return;
          ensureTextPropHistory('tpColor', 'Đổi màu chữ');
          clip.style = { ...(clip.style || {}) };
          if (id === 'tpColor') clip.style.color = $(id).value;
          else clip.style.outlineColor = $(id).value;
          renderTextOverlayPreview();
        });
        $(id).addEventListener('change', () => { commitTextPropEdit('tpColor'); renderTextOverlayTrack(); });
      });

      $('tpOutlineWidth').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi độ dày viền');
        clip.style = { ...(clip.style || {}), outlineWidth: Math.max(0, parseFloat(e.target.value) || 0) };
        renderTextOverlayPreview();
      });
      $('tpBold').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi đậm/nhạt');
        clip.style = { ...(clip.style || {}), bold: e.target.checked };
        renderTextOverlayPreview();
      });
      $('tpBgOpacity').addEventListener('input', () => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        ensureTextPropHistory('tpBg', 'Đổi nền mờ');
        clip.style = { ...(clip.style || {}), bgOpacity: (parseFloat($('tpBgOpacity').value) || 0) / 100 };
        $('tpBgOpacityVal').innerText = Math.round((clip.style.bgOpacity || 0) * 100) + '%';
        renderTextOverlayPreview();
      });
      $('tpBgOpacity').addEventListener('change', () => commitTextPropEdit('tpBg'));

      $('tpAnimIn').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi animation vào');
        clip.animIn = e.target.value;
      });
      $('tpAnimOut').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi animation ra');
        clip.animOut = e.target.value;
      });
      $('tpAt').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi vị trí chữ/logo');
        clip.at = Math.max(0, parseFloat(e.target.value) || 0);
        renderTextOverlayTrack();
        renderTextOverlayPreview();
      });
      $('tpDur').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi thời lượng chữ/logo');
        clip.dur = Math.max(0.3, parseFloat(e.target.value) || 3);
        renderTextOverlayTrack();
        renderTextOverlayPreview();
      });
      $('tpScale').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi cỡ logo');
        clip.scale = Math.max(0.05, Math.min(3, parseFloat(e.target.value) || 1));
        renderTextOverlayPreview();
      });
      $('tpOpacity').addEventListener('change', (e) => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đổi độ mờ logo');
        clip.opacity = Math.max(0, Math.min(1, parseFloat(e.target.value) === 0 ? 0 : parseFloat(e.target.value) || 1));
        renderTextOverlayPreview();
      });
      $('tpCenter').addEventListener('click', () => {
        const clip = getSelectedTextClip();
        if (!clip) return;
        pushTimelineHistory('Đưa giữa màn hình');
        clip.pos = { x: 0.5, y: 0.5 };
        renderTextOverlayPreview();
        updateTextPropsPanel();
      });
      $('tpDelete').addEventListener('click', () => {
        if (selectedTextClipId) deleteTextClipById(selectedTextClipId);
      });

      window.addEventListener('resize', () => renderTextOverlayPreview());
    }

    function copySelectedClip() {
      if (!videoClipsState || !videoClipsState.length) return;
      if (!selectedClipId || !videoClipsState.some(c => c.id === selectedClipId)) {
        selectedClipId = videoClipsState[0].id;
      }
      const clipIdx = videoClipsState.findIndex(c => c.id === selectedClipId);
      if (clipIdx === -1) return;
      const clip = videoClipsState[clipIdx];
      copiedClipBuffer = {
        start: clip.start,
        end: clip.end,
        duration: clip.end - clip.start,
        sourceIndex: clipIdx + 1
      };
      updateTimelineToolbarStates();
      // Hiệu ứng nháy nhẹ trên đoạn vừa copy
      const cardEl = document.querySelector(`#videoClipsContainer .timeline-clip-card[data-clip-id="${selectedClipId}"]`);
      if (cardEl) {
        cardEl.style.transition = 'transform 0.15s ease, box-shadow 0.15s ease';
        cardEl.style.transform = 'scale(0.97)';
        setTimeout(() => { cardEl.style.transform = ''; }, 160);
      }
      showToast(`📄 Đã sao chép Đoạn ${clipIdx + 1} (${formatTime(clip.start)} - ${formatTime(clip.end)})! Nhấn Ctrl+V để dán.`);
    }

    function pasteCopiedClip() {
      if (!videoClipsState || !videoClipsState.length) return;
      if (!copiedClipBuffer) {
        // Nếu người dùng chưa bấm Ctrl+C lần nào mà bấm Ctrl+V luôn, tự động copy đoạn đang chọn để dán tiện lợi
        copySelectedClip();
        if (!copiedClipBuffer) return;
      }

      pushTimelineHistory('Dán đoạn clip');

      const newClip = {
        id: 'clip_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
        start: copiedClipBuffer.start,
        end: copiedClipBuffer.end,
        duration: copiedClipBuffer.end - copiedClipBuffer.start
      };

      const insertIdx = videoClipsState.findIndex(c => c.id === selectedClipId);
      if (insertIdx === -1) {
        rippleInsertClip(videoClipsState, getTimelineSpan(), newClip);
      } else {
        const anchor = videoClipsState[insertIdx];
        rippleInsertClip(videoClipsState, (anchor.at || 0) + clipDurOf(anchor), newClip);
      }

      setSingleSelection(newClip.id);
      renderVideoClipsTrack();
      updateTimelineToolbarStates();
      const newIdx = videoClipsState.findIndex(c => c.id === newClip.id);
      showToast(`📋 Đã dán đoạn video (${(newClip.end - newClip.start).toFixed(1)}s) vào vị trí Đoạn ${newIdx + 1} (Ctrl+V)!`);
    }

    function initVideoClipsForCurrentVideo(forceReset = false, opts = null) {
      if (!mainPlayer) return;
      const dur = mainPlayer.duration;
      if (isNaN(dur) || dur <= 0) return;

      if (forceReset || !videoClipsState || !videoClipsState.length) {
        videoClipsState = [
          {
            id: 'clip_' + Date.now(),
            start: 0,
            end: dur,
            at: 0,
            duration: dur
          }
        ];
        setSingleSelection(videoClipsState[0].id);
        timelineUndoStack = [];
        timelineRedoStack = [];
        if (opts && opts.saveFresh) {
          // Sau apply-edits: video thành phẩm mới → project.json cũ lỗi thời, ghi đè bằng trạng thái hiện tại
          timelineProjectDirty = true;
          saveTimelineProjectNow(true);
          loadAudioWaveform(); // P5: audio nguồn đổi (video mới) → tính lại waveform
          _proxyActiveStem = null; // P6.2: proxy cũ lỗi thời
          maybeSwitchToProxy();
        } else if (opts && opts.restore) {
          // P6.1: chọn video → thử khôi phục project đã autosave (khôi phục sau crash/đóng trình duyệt)
          maybeRestoreTimelineProject(currentVideo && currentVideo.stem);
          loadAudioWaveform(); // P5: vẽ waveform cho video vừa chọn
          _proxyRequestedStem = currentVideo ? currentVideo.stem : null;
          maybeSwitchToProxy(); // P6.2: dùng proxy 540p nếu có sẵn / tạo nền
        }
      }
      renderVideoClipsTrack();
      updateTimelineToolbarStates();
    }

    function renderVideoClipsTrack() {
      const container = document.getElementById('videoClipsContainer');
      const summaryEl = document.getElementById('timelineClipsSummary');
      const badgeCount = document.getElementById('badgeVideoClipsCount');
      if (!container) return;

      if (!videoClipsState || !videoClipsState.length) {
        const dur = (mainPlayer && !isNaN(mainPlayer.duration) && mainPlayer.duration > 0) ? mainPlayer.duration : 0;
        if (dur > 0) {
          videoClipsState = [{ id: 'clip_' + Date.now(), start: 0, end: dur, at: 0, duration: dur }];
          setSingleSelection(videoClipsState[0].id);
        } else {
          container.innerHTML = '<div style="display:flex;align-items:center;padding:0 10px;font-size:11px;color:#94a3b8;">Đang tải video...</div>';
          return;
        }
      }

      if (!selectedClipId || !videoClipsState.some(c => c.id === selectedClipId)) {
        setSingleSelection(videoClipsState[0].id);
      }

      // Chiều rộng track + ruler theo zoom hiện tại
      applyZoomLayout();

      const totalSpan = getTimelineSpan();
      const selCount = selectedClipIds.length;
      if (summaryEl) summaryEl.innerText = selCount > 1
        ? `🎬 ${videoClipsState.length} Đoạn (chọn ${selCount}) | ⏱️ ${formatTime(totalSpan)}`
        : `🎬 ${videoClipsState.length} Đoạn | ⏱️ ${formatTime(totalSpan)}`;
      if (badgeCount) badgeCount.innerText = `${videoClipsState.length} clip${videoClipsState.length > 1 ? 's' : ''}`;

      container.innerHTML = '';

      videoClipsState.forEach((clip, idx) => {
        const clipDur = clipDurOf(clip); // thời lượng TRÊN TIMELINE (đã chia speed + freeze)
        const widthPx = Math.max(3, Math.round(clipDur * timelinePxPerSec) - 2);
        const leftPx = Math.round((clip.at || 0) * timelinePxPerSec) + 1;
        const isActive = (clip.id === selectedClipId);
        const isMulti = (!isActive && selectedClipIds.includes(clip.id));
        const isHook = (idx === 0 && videoClipsState.length > 1);
        const isCompact = widthPx < 88;
        const isSuperCompact = widthPx < 44;

        const card = document.createElement('div');
        card.className = 'timeline-clip-card' + (isActive ? ' active-clip' : '') + (isMulti ? ' multi-selected' : '');
        card.style.left = leftPx + 'px';
        card.style.width = widthPx + 'px';
        card.dataset.clipId = clip.id;
        card.draggable = true;

        card.innerHTML = `
          <div class="clip-trim-handle handle-left" title="Kéo để chỉnh điểm bắt đầu (Trim In) — dính snap nếu bật 🧲" data-handle="left" data-clip-id="${clip.id}"></div>

          <div class="clip-card-inner">
            <span class="clip-num-badge ${isHook ? 'hook-badge' : ''}">${isHook ? (isCompact ? '🧲 Hook' : '🧲 Hook (Đoạn 1)') : (isSuperCompact ? `${idx + 1}` : `Đoạn ${idx + 1}`)}</span>
            ${!isSuperCompact ? `<span class="clip-dur-text">${isCompact ? `${clipDur.toFixed(1)}s` : `${formatTime(clip.start)} - ${formatTime(clip.end)} (${clipDur.toFixed(1)}s)`}</span>` : ''}
          </div>

          <div class="clip-quick-actions" style="${isSuperCompact ? 'display: none;' : ''}">
            ${(!isHook && !isCompact) ? `<button type="button" class="btn-clip-mini" title="Đưa đoạn này lên đầu làm Hook (00:00)" onclick="event.stopPropagation(); moveClipToHook('${clip.id}')">🧲</button>` : ''}
            <button type="button" class="btn-clip-mini" title="Cắt tại giây hiện tại (Phím C)" onclick="event.stopPropagation(); splitClipAtCurrent('${clip.id}')">✂️</button>
            ${!isCompact ? `<button type="button" class="btn-clip-mini" title="Copy & Dán nhân bản đoạn này (Ctrl+C / Ctrl+V)" onclick="event.stopPropagation(); duplicateClipById('${clip.id}')">📋</button>` : ''}
            ${videoClipsState.length > 1 ? `<button type="button" class="btn-clip-mini" style="color:#f87171;" title="Xóa đoạn này (Delete: rip-nhịp)" onclick="event.stopPropagation(); deleteClipById('${clip.id}')">🗑️</button>` : ''}
          </div>

          <div class="clip-trim-handle handle-right" title="Kéo để chỉnh điểm kết thúc (Trim Out) — dính snap nếu bật 🧲" data-handle="right" data-clip-id="${clip.id}"></div>
        `;

        card.onclick = (e) => {
          if (e.target.closest('.clip-trim-handle') || e.target.closest('.btn-clip-mini')) return;
          e.stopPropagation();
          if (e.ctrlKey || e.metaKey || e.shiftKey) {
            toggleMultiSelect(clip.id, e.shiftKey && !e.ctrlKey && !e.metaKey);
            return;
          }
          clearMultiSelect();
          const cardRect = card.getBoundingClientRect();
          const relRatio = cardRect.width > 0 ? Math.max(0, Math.min(1, (e.clientX - cardRect.left) / cardRect.width)) : 0;
          const clickedClipTime = clip.start + relRatio * (clip.end - clip.start);
          selectClip(clip.id, clickedClipTime);
        };

        // Kéo thả đổi thứ tự các đoạn video chuẩn CapCut (hỗ trợ chèn trước / sau linh hoạt)
        card.ondragstart = (e) => {
          e.dataTransfer.setData('text/plain', clip.id);
          card.classList.add('dragging');
        };
        card.ondragover = (e) => {
          e.preventDefault();
          const cardRect = card.getBoundingClientRect();
          const isLeftHalf = (e.clientX - cardRect.left) < cardRect.width / 2;
          if (isLeftHalf) {
            card.classList.add('drop-before');
            card.classList.remove('drop-after');
          } else {
            card.classList.add('drop-after');
            card.classList.remove('drop-before');
          }
        };
        card.ondragleave = () => {
          card.classList.remove('drop-before', 'drop-after');
        };
        card.ondrop = (e) => {
          e.preventDefault();
          const cardRect = card.getBoundingClientRect();
          const isLeftHalf = (e.clientX - cardRect.left) < cardRect.width / 2;
          card.classList.remove('drop-before', 'drop-after');
          const srcId = e.dataTransfer.getData('text/plain');
          if (srcId && srcId !== clip.id) {
            insertClipAt(srcId, clip.id, isLeftHalf ? 'before' : 'after');
          }
        };
        card.ondragend = () => {
          card.classList.remove('dragging');
          document.querySelectorAll('.timeline-clip-card').forEach(c => c.classList.remove('drop-before', 'drop-after', 'drag-over'));
        };

        container.appendChild(card);
      });

      // P4: badge chuyển cảnh tại điểm nối (bấm để sửa)
      container.parentElement.querySelectorAll('.trans-chip').forEach(b => b.remove());
      videoClipsState.forEach((clip, idx) => {
        if (!clip.trans || idx >= videoClipsState.length - 1) return;
        const next = videoClipsState[idx + 1];
        const chip = document.createElement('div');
        chip.className = 'trans-chip';
        chip.style.left = Math.max(0, Math.round((next.at || 0) * timelinePxPerSec) - 36) + 'px';
        chip.textContent = `🔄 ${clip.trans.type} ${(+(clip.trans.dur) || 0.5).toFixed(1)}s`;
        chip.title = 'Bấm để sửa chuyển cảnh trong panel Thuộc Tính Clip';
        chip.onclick = (e) => {
          e.stopPropagation();
          clearMultiSelect();
          selectClip(clip.id);
          openClipPropsTab();
        };
        container.parentElement.appendChild(chip);
      });

      renderTimelineRuler();
      initTrimHandleListeners();
      updateTimelineToolbarStates();
      updatePlayhead();
      updateClipPropsPanel();
      applyClipVisualPreview();
      renderTextOverlayTrack();
      renderTextOverlayPreview();
      renderMusicTrack();
    }

    function selectClip(clipId, seekToTime = null) {
      selectedClipId = clipId;
      if (!selectedClipIds.includes(clipId)) selectedClipIds = [clipId];
      lastMultiSelectAnchorId = clipId;
      selectedTextClipId = null; // chọn video clip → bỏ chọn chữ/logo
      document.querySelectorAll('#videoClipsContainer .timeline-clip-card').forEach(c => {
        c.classList.toggle('active-clip', c.dataset.clipId === clipId);
        c.classList.toggle('multi-selected', c.dataset.clipId !== clipId && selectedClipIds.includes(c.dataset.clipId));
      });
      const clip = videoClipsState.find(c => c.id === clipId);
      if (clip && mainPlayer) {
        previewClipAudio(clip); // P2: nghe thử âm lượng/mute theo đoạn đang chọn
        if (typeof seekToTime === 'number' && !isNaN(seekToTime)) {
          mainPlayer.currentTime = Math.max(clip.start, Math.min(clip.end, seekToTime));
          updateTimecode();
          updatePlayhead();
          updateScrubberUI();
        } else if (mainPlayer.currentTime < clip.start || mainPlayer.currentTime > clip.end) {
          mainPlayer.currentTime = clip.start;
          updateTimecode();
          updatePlayhead();
          updateScrubberUI();
        }
      }
    }

    function insertClipAt(srcId, targetId, position = 'before') {
      const srcIdx = videoClipsState.findIndex(c => c.id === srcId);
      const targetIdx0 = videoClipsState.findIndex(c => c.id === targetId);
      if (srcIdx === -1 || targetIdx0 === -1) return;

      pushTimelineHistory('Chèn đổi vị trí đoạn');

      const src = rippleRemoveClipIdx(videoClipsState, srcIdx);
      const target = videoClipsState.find(c => c.id === targetId);
      if (!target) {
        // Trường hợp hiếm: target không còn — trả src về cuối timeline
        rippleInsertClip(videoClipsState, getTimelineSpan(), src);
      } else {
        const insertAt = position === 'after' ? (target.at || 0) + clipDurOf(target) : (target.at || 0);
        rippleInsertClip(videoClipsState, insertAt, src);
      }
      setSingleSelection(src.id);
      const newIdx = videoClipsState.findIndex(c => c.id === src.id);
      renderVideoClipsTrack();

      if (newIdx === 0) {
        showToast('🧲 Đã đưa đoạn video lên ĐẦU VIDEO (HOOK) thành công!');
      } else {
        showToast(`🔀 Đã chèn đoạn video vào vị trí Đoạn ${newIdx + 1}!`);
      }
    }

    function reorderClips(srcId, targetId) {
      insertClipAt(srcId, targetId, 'before');
    }

    function moveClipToHook(clipId) {
      const idx = videoClipsState.findIndex(c => c.id === clipId);
      if (idx === -1) return;
      if (idx === 0 && (videoClipsState[0].at || 0) === 0) {
        showToast('ℹ️ Đoạn này đã nằm ở đầu video (Hook) rồi!');
        return;
      }
      pushTimelineHistory('Đưa đoạn lên đầu làm Hook');
      const moved = rippleRemoveClipIdx(videoClipsState, idx);
      rippleInsertClip(videoClipsState, 0, moved);
      setSingleSelection(moved.id);
      renderVideoClipsTrack();
      if (mainPlayer) {
        mainPlayer.currentTime = moved.start;
        updateTimecode();
        updatePlayhead();
      }
      showToast('🧲 Đã đưa đoạn này lên ĐẦU VIDEO (HOOK) mở đầu!');
    }

    function moveSelectedClipToHook() {
      if (!selectedClipId && videoClipsState.length) setSingleSelection(videoClipsState[0].id);
      if (!selectedClipId) return;
      moveClipToHook(selectedClipId);
    }

    function cloneSelectedClipToHook() {
      if (!selectedClipId && videoClipsState.length) setSingleSelection(videoClipsState[0].id);
      if (!selectedClipId) return;
      const idx = videoClipsState.findIndex(c => c.id === selectedClipId);
      if (idx === -1) return;

      pushTimelineHistory('Nhân bản làm Hook đầu video');
      const orig = videoClipsState[idx];
      const hookClip = {
        id: 'clip_hook_' + Date.now(),
        start: orig.start,
        end: orig.end,
        duration: orig.end - orig.start
      };
      rippleInsertClip(videoClipsState, 0, hookClip);
      setSingleSelection(hookClip.id);
      renderVideoClipsTrack();
      if (mainPlayer) {
        mainPlayer.currentTime = hookClip.start;
        updateTimecode();
        updatePlayhead();
      }
      showToast(`📌 Đã nhân bản đoạn (${(hookClip.end - hookClip.start).toFixed(1)}s) đưa lên làm HOOK đầu video (Giữ nguyên đoạn gốc)!`);
    }

    function splitCurrentClip() {
      if (!mainPlayer || !videoClipsState || !videoClipsState.length) return;
      const t = mainPlayer.currentTime;

      // Ưu tiên kiểm tra đoạn đang được chọn trước (nếu có nhiều đoạn trùng vùng thời gian do copy/paste)
      let clipIdx = videoClipsState.findIndex(c => c.id === selectedClipId && t > c.start + 0.12 && t < c.end - 0.12);
      if (clipIdx === -1) {
        clipIdx = videoClipsState.findIndex(c => t > c.start + 0.12 && t < c.end - 0.12);
      }

      if (clipIdx === -1) {
        showToast('⚠️ Hãy di chuyển thanh thời gian (Playhead) vào bên trong đoạn video (cách mép ít nhất 0.15s) rồi bấm phím C để cắt!');
        return;
      }

      pushTimelineHistory('Cắt đoạn video');

      const orig = videoClipsState[clipIdx];
      const nowTs = Date.now();
      const leftDurTl = (t - orig.start) / clipSpeedOf(orig) + (orig.freeze || 0);
      const inheritProps = {
        speed: orig.speed || 1, volume: orig.volume, muted: orig.muted, keepPitch: orig.keepPitch,
        fadeIn: orig.fadeIn, fadeOut: orig.fadeOut, crop: orig.crop, rotate: orig.rotate,
        flipH: orig.flipH, flipV: orig.flipV, reverse: orig.reverse, detachAudio: orig.detachAudio
      };
      // freeze-frame thuộc về đầu đoạn → chỉ đoạn trước giữ lại
      const clip1 = { id: 'clip_' + nowTs + '_a', start: orig.start, end: t, at: orig.at || 0, duration: t - orig.start, ...inheritProps, freeze: orig.freeze || 0 };
      const clip2 = { id: 'clip_' + nowTs + '_b', start: t, end: orig.end, at: (orig.at || 0) + leftDurTl, duration: orig.end - t, ...inheritProps, freeze: 0 };

      videoClipsState.splice(clipIdx, 1, clip1, clip2);
      setSingleSelection(clip2.id);
      renderVideoClipsTrack();
      showToast(`✂️ Đã cắt tách đoạn video tại ${formatTime(t)} (Phím C)! Bấm Ctrl+Z để hoàn tác.`);
    }

    function splitClipAtCurrent(clipId) {
      setSingleSelection(clipId);
      splitCurrentClip();
    }

    function duplicateSelectedClip() {
      if (!selectedClipId && videoClipsState.length) setSingleSelection(videoClipsState[0].id);
      if (!selectedClipId) return;
      duplicateClipById(selectedClipId);
    }

    function duplicateClipById(clipId) {
      const idx = videoClipsState.findIndex(c => c.id === clipId);
      if (idx === -1) return;

      pushTimelineHistory('Nhân bản đoạn');

      const orig = videoClipsState[idx];
      copiedClipBuffer = { start: orig.start, end: orig.end, duration: orig.end - orig.start, sourceIndex: idx + 1 };
      const newClip = {
        id: 'clip_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
        start: orig.start,
        end: orig.end,
        duration: orig.end - orig.start
      };
      rippleInsertClip(videoClipsState, (orig.at || 0) + clipDurOf(orig), newClip);
      setSingleSelection(newClip.id);
      renderVideoClipsTrack();
      showToast(`📋 Đã sao chép & dán đoạn video (${(newClip.end - newClip.start).toFixed(1)}s)!`);
    }

    function duplicateAllClips() {
      if (!videoClipsState.length) return;
      pushTimelineHistory('Nhân đôi toàn bộ video');
      const span = getTimelineSpan();
      const clones = videoClipsState.map(c => ({
        id: 'clip_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
        start: c.start,
        end: c.end,
        at: (c.at || 0) + span,
        duration: c.end - c.start
      }));
      videoClipsState = videoClipsState.concat(clones);
      sortClipsByAt(videoClipsState);
      renderVideoClipsTrack();
      showToast(`🎞️ Đã nhân đôi toàn bộ video (Tổng cộng ${videoClipsState.length} đoạn)!`);
    }

    function deleteSelectedClip(keepGap = false) {
      const ids = selectedClipIds.length ? [...selectedClipIds] : (selectedClipId ? [selectedClipId] : []);
      deleteClipsByIds(ids, keepGap);
    }

    function deleteClipById(clipId, keepGap = false) {
      // Nếu clip nằm trong một vùng multi-select đang bật: xóa cả nhóm
      const ids = (selectedClipIds.length > 1 && selectedClipIds.includes(clipId)) ? [...selectedClipIds] : [clipId];
      deleteClipsByIds(ids, keepGap);
    }

    function deleteClipsByIds(ids, keepGap) {
      if (!ids || !ids.length || !videoClipsState.length) return;
      if (videoClipsState.length - ids.length < 1) {
        showToast('⚠️ Không thể xóa hết mọi đoạn! Bạn có thể kéo mép 2 đầu để cắt ngắn đoạn này.');
        return;
      }
      pushTimelineHistory(keepGap ? 'Xóa đoạn để trống (Shift+Del)' : 'Xóa đoạn rip-nhịp (Del)');

      const removed = [];
      const firstRemovedAt = (() => {
        const c = videoClipsState.find(k => k.id === ids[0]);
        return c ? (c.at || 0) : 0;
      })();
      videoClipsState = videoClipsState.filter(c => {
        if (ids.includes(c.id)) { removed.push(c); return false; }
        return true;
      });
      if (!keepGap) {
        // Rip-nhịp: các đoạn bên phải mỗi vùng xóa dồn về trái
        removed.forEach(r => {
          const re = (r.at || 0) + clipDurOf(r);
          videoClipsState.forEach(k => {
            if ((k.at || 0) >= re - 1e-6) k.at = (k.at || 0) - clipDurOf(r);
          });
        });
      }
      sortClipsByAt(videoClipsState);

      const next = videoClipsState.find(c => (c.at || 0) >= firstRemovedAt - 1e-6) || videoClipsState[videoClipsState.length - 1];
      setSingleSelection(next.id);
      renderVideoClipsTrack();
      showToast(keepGap
        ? `🗑️ Đã xóa ${removed.length} đoạn và GIỮ khoảng trống trên timeline (Shift+Del). Del thường sẽ dồn đoạn về trái.`
        : `🗑️ Đã xóa ${removed.length} đoạn (rip-nhịp). Nhấn Ctrl+Z nếu muốn lấy lại!`);
    }

    function resetVideoClips() {
      if (videoClipsState.length > 0) {
        pushTimelineHistory('Khôi phục gốc');
      }
      const dur = (mainPlayer && !isNaN(mainPlayer.duration) && mainPlayer.duration > 0) ? mainPlayer.duration : 0;
      if (dur > 0) {
        videoClipsState = [{ id: 'clip_' + Date.now(), start: 0, end: dur, at: 0, duration: dur }];
        setSingleSelection(videoClipsState[0].id);
      }
      renderVideoClipsTrack();
      showToast('🔄 Đã khôi phục về 1 đoạn video gốc! (Bấm Ctrl+Z để hoàn tác)');
    }

    function initTrimHandleListeners() {
      const handles = document.querySelectorAll('.clip-trim-handle');
      handles.forEach(h => {
        h.onmousedown = (e) => {
          e.stopPropagation();
          e.preventDefault();
          const handleType = h.dataset.handle;
          const clipId = h.dataset.clipId;
          const clip = videoClipsState.find(c => c.id === clipId);
          if (!clip || !mainPlayer) return;

          pushTimelineHistory('Kéo chỉnh độ dài đoạn');

          const startClientX = e.clientX;
          const initAt = clip.at || 0;
          const initStart = clip.start;
          const initEnd = clip.end;
          const initDur = clipDurOf(clip); // thời lượng TRÊN TIMELINE để kẹp mép kéo
          const srcDur = (mainPlayer && !isNaN(mainPlayer.duration) && mainPlayer.duration > 0) ? mainPlayer.duration : Math.max(initEnd, 10);
          const wasPlaying = !mainPlayer.paused;
          if (wasPlaying) mainPlayer.pause();
          setSingleSelection(clipId);

          // Giới hạn kéo theo hàng xóm để không đè lên đoạn khác
          const sorted = [...videoClipsState].sort((a, b) => (a.at || 0) - (b.at || 0));
          const myIdx = sorted.findIndex(c => c.id === clipId);
          const prevEdge = myIdx > 0 ? (sorted[myIdx - 1].at || 0) + clipDurOf(sorted[myIdx - 1]) : 0;
          const nextEdge = myIdx < sorted.length - 1 ? (sorted[myIdx + 1].at || 0) : Infinity;

          let lastPreviewTs = 0;
          const previewTrim = (srcT) => {
            const now = performance.now();
            if (now - lastPreviewTs < 70) return;
            lastPreviewTs = now;
            try {
              mainPlayer.currentTime = Math.max(0, Math.min((mainPlayer.duration || srcT) - 0.05, srcT));
              updateTimecode();
            } catch (err) { /* bỏ qua lỗi seek khi video chưa sẵn sàng */ }
          };

          const onMouseMove = (moveE) => {
            const dt = (moveE.clientX - startClientX) / timelinePxPerSec;
            const spd = clipSpeedOf(clip);
            const initFreeze = clip.freeze || 0;

            if (handleType === 'left') {
              // Mép trái đi theo con trỏ (snap 8px); điểm in trượt theo đúng tỉ lệ speed
              let newAt = snapTimelineTime(initAt + dt, clipId);
              newAt = Math.max(prevEdge, Math.min(initAt + initDur - 0.2, newAt));
              let newStart = initStart + (newAt - initAt) * spd;
              newStart = Math.max(0, Math.min(initEnd - 0.2, newStart));
              clip.start = newStart;
              clip.at = initAt + (newStart - initStart) / spd;
              clip.duration = clip.end - clip.start;
              previewTrim(newStart + 0.02);
            } else if (handleType === 'right') {
              // Mép phải đi theo con trỏ (snap 8px); điểm out trượt theo tỉ lệ speed
              let newRight = snapTimelineTime(initAt + initDur + dt, clipId);
              const maxRight = Math.min(nextEdge, initAt + initFreeze + (srcDur - initStart) / spd);
              newRight = Math.max(initAt + 0.2, Math.min(maxRight, newRight));
              clip.end = Math.max(clip.start + 0.2, Math.min(srcDur, initStart + (newRight - initAt - initFreeze) * spd));
              clip.duration = clip.end - clip.start;
              previewTrim(clip.end - 0.02);
            }
            renderVideoClipsTrack();
          };

          const onMouseUp = () => {
            window.removeEventListener('mousemove', onMouseMove);
            window.removeEventListener('mouseup', onMouseUp);
            hideSnapGuide();
            if (wasPlaying) mainPlayer.play().catch(() => {});
            renderVideoClipsTrack();
            showToast(`✂️ Đã chỉnh độ dài đoạn: ${formatTime(clip.start)} - ${formatTime(clip.end)} (Ctrl+Z để hoàn tác)`);
          };

          window.addEventListener('mousemove', onMouseMove);
          window.addEventListener('mouseup', onMouseUp);
        };
      });
    }

    function handleSmartTimelinePlayback() {
      if (!mainPlayer || mainPlayer.paused || isHandlingSmartPlay) return;
      if (!videoClipsState || videoClipsState.length <= 1) return;

      const t = mainPlayer.currentTime;
      let currIdx = videoClipsState.findIndex(c => c.id === selectedClipId && t >= c.start && t < c.end - 0.08);
      if (currIdx === -1) {
        currIdx = videoClipsState.findIndex(c => t >= c.start && t < c.end - 0.08);
      }

      if (currIdx !== -1) {
        if (selectedClipId !== videoClipsState[currIdx].id) {
          selectedClipId = videoClipsState[currIdx].id;
          mainPlayer.playbackRate = clipSpeedOf(videoClipsState[currIdx]); // preview đúng tốc độ clip (P2)
          document.querySelectorAll('#videoClipsContainer .timeline-clip-card').forEach(c => {
            c.classList.toggle('active-clip', c.dataset.clipId === selectedClipId);
          });
        }
      } else {
        // Player chạy hết clip hiện tại: Tự động nhảy sang clip tiếp theo theo đúng thứ tự mảng
        let activeIdx = videoClipsState.findIndex(c => c.id === selectedClipId);
        if (activeIdx === -1) activeIdx = 0;

        const nextIdx = (activeIdx + 1 < videoClipsState.length) ? activeIdx + 1 : 0;
        isHandlingSmartPlay = true;
        selectedClipId = videoClipsState[nextIdx].id;
        mainPlayer.currentTime = videoClipsState[nextIdx].start;
        mainPlayer.playbackRate = clipSpeedOf(videoClipsState[nextIdx]); // preview đúng tốc độ clip (P2)
        warmPreloadForNextClip(nextIdx); // P6.2: làm ấm buffer vùng clip kế

        document.querySelectorAll('#videoClipsContainer .timeline-clip-card').forEach(c => {
          c.classList.toggle('active-clip', c.dataset.clipId === selectedClipId);
        });

        setTimeout(() => { isHandlingSmartPlay = false; }, 90);
      }
    }

    // ===== P6.3: RENDER NỀN — payload dùng chung + poll tiến độ + huỷ =====
    let _activeRenderJobId = null;
    const RENDER_TOKEN_HEADERS = () => ({
      'Content-Type': 'application/json',
      'X-Local-Control-Token': (typeof CONTROL_PLANE_TOKEN !== 'undefined' ? CONTROL_PLANE_TOKEN : '__REPLACE_TOKEN__'),
      'X-Dashboard-Input': '1'
    });

    function buildTimelineRenderPayload() {
      return {
        stem: currentVideo.stem,
        clips: videoClipsState.map(c => ({
          start: parseFloat(c.start.toFixed(3)),
          end: parseFloat(c.end.toFixed(3)),
          at: parseFloat((c.at || 0).toFixed(3)), // P4: vị trí timeline — backend map text/nhạc theo đây
          // P2: gửi kèm thuộc tính clip cho backend dựng filter
          speed: clipSpeedOf(c),
          volume: (typeof c.volume === 'number') ? parseFloat(c.volume.toFixed(3)) : 1,
          muted: !!c.muted,
          keepPitch: c.keepPitch !== false,
          fadeIn: parseFloat((c.fadeIn || 0).toFixed(3)),
          fadeOut: parseFloat((c.fadeOut || 0).toFixed(3)),
          crop: c.crop || null,
          rotate: c.rotate || 0,
          flipH: !!c.flipH,
          flipV: !!c.flipV,
          reverse: !!c.reverse,
          freeze: parseFloat((c.freeze || 0).toFixed(3)),
          detachAudio: !!c.detachAudio,
          // P4: chuyển cảnh + filter màu
          trans: c.trans ? { type: c.trans.type, dur: +(+(c.trans.dur) || 0.5).toFixed(3) } : null,
          fx: c.fx ? {
            brightness: +(c.fx.brightness || 0).toFixed(3),
            contrast: +((c.fx.contrast === undefined ? 1 : c.fx.contrast)).toFixed(3),
            saturation: +((c.fx.saturation === undefined ? 1 : c.fx.saturation)).toFixed(3),
            temperature: +(c.fx.temperature || 0).toFixed(3),
            blur: +(c.fx.blur || 0).toFixed(2),
            vignette: !!c.fx.vignette
          } : null
        })),
        // P3: chữ tự do + logo/overlay (at/dur theo timeline — backend tự map về file xuất)
        text_clips: (getTextTrack()?.clips || []).map(c => ({
          id: c.id,
          text: c.text || '',
          at: +(c.at || 0).toFixed(3),
          dur: +(c.dur || 3).toFixed(3),
          pos: { x: +(c.pos?.x ?? 0.5).toFixed(4), y: +(c.pos?.y ?? 0.8).toFixed(4) },
          style: {
            fontSize: c.style?.fontSize || 64,
            color: c.style?.color || '#FFFFFF',
            outlineColor: c.style?.outlineColor || '#000000',
            outlineWidth: c.style?.outlineWidth === undefined ? 3 : c.style.outlineWidth,
            bgOpacity: c.style?.bgOpacity || 0,
            bold: !!c.style?.bold
          },
          animIn: c.animIn || 'none',
          animOut: c.animOut || 'none'
        })),
        overlay_clips: (getOverlayTrack()?.clips || []).map(c => ({
          id: c.id,
          src: c.src || '',
          at: +(c.at || 0).toFixed(3),
          dur: +(c.dur || 3).toFixed(3),
          pos: { x: +(c.pos?.x ?? 0.5).toFixed(4), y: +(c.pos?.y ?? 0.85).toFixed(4) },
          scale: c.scale === undefined ? 1 : c.scale,
          opacity: c.opacity === undefined ? 1 : c.opacity
        })),
        // P5: nhạc/SFX trộn lên audio nền (volume/fade/ducking từng clip)
        audio_clips: (getMusicTrack()?.clips || []).map(c => ({
          id: c.id,
          src: c.src || '',
          at: +(c.at || 0).toFixed(3),
          dur: +(c.dur || 3).toFixed(3),
          volume: (typeof c.volume === 'number') ? +c.volume.toFixed(3) : 1,
          fadeIn: parseFloat((c.fadeIn || 0).toFixed(3)),
          fadeOut: parseFloat((c.fadeOut || 0).toFixed(3)),
          muted: !!c.muted,
          duck: !!c.duck
        })),
        // P6.2: preset khung xuất (9:16 / 1:1 / 16:9)
        export_frame: (() => {
          const v = (document.getElementById('exportFrameSel') || {}).value || '9:16';
          return v === '1:1' ? { w: 1080, h: 1080 } : (v === '16:9' ? { w: 1920, h: 1080 } : { w: 1080, h: 1920 });
        })(),
        target_version: currentVideoVersion || 'final'
      };
    }

    function handleApplyEditsSuccess(data) {
      showToast(`🎉 ${data.message || 'Đã ghép video thành công!'}`);
      // Reload player with newly concatenated video
      if (data.video_url && mainPlayer) {
        currentVideo.has_final = true;
        if (currentVideoData) {
          currentVideoData.has_final = true;
          currentVideoData.final_video_url = data.final_video_url;
          currentVideoData.video_url = data.video_url;
        }
        mainPlayer.src = data.video_url;
        mainPlayer.load();
        mainPlayer.play().catch(() => {});
      }
      if (data.subtitles && data.subtitles.length) {
        currentSubtitles = data.subtitles;
        if (currentVideoData) {
          currentVideoData.vietnamese_subtitles = data.subtitles;
        }
        renderSubtitlesTimeline();
        updateSubtitlesOverlay();
      }
      setTimeout(() => {
        initVideoClipsForCurrentVideo(true, { saveFresh: true }); // P6.1: ghi đè project.json bằng video thành phẩm mới
        renderMediaList();
      }, 800);
    }

    async function applyTimelineEditsSync(payload) {
      const res = await fetch('/api/workflow/timeline/apply-edits', {
        method: 'POST',
        headers: RENDER_TOKEN_HEADERS(),
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok && (data.status === 'ok' || data.success)) {
        handleApplyEditsSuccess(data);
      } else {
        showToast(`❌ Lỗi cắt ghép video: ${data.detail || 'Thất bại'}`);
      }
    }

    async function applyTimelineEditsToServer() {
      if (!currentVideo || !currentVideo.stem) {
        showToast('⚠️ Vui lòng chọn một video trước!');
        return;
      }
      if (!videoClipsState || !videoClipsState.length) {
        showToast('⚠️ Không có đoạn video nào để ghép!');
        return;
      }

      const btn = document.getElementById('btnApplyTimelineEdits');

      // P6.3: đang có job render → bấm lần nữa = HUỶ
      if (_activeRenderJobId) {
        try {
          await fetch('/api/workflow/timeline/render-cancel', {
            method: 'POST',
            headers: RENDER_TOKEN_HEADERS(),
            body: JSON.stringify({ job_id: _activeRenderJobId })
          });
          showToast('⏹️ Đã gửi yêu cầu huỷ render...');
        } catch (err) { /* bỏ qua lỗi mạng */ }
        return;
      }

      const payload = buildTimelineRenderPayload();
      if (btn) {
        btn.disabled = true;
        btn.innerText = '⏳ Đang gửi job...';
      }
      showToast(`🎬 Đang tạo job render nền cho ${videoClipsState.length} đoạn video...`);

      try {
        const res = await fetch('/api/workflow/timeline/apply-edits-async', {
          method: 'POST',
          headers: RENDER_TOKEN_HEADERS(),
          body: JSON.stringify(payload)
        });
        if (res.status === 404) {
          // backend cũ chưa có P6.3 → fallback render đồng bộ
          await applyTimelineEditsSync(payload);
          return;
        }
        const data = await res.json();
        if (!res.ok || !data.job_id) {
          showToast(`❌ ${data.detail || 'Không tạo được job render'}`);
          return;
        }
        _activeRenderJobId = data.job_id;
        if (btn) btn.innerText = '⏳ Trong hàng đợi GPU... (bấm để huỷ)';

        // Poll tiến độ tới khi xong / lỗi / huỷ
        while (_activeRenderJobId) {
          await new Promise(r => setTimeout(r, 800));
          const sres = await fetch(`/api/workflow/timeline/render-status?job_id=${encodeURIComponent(_activeRenderJobId)}`);
          if (!sres.ok) throw new Error('Mất kết nối tới job render');
          const st = await sres.json();
          if (btn) btn.innerText = `⏳ ${st.progress || 0}% — ${st.stage || 'Đang xử lý'} (bấm để huỷ)`;
          if (st.status === 'done') {
            _activeRenderJobId = null;
            handleApplyEditsSuccess(st.result || {});
            break;
          }
          if (st.status === 'error') {
            _activeRenderJobId = null;
            showToast(`❌ Lỗi render: ${st.stage || st.error || 'Thất bại'}`);
            break;
          }
          if (st.status === 'cancelled') {
            _activeRenderJobId = null;
            showToast('⏹️ Đã huỷ render');
            break;
          }
        }
      } catch (err) {
        console.error('Lỗi cắt ghép video:', err);
        showToast('❌ Lỗi kết nối khi gửi yêu cầu cắt ghép video.');
      } finally {
        _activeRenderJobId = null;
        if (btn) {
          btn.disabled = false;
          btn.innerText = '💾 ÁP DỤNG & GHÉP VIDEO';
        }
      }
    }

    // ===== P7: TÍCH HỢP CONCAT (tiến trình ngoài — AGPL) =====
    async function openInConcat() {
      if (!currentVideo || !currentVideo.stem) {
        showToast('⚠️ Vui lòng chọn một video trước!');
        return;
      }
      try {
        const res = await fetch('/api/workflow/concat/open', {
          method: 'POST',
          headers: RENDER_TOKEN_HEADERS(),
          body: JSON.stringify({ stem: currentVideo.stem })
        });
        const data = await res.json();
        if (res.ok) {
          showToast('🧩 Đã mở Concat với thư mục project của video này!');
        } else {
          showToast('❌ ' + (data.detail || 'Lỗi mở Concat'));
        }
      } catch (err) {
        showToast('❌ Lỗi kết nối khi mở Concat');
      }
    }

    async function importConcatFile(input) {
      const f = input.files && input.files[0];
      input.value = '';
      if (!f) return;
      if (!currentVideo || !currentVideo.stem) {
        showToast('⚠️ Chọn một video trước (để đặt tên import theo video đang làm)!');
        return;
      }
      const fd = new FormData();
      fd.append('stem', currentVideo.stem);
      fd.append('file', f, f.name);
      showToast('📥 Đang nhập file từ Concat vào thư viện...');
      try {
        const res = await fetch('/api/workflow/concat/import', {
          method: 'POST',
          headers: {
            'X-Local-Control-Token': (typeof CONTROL_PLANE_TOKEN !== 'undefined' ? CONTROL_PLANE_TOKEN : '__REPLACE_TOKEN__'),
            'X-Dashboard-Input': '1'
          },
          body: fd
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`📥 Đã nhập ${data.filename} (${data.size_mb}MB) vào thư viện!`);
          if (typeof renderMediaList === 'function') renderMediaList();
        } else {
          showToast('❌ ' + (data.detail || 'Lỗi nhập file'));
        }
      } catch (err) {
        showToast('❌ Lỗi kết nối khi nhập file');
      }
    }

    // ===== P6.2: PROXY 540p + PRELOAD CLIP KẾ + AUTO-CUT SILENCE =====
    let _proxyActiveStem = null;
    let _proxyRequestedStem = null;

    async function maybeSwitchToProxy() {
      try {
        if (!currentVideo || !currentVideo.stem) return;
        const ver = (currentVideoVersion === 'raw' || currentVideoVersion === 'input') ? 'raw' : 'final';
        const proxyTag = 'proxy_' + ver;
        if (_proxyActiveStem === (currentVideo.stem + '|' + ver) && (mainPlayer.src || '').indexOf(proxyTag) >= 0) return;
        const reqKey = currentVideo.stem + '|' + ver;
        _proxyRequestedStem = reqKey;
        const res = await fetch('/api/workflow/proxy/generate', {
          method: 'POST',
          headers: RENDER_TOKEN_HEADERS(),
          body: JSON.stringify({ stem: currentVideo.stem, version: ver })
        });
        const data = await res.json();
        if (!res.ok || !data.url) return;
        if (data.ready) {
          swapMainToProxy(data.url, reqKey);
          return;
        }
        // Poll chờ proxy (tối đa ~60s, 2s/lần) — không chặn thao tác
        for (let i = 0; i < 30; i++) {
          await new Promise(r => setTimeout(r, 2000));
          const curVer = (currentVideoVersion === 'raw' || currentVideoVersion === 'input') ? 'raw' : 'final';
          if (!currentVideo || (currentVideo.stem + '|' + curVer) !== _proxyRequestedStem) return;
          const sres = await fetch(`/api/workflow/proxy/status?stem=${encodeURIComponent(currentVideo.stem)}&version=${encodeURIComponent(ver)}`);
          const st = await sres.json();
          if (st.ready) { swapMainToProxy(st.url, reqKey); return; }
        }
      } catch (err) { /* preview vẫn chạy bản full nếu proxy lỗi */ }
    }

    function swapMainToProxy(url, reqKey) {
      if (!mainPlayer || !url) return;
      const wasPlaying = !mainPlayer.paused;
      const t = mainPlayer.currentTime || 0;
      mainPlayer.src = url;
      mainPlayer.load();
      mainPlayer.currentTime = t;
      if (wasPlaying) mainPlayer.play().catch(() => {});
      _proxyActiveStem = reqKey || (currentVideo ? currentVideo.stem : null);
      _proxyRequestedStem = _proxyActiveStem;
      showToast('🪶 Đang dùng proxy 540p cho preview mượt (xuất video vẫn dùng bản gốc full)');
    }

    async function autoCutTimelineSilences() {
      try {
        if (!currentVideo || !currentVideo.stem) {
          showToast('⚠️ Vui lòng chọn một video trước khi cắt khoảng lặng');
          return;
        }
        const ver = (currentVideoVersion === 'raw' || currentVideoVersion === 'input') ? 'raw' : 'final';
        showToast('🔍 Đang quét và phát hiện các khoảng lặng trong video...');
        const res = await fetch('/api/workflow/timeline/detect-silences', {
          method: 'POST',
          headers: RENDER_TOKEN_HEADERS(),
          body: JSON.stringify({ stem: currentVideo.stem, version: ver, min_silence_sec: 0.65, noise_db: -35.0 })
        });
        const data = await res.json();
        if (!res.ok) {
          showToast('❌ ' + (data.detail || 'Không thể phân tích khoảng lặng'));
          return;
        }
        if (!data.silence_count || !Array.isArray(data.active_clips) || data.active_clips.length === 0) {
          showToast('✅ Video có nhịp thoại liên tục, không có khoảng lặng dài (> 0.65s) cần cắt!');
          return;
        }
        if (typeof pushTimelineUndoState === 'function') {
          pushTimelineUndoState();
        }
        videoClipsState = data.active_clips;
        if (videoClipsState.length > 0) {
          setSingleSelection(videoClipsState[0].id);
        }
        markTimelineProjectDirty();
        renderVideoClipsTrack();
        updateTimelineToolbarStates();
        showToast(`✂️ Đã tự động cắt ${data.silence_count} khoảng lặng (tiết kiệm ${data.removed_seconds}s → còn ${data.active_clips.length} đoạn)!`);
      } catch (err) {
        console.error('Lỗi cắt khoảng lặng:', err);
        showToast('❌ Lỗi kết nối khi phân tích khoảng lặng');
      }
    }

    function warmPreloadForNextClip(nextIdx) {
      try {
        const pv = document.getElementById('preloadPlayer');
        if (!pv || !mainPlayer || !mainPlayer.src || !videoClipsState.length) return;
        if (pv.dataset.srcUrl !== mainPlayer.src) {
          pv.src = mainPlayer.src;
          pv.dataset.srcUrl = mainPlayer.src;
        }
        const nn = (nextIdx + 1 < videoClipsState.length) ? nextIdx + 1 : 0;
        const target = videoClipsState[nn].start || 0;
        if (Math.abs(pv.currentTime - target) > 0.1) {
          pv.currentTime = target; // kéo range request làm ấm cache HTTP vùng sắp phát
        }
      } catch (err) { /* bỏ qua */ }
    }
