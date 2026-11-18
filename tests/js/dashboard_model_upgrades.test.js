const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../../backend/templates/dashboard.html'), 'utf8');

const stepModelsSource = html.slice(
  html.indexOf('function formatVoiceTagLabel('),
  html.indexOf('function updateRamUI(')
);
const asrControlsSource = html.slice(
  html.indexOf("let currentAsrModel = 'whisper_sensevoice';"),
  html.indexOf('async function loadVisualCoverSettings()')
);

function createHarness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        textContent: '',
        title: '',
        value: '',
        checked: false,
        disabled: false,
        style: {},
        selectedOptions: [],
        options: [],
        querySelector: () => null,
        appendChild() {},
      });
    }
    return elements.get(id);
  }
  const context = {
    document: { getElementById: element, createElement: () => ({ style: {} }) },
    lastStatusData: null,
    fetch: async () => ({ ok: true, json: async () => ({ status: 'ok' }) }),
    showToast: () => {},
    selectV1Mode: async () => {},
    loadVisualCoverSettings: async () => {},
    console,
  };
  vm.runInNewContext(asrControlsSource + '\n' + stepModelsSource, context);
  return { element, context };
}

test('dashboard static HTML includes upgraded stage tags, Silero VAD toggle, Gemini selector, and Paraformer card', () => {
  assert.ok(html.includes('id="flagVadSmartBypass"'));
  assert.ok(html.includes('id="selectGeminiModel"'));
  assert.ok(html.includes('id="cardAsrParaformer"'));
  assert.ok(html.includes('id="badgeModelOcrHybrid"'));
  assert.ok(html.includes('id="badgeModelCamplus"'));
  assert.ok(html.includes('PP-OCRv6 + v5 Server'));
  assert.ok(html.includes('Pixel QC · PP-OCRv5'));
  assert.equal(html.includes('gemini-3.7-flash'), false);
});

test('step model tags reflect Silero VAD, Hybrid Whisper+SenseVoice+PF, PP-OCRv6+v5 Server, Gemini short label, CAM++, and Pixel QC', () => {
  const { element, context } = createHarness();
  context.updateStepModelsUI({
    active: false,
    video_status: 'completed',
    step: 9,
    vad_smart_bypass: true,
    configured_asr_model: 'whisper_sensevoice',
    paraformer_co_rec: true,
    ocr_model: 'PP-OCRv6 + v5 Server',
    ocr_model_detail: 'PP-OCRv6_tiny_det + PP-OCRv5_server_rec (PaddleOCR ONNX CUDA)',
    configured_gemini_model: 'gemini-3-flash-preview',
    translation_models: ['gemini-3.1-flash-lite-preview'],
    voice_name: 'Á Mai · CapCut',
    speaker_lock: 'CAM++ 512-D',
    qc_pixel_check: true,
  });

  assert.equal(element('step2ModelTag').textContent, 'BS-RoFormer + Silero VAD');
  assert.equal(element('step3ModelTag').textContent, 'Whisper + SenseVoice + PF');
  assert.equal(element('step4ModelTag').textContent, 'PP-OCRv6 + v5 Server');
  assert.equal(element('step5ModelTag').textContent, 'Đã dịch: gemini-3.1-flash-lite');
  assert.equal(element('step5ModelTag').title, 'Mô hình dịch AI thực tế đã hoàn thành: gemini-3.1-flash-lite-preview');
  assert.equal(element('step6ModelTag').textContent, 'Á Mai · CapCut · CAM++');
  assert.equal(element('step9ModelTag').textContent, 'Pixel QC · PP-OCRv5');

  context._updateSingleStepModelsUI({
    active: false,
    details: { voice: 'CapCut · Mai (Nữ Chuẩn - Mặc định) (Khóa theo giọng nữ đầu video)' },
    speaker_lock: 'CAM++ 512-D',
  }, '');
  assert.equal(element('step6ModelTag').textContent, 'CapCut Mai · CAM++');
});

test('step model tags reflect Silero VAD BGM bypass, Paraformer-large-zh standalone, and OCR-to-TTS visual caption mode', () => {
  const { element, context } = createHarness();

  context._updateSingleStepModelsUI({
    active: true,
    step: 3,
    details: { model: 'silero_vad_bypass' },
    asr_model: 'paraformer_zh',
    qc_pixel_check: false,
  }, '');
  assert.equal(element('step2ModelTag').textContent, 'Silero VAD (Giữ BGM)');
  assert.equal(element('step3ModelTag').textContent, 'Paraformer-large-zh + CTC');
  assert.equal(element('step9ModelTag').textContent, 'Quality Gate QC');

  context._updateSingleStepModelsUI({
    active: true,
    step: 3,
    asr_source: 'visual_caption',
  }, 'w1');
  assert.equal(element('w1Step3ModelTag').textContent, 'OCR-to-TTS (Sub hình)');
});

test('selecting Paraformer-large-zh updates radio cards, active badge, and idle step3 tags across Single/W1/W2', async () => {
  const { element, context } = createHarness();
  await context.selectAsrModel('paraformer_zh', true);

  assert.equal(element('radioAsrParaformer').checked, true);
  assert.equal(element('badgeParaformerActive').textContent, '[ĐANG DÙNG]');
  assert.equal(element('badgeHybridActive').textContent, '[BẤM ĐỂ CHỌN]');
  assert.ok(element('activeAsrBadge').textContent.includes('Paraformer-large-zh'));
  assert.equal(element('step3ModelTag').textContent, 'Paraformer-large-zh + CTC');
  assert.equal(element('w1Step3ModelTag').textContent, 'Paraformer-large-zh + CTC');
  assert.equal(element('w2Step3ModelTag').textContent, 'Paraformer-large-zh + CTC');
});

test('conditional QC pixel card and blur params row auto-hide when unused', () => {
  const coverSource = html.slice(
    html.indexOf('function syncBlurParamsVisibility()'),
    html.indexOf('async function saveVisualCoverSettings()')
  );
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, { id, value: '', style: {} });
    return elements.get(id);
  };
  const ctx = { document: { getElementById: getEl }, fetch: async () => ({ ok: false }), console };
  vm.runInNewContext(coverSource, ctx);

  getEl('selectSubCoverStyle').value = 'solid_box';
  ctx.syncBlurParamsVisibility();
  assert.equal(getEl('subBlurParamsRow').style.display, 'none');

  getEl('selectSubCoverStyle').value = 'localized_blur';
  ctx.syncBlurParamsVisibility();
  assert.equal(getEl('subBlurParamsRow').style.display, 'flex');

  getEl('selectQcPolicy').value = 'OFF';
  ctx.syncQcPixelCardVisibility();
  assert.equal(getEl('qcPixelCheckCard').style.display, 'none');

  getEl('selectQcPolicy').value = 'WARN';
  ctx.syncQcPixelCardVisibility();
  assert.equal(getEl('qcPixelCheckCard').style.display, 'flex');
});

test('1-click runSinglePhoiVideo dispatches /api/jobs/batch without confirmation dialog', async () => {
  const bulkSource = html.slice(
    html.indexOf('async function dispatchPhoiItems('),
    html.indexOf('async function openPreRunSummaryModal()')
  );
  const calls = [];
  const ctx = {
    isSubmittingBatch: false,
    videoDraftsCache: {},
    currentActivePresetId: 'preset-fast',
    currentV1Mode: 'AUTO',
    selectedPhoiFiles: new Set(),
    document: { getElementById: () => ({ disabled: false }) },
    fetch: async (url, cfg) => {
      calls.push({ url, cfg });
      return { ok: true, json: async () => ({ total_items: 1 }) };
    },
    showToast: () => {},
    fetchPhoi: () => {},
    fetchQueue: () => {},
    fetchStatus: () => {},
    clearPhoiSelection: () => {},
  };
  vm.runInNewContext(bulkSource, ctx);
  await ctx.runSinglePhoiVideo(encodeURIComponent('clip_demo.mp4'));

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/jobs/batch');
  const payload = JSON.parse(calls[0].cfg.body);
  assert.equal(payload.trigger, true);
  assert.equal(payload.items.length, 1);
  assert.equal(payload.items[0].filename, 'clip_demo.mp4');
  assert.equal(payload.items[0].preset_id, 'preset-fast');
});

test('btnRefreshBanve force-refreshes Video thành phẩm list, aborts hung poll, clears stale timeout notice, and reports new videos', async () => {
  assert.ok(html.includes('id="btnRefreshBanve"'), 'Header Video thành phẩm phải có nút Làm mới (#btnRefreshBanve)');
  assert.ok(html.includes('onclick="refreshBanveManual()"'), 'Nút Làm mới phải gọi refreshBanveManual()');

  const banveSource = html.slice(
    html.indexOf('let lastBanveFingerprint ='),
    html.indexOf('let logText =')
  );
  const els = {};
  const getEl = (id) => {
    if (!els[id]) {
      els[id] = {
        id,
        disabled: false,
        textContent: id === 'banveListNotice' ? 'Không cập nhật được danh sách. signal timed out' : '',
        innerHTML: '',
        classList: {
          _s: new Set(),
          add(c) { this._s.add(c); },
          remove(c) { this._s.delete(c); },
        },
      };
    }
    return els[id];
  };

  let abortedOld = false;
  const fetchUrls = [];
  const toasts = [];
  const dashboardLists = {
    banve: {
      offset: 0,
      limit: 100,
      pending: true,
      dirty: false,
      timer: null,
      data: { total_count: 13 },
      controller: { abort() { abortedOld = true; } },
    },
  };

  const ctx = {
    DASHBOARD_TOOL: 'V1',
    dashboardLists,
    AbortController,
    DOMException,
    Date,
    setTimeout,
    clearTimeout,
    console,
    document: { getElementById: getEl },
    beginDashboardList(scope) {
      const l = dashboardLists[scope];
      if (l.pending) return null;
      l.pending = true;
      return '/api/banve?limit=100&offset=0&search=&status=all';
    },
    acceptDashboardList(scope, data) {
      dashboardLists[scope].data = data;
      return true;
    },
    endDashboardList(scope) {
      dashboardLists[scope].pending = false;
    },
    setDashboardOutputPath() {},
    escapeHtml: (s) => String(s),
    decorateBanveQcBadges() {},
    showDashboardListError() {},
    showToast(msg, level) { toasts.push({ msg, level }); },
    fetch: async (url, cfg) => {
      fetchUrls.push({ url, cache: cfg.cache });
      return {
        ok: true,
        json: async () => ({
          path: 'D:\\banve',
          total_count: 15,
          filtered_count: 15,
          total_size_mb: 560.2,
          offset: 0,
          files: [{ name: 'Dubbed_new.mp4', size_mb: 28.1, duration_formatted: '4p 10s', created: '2026-11-18 10:32:00', status: 'completed', status_label: 'Media hợp lệ' }],
        }),
      };
    },
  };

  vm.runInNewContext(banveSource, ctx);
  await ctx.refreshBanveManual();

  assert.equal(abortedOld, true, 'Phải huỷ request treo cũ để làm mới ngay lập tức');
  assert.equal(getEl('banveListNotice').textContent, '', 'Phải xoá cảnh báo signal timed out cũ');
  assert.equal(fetchUrls.length, 1);
  assert.ok(fetchUrls[0].url.includes('/api/banve?') && fetchUrls[0].url.includes('&_t='));
  assert.equal(fetchUrls[0].cache, 'no-store');
  assert.equal(getEl('banveBadgeCount').textContent, '15 video V1 (560.2 MB)');
  assert.equal(getEl('btnRefreshBanve').disabled, false);
  assert.ok(toasts.some((t) => t.msg.includes('+2') || t.msg.includes('2 video thành phẩm mới')));
});

test('Dashboard <-> Studio Quy Trình: nút mở thẳng ?stem=, duyệt liên hoàn ↑/↓ trong Xem thử, và lưu nhanh Glossary', async () => {
  assert.ok(html.includes('openInStudioWorkflow('), 'Bảng thành phẩm & phôi phải có nút mở thẳng sang Studio Quy Trình');
  assert.ok(html.includes('stepPreviewModalVideo('), 'Modal Xem thử phải hỗ trợ duyệt liên hoàn ↑/↓');
  assert.ok(html.includes('saveQuickGlossaryTerm()'), 'Modal Sửa câu phải có thanh Nhớ từ nhanh vào Glossary');

  const previewSource = html.slice(
    html.indexOf('let _previewModalState ='),
    html.indexOf('function closeModal(')
  );
  const els = {};
  const getEl = (id) => {
    if (!els[id]) {
      els[id] = {
        id,
        disabled: false,
        textContent: '',
        innerHTML: '',
        src: '',
        style: { display: 'none' },
        pause() {},
        removeAttribute() {},
        load() {},
        play() { return Promise.resolve(); },
      };
    }
    return els[id];
  };
  let navHref = '';
  const ctx = {
    window: { location: { get href() { return navHref; }, set href(v) { navHref = v; } } },
    document: { getElementById: getEl, addEventListener() {} },
    dashboardLists: {
      banve: {
        data: {
          files: [
            { name: 'Dubbed_telegram_aaa_111.mp4' },
            { name: 'Dubbed_telegram_bbb_222_rev2.mp4' },
            { name: 'Dubbed_telegram_ccc_333.mp4' },
          ],
        },
      },
    },
    escapeHtml: (s) => String(s),
    closeModal() { getEl('videoModal').style.display = 'none'; },
    openSegmentEditorModal() {},
    openCompareModal() {},
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    showToast() {},
    console,
  };
  vm.runInNewContext(previewSource, ctx);

  // 1. Trích stem chuẩn (bỏ Dubbed_, bỏ _rev2, bỏ .mp4) và điều hướng sang /quy-trinh?stem=...
  assert.equal(ctx.extractVideoStemForStudio('Dubbed_telegram_bbb_222_rev2.mp4'), 'telegram_bbb_222');
  ctx.openInStudioWorkflow(encodeURIComponent('Dubbed_telegram_bbb_222_rev2.mp4'));
  assert.equal(navHref, '/quy-trinh?stem=telegram_bbb_222');

  // 2. Duyệt liên hoàn ↑/↓ kiểu TikTok trong cửa sổ Xem thử
  ctx.previewVideo('banve', encodeURIComponent('Dubbed_telegram_aaa_111.mp4'));
  assert.equal(getEl('modalVideoIndexBadge').textContent, '1 / 3');
  ctx.stepPreviewModalVideo(1);
  assert.equal(getEl('modalVideoIndexBadge').textContent, '2 / 3');
  assert.ok(getEl('previewVideoPlayer').src.includes('Dubbed_telegram_bbb_222_rev2.mp4'));
  ctx.stepPreviewModalVideo(-1);
  assert.equal(getEl('modalVideoIndexBadge').textContent, '1 / 3');

  // 3. Lưu nhanh cặp từ vào /api/v1/glossary từ Sửa câu
  const glossarySource = html.slice(
    html.indexOf('function captureSelectedSourceForGlossary()'),
    html.indexOf('function onSegmentTextChange(')
  );
  const posted = [];
  const glEls = {
    quickGlossarySrcInput: { value: '飞机盒' },
    quickGlossaryDstInput: { value: 'hộp carton nắp gài' },
    btnQuickSaveGlossary: { disabled: false },
  };
  const glCtx = {
    window: { getSelection: () => ({ toString: () => '飞机盒' }) },
    document: { getElementById: (id) => glEls[id] || null },
    fetch: async (url, cfg) => {
      posted.push({ url, body: JSON.parse(cfg.body) });
      return { ok: true, json: async () => ({ count: 12 }) };
    },
    showToast() {},
  };
  vm.runInNewContext(glossarySource, glCtx);
  await glCtx.saveQuickGlossaryTerm();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].url, '/api/v1/glossary');
  assert.deepEqual(posted[0].body, { entries: { '飞机盒': 'hộp carton nắp gài' }, merge: true });
  assert.equal(glEls.quickGlossarySrcInput.value, '');
});
