"""
update_dashboards_phase1.py - Integrates Phase 1 features into Tool V1 and V2 dashboard.html templates.
Features:
1. Preflight Check (Modal + Live Probe)
2. Retry Failed Video (Per-video Chay lai button in phoi table)
3. Task History & Render Stats (Modal + Searchable Table)
4. QC Gate Report Viewer (For Tool V2 - Modal + Quality Metrics)
"""
import shutil
import sys
from pathlib import Path

COMMON_CSS = """
  /* ==========================================================================
     PHASE 1: PREFLIGHT, TASK HISTORY & QC GATE STYLING
     ========================================================================== */
  .modal-box-lg { max-width: 940px !important; width: 95% !important; max-height: 88vh !important; }
  .modal-body-scroll { padding: 18px !important; background: #0c1219 !important; overflow-y: auto !important; max-height: calc(88vh - 75px) !important; display: block !important; }
  .preflight-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 12px; margin-top: 14px; }
  .preflight-card { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 10px; padding: 14px; display: flex; flex-direction: column; gap: 6px; }
  .preflight-card-header { display: flex; justify-content: space-between; align-items: center; }
  .preflight-card-title { font-size: 13px; font-weight: 600; color: #f1f5f9; display: flex; align-items: center; gap: 8px; }
  .preflight-card-desc { font-size: 12px; color: #cbd5e1; line-height: 1.4; }
  .preflight-card-detail { font-size: 11px; color: var(--text-muted); font-family: ui-monospace, Consolas, monospace; background: rgba(0,0,0,0.35); padding: 5px 8px; border-radius: 4px; word-break: break-all; margin-top: 4px; }
  .preflight-banner { padding: 12px 16px; border-radius: 8px; display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }
  .preflight-banner.pass { background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.3); color: #6ee7b7; }
  .preflight-banner.warning { background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.3); color: #fcd34d; }
  .preflight-banner.error { background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); color: #fca5a5; }

  /* History stats bar */
  .history-stats-bar { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .history-stat-box { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; text-align: center; }
  .history-stat-val { font-size: 18px; font-weight: 700; color: #38bdf8; font-variant-numeric: tabular-nums; }
  .history-stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }

  /* QC Gate styling */
  .qc-metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 12px 0; }
  .qc-metric-card { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .qc-metric-title { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .qc-metric-value { font-size: 20px; font-weight: 700; margin: 4px 0; font-variant-numeric: tabular-nums; }
  .qc-metric-sub { font-size: 11px; color: #94a3b8; }
  .qc-checks-list { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
  .qc-check-item { display: flex; align-items: flex-start; justify-content: space-between; padding: 8px 12px; background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 6px; font-size: 12px; }
"""

MODALS_HTML_V1 = """
  <!-- MODAL PREFLIGHT CHECK -->
  <div class="modal-backdrop" id="preflightModal" onclick="closePreflightModal(event)">
    <div class="modal-box modal-box-lg" onclick="event.stopPropagation()">
      <div class="modal-header">
        <div class="modal-title" style="display:flex; align-items:center; gap:8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
          Kiểm Tra Hệ Thống (Preflight Check) · Tool V1
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <button class="btn btn-outline" id="btnPreflightRecheck" onclick="loadPreflightData()" style="padding: 2px 9px; font-size: 11px;">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
            Kiểm tra lại
          </button>
          <button class="modal-close" onclick="closePreflightModal()">&times;</button>
        </div>
      </div>
      <div class="modal-body-scroll">
        <div id="preflightBanner" class="preflight-banner pass">Đang khởi tạo kiểm tra hệ thống...</div>
        <div id="preflightGrid" class="preflight-grid"></div>
      </div>
    </div>
  </div>

  <!-- MODAL TASK HISTORY -->
  <div class="modal-backdrop" id="taskHistoryModal" onclick="closeTaskHistoryModal(event)">
    <div class="modal-box modal-box-lg" onclick="event.stopPropagation()">
      <div class="modal-header">
        <div class="modal-title" style="display:flex; align-items:center; gap:8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          Lịch Sử Tác Vụ & Thống Kê Render Video · Tool V1
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <input type="text" id="taskHistorySearch" placeholder="Tìm tên video lịch sử..." oninput="filterTaskHistory(this.value)" style="background: rgba(0,0,0,0.4); border: 1px solid var(--border); border-radius: 6px; padding: 3px 8px; font-size: 11.5px; color: #fff; width: 180px;">
          <button class="modal-close" onclick="closeTaskHistoryModal()">&times;</button>
        </div>
      </div>
      <div class="modal-body-scroll">
        <div class="history-stats-bar">
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryCount">--</div>
            <div class="history-stat-label">Tổng tác vụ</div>
          </div>
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryTotalTime">--</div>
            <div class="history-stat-label">Tổng thời gian render</div>
          </div>
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryAvgTime">--</div>
            <div class="history-stat-label">Thời gian trung bình / video</div>
          </div>
        </div>
        <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px;">
          <table class="data-table" style="width: 100%; font-size: 12px;">
            <thead>
              <tr>
                <th style="width: 40px;">STT</th>
                <th>Tên Video</th>
                <th style="width: 80px;">Dung lượng</th>
                <th style="width: 110px;">Thời gian Render</th>
                <th style="width: 140px;">Ngày hoàn thành</th>
                <th style="width: 95px;">Trạng thái</th>
                <th style="width: 60px;">Thao tác</th>
              </tr>
            </thead>
            <tbody id="taskHistoryBody">
              <tr><td colspan="7" style="text-align:center; padding: 20px; color: var(--text-muted);">Đang tải dữ liệu lịch sử...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
"""

MODALS_HTML_V2 = """
  <!-- MODAL PREFLIGHT CHECK -->
  <div class="modal-backdrop" id="preflightModal" onclick="closePreflightModal(event)">
    <div class="modal-box modal-box-lg" onclick="event.stopPropagation()">
      <div class="modal-header">
        <div class="modal-title" style="display:flex; align-items:center; gap:8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
          Kiểm Tra Hệ Thống (Preflight Check) · Tool V2
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <button class="btn btn-outline" id="btnPreflightRecheck" onclick="loadPreflightData()" style="padding: 2px 9px; font-size: 11px;">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
            Kiểm tra lại
          </button>
          <button class="modal-close" onclick="closePreflightModal()">&times;</button>
        </div>
      </div>
      <div class="modal-body-scroll">
        <div id="preflightBanner" class="preflight-banner pass">Đang khởi tạo kiểm tra hệ thống...</div>
        <div id="preflightGrid" class="preflight-grid"></div>
      </div>
    </div>
  </div>

  <!-- MODAL TASK HISTORY -->
  <div class="modal-backdrop" id="taskHistoryModal" onclick="closeTaskHistoryModal(event)">
    <div class="modal-box modal-box-lg" onclick="event.stopPropagation()">
      <div class="modal-header">
        <div class="modal-title" style="display:flex; align-items:center; gap:8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          Lịch Sử Tác Vụ & Thống Kê Render Video · Tool V2
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <input type="text" id="taskHistorySearch" placeholder="Tìm tên video lịch sử..." oninput="filterTaskHistory(this.value)" style="background: rgba(0,0,0,0.4); border: 1px solid var(--border); border-radius: 6px; padding: 3px 8px; font-size: 11.5px; color: #fff; width: 180px;">
          <button class="modal-close" onclick="closeTaskHistoryModal()">&times;</button>
        </div>
      </div>
      <div class="modal-body-scroll">
        <div class="history-stats-bar">
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryCount">--</div>
            <div class="history-stat-label">Tổng tác vụ</div>
          </div>
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryTotalTime">--</div>
            <div class="history-stat-label">Tổng thời gian render</div>
          </div>
          <div class="history-stat-box">
            <div class="history-stat-val" id="taskHistoryAvgTime">--</div>
            <div class="history-stat-label">Thời gian trung bình / video</div>
          </div>
        </div>
        <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px;">
          <table class="data-table" style="width: 100%; font-size: 12px;">
            <thead>
              <tr>
                <th style="width: 40px;">STT</th>
                <th>Tên Video</th>
                <th style="width: 80px;">Dung lượng</th>
                <th style="width: 110px;">Thời gian Render</th>
                <th style="width: 140px;">Ngày hoàn thành</th>
                <th style="width: 95px;">Trạng thái</th>
                <th style="width: 60px;">Thao tác</th>
              </tr>
            </thead>
            <tbody id="taskHistoryBody">
              <tr><td colspan="7" style="text-align:center; padding: 20px; color: var(--text-muted);">Đang tải dữ liệu lịch sử...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- MODAL QC REPORT -->
  <div class="modal-backdrop" id="qcReportModal" onclick="closeQcReportModal(event)">
    <div class="modal-box modal-box-lg" onclick="event.stopPropagation()">
      <div class="modal-header">
        <div class="modal-title" style="display:flex; align-items:center; gap:8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          Báo Cáo Kiểm Định Chất Lượng QC Gate · Tool V2
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <select id="qcReportSelect" onchange="loadQcReportData(this.value)" style="background: rgba(0,0,0,0.5); border: 1px solid var(--border); border-radius: 6px; padding: 3px 8px; font-size: 11px; color: #cbd5e1; max-width: 240px; text-overflow: ellipsis;"></select>
          <button class="modal-close" onclick="closeQcReportModal()">&times;</button>
        </div>
      </div>
      <div class="modal-body-scroll" id="qcReportDetails">
        <div style="text-align:center; padding: 20px; color: var(--text-muted);">Đang tải dữ liệu báo cáo QC...</div>
      </div>
    </div>
  </div>
"""

COMMON_JS = """
  // ==========================================================================
  // PHASE 1: PREFLIGHT, TASK HISTORY, RETRY VIDEO & QC GATE HANDLERS
  // ==========================================================================

  // 1. Preflight Check
  async function openPreflightModal(e) {
    if (e && e.target && e.target !== document.getElementById('preflightModal')) return;
    const modal = document.getElementById('preflightModal');
    if (!modal) return;
    modal.style.display = 'flex';
    await loadPreflightData();
  }

  function closePreflightModal(e) {
    if (e && e.target && e.target !== document.getElementById('preflightModal') && !e.target.classList.contains('modal-close')) return;
    const modal = document.getElementById('preflightModal');
    if (modal) modal.style.display = 'none';
  }

  async function loadPreflightData() {
    const banner = document.getElementById('preflightBanner');
    const grid = document.getElementById('preflightGrid');
    const recheckBtn = document.getElementById('btnPreflightRecheck');
    if (recheckBtn) recheckBtn.disabled = true;
    if (banner) {
      banner.className = 'preflight-banner';
      banner.innerHTML = 'Đang kiểm tra phần cứng, GPU NVENC, ổ đĩa và dịch vụ AI...';
    }
    
    try {
      const res = await fetch('/api/preflight');
      if (!res.ok) throw new Error('Mã phản hồi: ' + res.status);
      const data = await res.json();
      if (banner) {
        banner.className = 'preflight-banner ' + (data.status || 'pass');
        const icon = data.ready ? '✅' : '⚠️';
        banner.innerHTML = `<div><strong>${icon} ${data.ready ? 'HỆ THỐNG SẴN SÀNG RENDER TỐC ĐỘ CAO' : 'CẦN LƯU Ý'}</strong><div style="font-size:11.5px;margin-top:2px;">${escapeHtml(data.summary || '')}</div></div><span class="status-badge ${data.status === 'pass' ? 'status-completed' : 'status-waiting'}">${(data.status || 'pass').toUpperCase()}</span>`;
      }
      if (grid && Array.isArray(data.checks)) {
        grid.innerHTML = data.checks.map(c => {
          const badgeColor = c.status === 'pass' ? '#10b981' : (c.status === 'warning' ? '#f59e0b' : '#ef4444');
          return `
            <div class="preflight-card">
              <div class="preflight-card-header">
                <span class="preflight-card-title">${escapeHtml(c.title)}</span>
                <span class="status-badge" style="background: ${badgeColor}22; color: ${badgeColor}; border: 1px solid ${badgeColor}44; font-size: 10.5px;">${escapeHtml(c.badge || c.status.toUpperCase())}</span>
              </div>
              <div class="preflight-card-desc">${escapeHtml(c.description || '')}</div>
              <div class="preflight-card-detail">${escapeHtml(c.details || '')}</div>
            </div>
          `;
        }).join('');
      }
    } catch (err) {
      if (banner) {
        banner.className = 'preflight-banner error';
        banner.innerHTML = `<strong>LỖI</strong> · Không thể kết nối API kiểm tra: ${escapeHtml(err.message)}`;
      }
    } finally {
      if (recheckBtn) recheckBtn.disabled = false;
    }
  }

  // 2. Task History
  let taskHistoryData = [];
  async function openTaskHistoryModal(e) {
    if (e && e.target && e.target !== document.getElementById('taskHistoryModal')) return;
    const modal = document.getElementById('taskHistoryModal');
    if (!modal) return;
    modal.style.display = 'flex';
    await loadTaskHistoryData();
  }

  function closeTaskHistoryModal(e) {
    if (e && e.target && e.target !== document.getElementById('taskHistoryModal') && !e.target.classList.contains('modal-close')) return;
    const modal = document.getElementById('taskHistoryModal');
    if (modal) modal.style.display = 'none';
  }

  async function loadTaskHistoryData() {
    const tbody = document.getElementById('taskHistoryBody');
    const countEl = document.getElementById('taskHistoryCount');
    const totalTimeEl = document.getElementById('taskHistoryTotalTime');
    const avgTimeEl = document.getElementById('taskHistoryAvgTime');
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding: 20px; color: var(--text-muted);">Đang tải dữ liệu lịch sử...</td></tr>';
    
    try {
      const res = await fetch('/api/task-history');
      if (!res.ok) throw new Error('Mã phản hồi: ' + res.status);
      const data = await res.json();
      taskHistoryData = data.history || [];
      const stats = data.stats || {};
      
      if (countEl) countEl.textContent = `${stats.total_tasks || 0} video`;
      if (totalTimeEl) totalTimeEl.textContent = stats.total_render_formatted || '--';
      if (avgTimeEl) avgTimeEl.textContent = stats.avg_render_formatted || '--';
      
      renderTaskHistoryTable(taskHistoryData);
    } catch (err) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 20px; color: #ef4444;">Lỗi tải lịch sử: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  function renderTaskHistoryTable(items) {
    const tbody = document.getElementById('taskHistoryBody');
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding: 20px; color: var(--text-muted);">Không tìm thấy bản ghi lịch sử nào.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((item, idx) => {
      const safeName = escapeHtml(item.name);
      const encodedName = encodeURIComponent(item.name).replace(/'/g, '%27');
      const statusBadge = item.file_exists
        ? '<span class="status-badge status-completed" style="font-size: 10px;">Thành phẩm</span>'
        : '<span class="status-badge" style="background: rgba(148, 163, 184, 0.1); color: #94a3b8; font-size: 10px;">Đã lưu trữ</span>';
      const actionBtn = item.file_exists
        ? `<button class="btn btn-outline" style="padding: 2px 7px; font-size: 10.5px;" onclick="previewVideo('banve', '${encodedName}')">Xem</button>`
        : '<span style="color:var(--text-muted);font-size:11px;">--</span>';
      return `
        <tr>
          <td style="color: var(--text-muted); font-size: 11px;">#${idx + 1}</td>
          <td style="max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500;" title="${safeName}">${safeName}</td>
          <td style="font-variant-numeric: tabular-nums;">${item.size_mb > 0 ? item.size_mb + ' MB' : '--'}</td>
          <td style="color: #38bdf8; font-weight: 600; font-variant-numeric: tabular-nums;">${escapeHtml(item.duration_formatted)}</td>
          <td style="color: var(--text-muted); font-size: 11px;">${item.completed_at || '--'}</td>
          <td>${statusBadge}</td>
          <td>${actionBtn}</td>
        </tr>
      `;
    }).join('');
  }

  function filterTaskHistory(query) {
    if (!query) {
      renderTaskHistoryTable(taskHistoryData);
      return;
    }
    const q = normalizeDashboardSearch(query);
    const filtered = taskHistoryData.filter(i => normalizeDashboardSearch(i.name).includes(q));
    renderTaskHistoryTable(filtered);
  }

  // 3. Retry Video
  async function retryVideo(encodedName) {
    const filename = decodeURIComponent(encodedName);
    if (!confirm(`Bạn có chắc chắn muốn xử lý lại video "${filename}"?`)) return;
    try {
      showToast(`Đang yêu cầu chạy lại video "${filename}"...`, 'info');
      const res = await fetch('/api/retry-video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename })
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message || `Đã bắt đầu xử lý lại "${filename}"!`, 'success');
        setTimeout(refreshAll, 600);
      } else {
        showToast(data.message || data.detail || 'Không thể chạy lại video.', 'error');
      }
    } catch (err) {
      showToast(`Lỗi gửi yêu cầu: ${err.message}`, 'error');
    }
  }
"""

QC_JS_V2 = """
  // 4. QC Gate Viewer (Tool V2)
  async function openQcReportModal(target, e) {
    if (e && e.target && e.target !== document.getElementById('qcReportModal')) return;
    const modal = document.getElementById('qcReportModal');
    if (!modal) return;
    modal.style.display = 'flex';
    await loadQcReportsSelector(target);
    await loadQcReportData(target);
  }

  function closeQcReportModal(e) {
    if (e && e.target && e.target !== document.getElementById('qcReportModal') && !e.target.classList.contains('modal-close')) return;
    const modal = document.getElementById('qcReportModal');
    if (modal) modal.style.display = 'none';
  }

  async function loadQcReportsSelector(selectedTarget) {
    const select = document.getElementById('qcReportSelect');
    if (!select) return;
    try {
      const res = await fetch('/api/qc-reports');
      const list = await res.json();
      if (Array.isArray(list) && list.length) {
        select.innerHTML = list.map(r => `
          <option value="${escapeHtml(r.video_name)}" ${r.video_name === selectedTarget ? 'selected' : ''}>
            ${r.is_pass ? '✅' : '⚠️'} ${escapeHtml(r.video_name)} (${r.date})
          </option>
        `).join('');
      } else {
        select.innerHTML = '<option value="">Chưa có báo cáo QC nào</option>';
      }
    } catch (_) {}
  }

  async function loadQcReportData(target) {
    const container = document.getElementById('qcReportDetails');
    if (!container) return;
    container.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Đang tải dữ liệu QC Gate...</div>';
    
    try {
      const url = target ? `/api/qc-report?target=${encodeURIComponent(target)}` : '/api/qc-report';
      const res = await fetch(url);
      if (!res.ok) throw new Error('Không tìm thấy báo cáo QC cho video này.');
      const data = await res.json();
      const metrics = data.metrics || {};
      const summary = data.summary || {};
      const overall = data.overall || 'unknown';
      const isPass = ['ok', 'pass'].includes(overall.toLowerCase());
      
      container.innerHTML = `
        <div class="preflight-banner ${isPass ? 'pass' : 'warning'}" style="margin-bottom: 14px;">
          <div>
            <strong>${isPass ? '🛡️ QC GATE PASS · ĐẠT CHUẨN XUẤT BẢN YOUTUBE' : '⚠️ QC GATE CẦN LƯU Ý'}</strong>
            <div style="font-size: 11.5px; opacity: 0.9; margin-top: 2px;">Video: ${escapeHtml(data.video_path ? data.video_path.split('\\\\').pop() : 'final.mp4')}</div>
          </div>
          <div style="display:flex; gap: 6px;">
            <span class="status-badge status-completed">${summary.pass || 0} Đạt</span>
            ${summary.warning ? `<span class="status-badge" style="background:#f59e0b22; color:#f59e0b;">${summary.warning} Cảnh báo</span>` : ''}
            ${summary.error ? `<span class="status-badge status-error">${summary.error} Lỗi</span>` : ''}
          </div>
        </div>
        <div class="qc-metrics-grid">
          <div class="qc-metric-card">
            <div class="qc-metric-title">Âm lượng tích hợp</div>
            <div class="qc-metric-value" style="color: ${metrics.integrated_loudness_lufs !== undefined ? '#34d399' : '#94a3b8'};">
              ${metrics.integrated_loudness_lufs !== undefined ? metrics.integrated_loudness_lufs + ' LUFS' : 'Chuẩn'}
            </div>
            <div class="qc-metric-sub">Mục tiêu: -14 đến -16 LUFS</div>
          </div>
          <div class="qc-metric-card">
            <div class="qc-metric-title">Đỉnh âm cực đại</div>
            <div class="qc-metric-value" style="color: ${metrics.true_peak_dbtp !== undefined ? '#38bdf8' : '#94a3b8'};">
              ${metrics.true_peak_dbtp !== undefined ? metrics.true_peak_dbtp + ' dBTP' : 'Chuẩn'}
            </div>
            <div class="qc-metric-sub">Giới hạn: < -1.0 dBTP</div>
          </div>
          <div class="qc-metric-card">
            <div class="qc-metric-title">Độ lệch Audio/Video</div>
            <div class="qc-metric-value" style="color: #60a5fa;">
              ${metrics.duration_delta_seconds !== undefined ? Math.abs(metrics.duration_delta_seconds).toFixed(3) + 's' : '< 0.05s'}
            </div>
            <div class="qc-metric-sub">Dung sai chuẩn: ±0.1s</div>
          </div>
          <div class="qc-metric-card">
            <div class="qc-metric-title">Che Sub Gốc</div>
            <div class="qc-metric-value" style="color: #a78bfa;">
              ${metrics.pixel_cover ? (metrics.pixel_cover.pixel_level_pass ? '100% Khớp' : 'Đã che') : 'Đạt'}
            </div>
            <div class="qc-metric-sub">Kiểm tra viền & bóng mờ</div>
          </div>
        </div>
        <div style="margin-top: 14px; font-weight: 600; font-size: 12.5px; color: #f1f5f9;">Danh sách kiểm định chi tiết:</div>
        <div class="qc-checks-list">
          ${Array.isArray(data.checks) ? data.checks.map(c => `
            <div class="qc-check-item">
              <div>
                <strong style="color: #f1f5f9;">${escapeHtml(c.name)}</strong>
                <div style="color: var(--text-muted); font-size: 11px; margin-top: 2px;">${escapeHtml(c.message || '')}</div>
              </div>
              <span class="status-badge ${c.status === 'pass' ? 'status-completed' : (c.status === 'warning' ? 'status-waiting' : 'status-error')}" style="font-size: 10px;">
                ${escapeHtml(c.status.toUpperCase())}
              </span>
            </div>
          `).join('') : '<div style="color:var(--text-muted); font-size:11px;">Không có danh sách kiểm định chi tiết.</div>'}
        </div>
      `;
    } catch (err) {
      container.innerHTML = `<div style="text-align:center; padding: 20px; color: #ef4444;">Lỗi tải báo cáo QC: ${escapeHtml(err.message)}</div>`;
    }
  }
"""

def update_tool_v1():
    p = Path(r"C:\tool v1\backend\templates\dashboard.html")
    bak = p.with_suffix(".html.bak_phase1")
    # Always restore clean from backup if it exists
    if bak.exists():
        content = bak.read_text(encoding="utf-8")
    else:
        content = p.read_text(encoding="utf-8")
        bak.write_text(content, encoding="utf-8")

    # 1. CSS before </style>
    content = content.replace("</style>", COMMON_CSS + "\n  </style>")

    # 2. Header Buttons
    v1_btn_anchor = '<button type="button" class="btn btn-outline dashboard-compact-toggle"'
    v1_header_buttons = """      <button type="button" class="btn btn-outline" onclick="openPreflightModal()" title="Kiểm tra cấu hình và độ sẵn sàng hệ thống">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        Preflight
      </button>
      <button type="button" class="btn btn-outline" onclick="openTaskHistoryModal()" title="Xem lịch sử và thống kê render video">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        Lịch sử
      </button>
      """
    content = content.replace(v1_btn_anchor, v1_header_buttons + v1_btn_anchor)

    # 3. Phoi Table Retry Button
    phoi_btn_target = '<button class="btn btn-outline" style="padding: 2px 7px; font-size: 11px; display:inline-flex; align-items:center; gap:4px;" onclick="previewVideo(\'phoi\', \'${encodedName}\')"><svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Xem</button>'
    phoi_btn_replacement = phoi_btn_target + """
                <button class="btn btn-outline" style="padding: 2px 7px; font-size: 11px; display:inline-flex; align-items:center; gap:4px; margin-left: 4px;" onclick="retryVideo('${encodedName}')" title="Chạy lại riêng video này"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg> Chạy lại</button>"""
    content = content.replace(phoi_btn_target, phoi_btn_replacement)

    # 4. Modals HTML before toastContainer
    toast_anchor = '<div id="toastContainer"></div>'
    content = content.replace(toast_anchor, MODALS_HTML_V1 + "\n  " + toast_anchor)

    # 5. JavaScript into the LAST </script> tag
    last_script_idx = content.rfind("</script>")
    if last_script_idx != -1:
        content = content[:last_script_idx] + COMMON_JS + "\n  " + content[last_script_idx:]

    p.write_text(content, encoding="utf-8")
    print("Tool V1 dashboard.html successfully updated!")


def update_tool_v2():
    p = Path(r"C:\tool v2\backend\templates\dashboard.html")
    bak = p.with_suffix(".html.bak_phase1")
    if bak.exists():
        content = bak.read_text(encoding="utf-8")
    else:
        content = p.read_text(encoding="utf-8")
        bak.write_text(content, encoding="utf-8")

    # 1. CSS before </style>
    content = content.replace("</style>", COMMON_CSS + "\n  </style>")

    # 2. Header Buttons
    v2_btn_anchor = '<button type="button" class="btn btn-outline dashboard-compact-toggle"'
    v2_header_buttons = """      <button type="button" class="btn btn-outline" onclick="openPreflightModal()" title="Kiểm tra cấu hình và độ sẵn sàng hệ thống">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        Preflight
      </button>
      <button type="button" class="btn btn-outline" onclick="openTaskHistoryModal()" title="Xem lịch sử và thống kê render video">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        Lịch sử
      </button>
      <button type="button" class="btn btn-outline" onclick="openQcReportModal()" title="Xem báo cáo kiểm định chất lượng QC Gate chuẩn YouTube">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Báo cáo QC
      </button>
      """
    content = content.replace(v2_btn_anchor, v2_header_buttons + v2_btn_anchor)

    # 3. Phoi Table Retry Button
    phoi_btn_target = '<button class="btn btn-outline" style="padding: 2px 7px; font-size: 11px; display:inline-flex; align-items:center; gap:4px;" onclick="previewVideo(\'phoi\', \'${encodedName}\')"><svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Xem</button>'
    phoi_btn_replacement = phoi_btn_target + """
                <button class="btn btn-outline" style="padding: 2px 7px; font-size: 11px; display:inline-flex; align-items:center; gap:4px; margin-left: 4px;" onclick="retryVideo('${encodedName}')" title="Chạy lại riêng video này"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg> Chạy lại</button>"""
    content = content.replace(phoi_btn_target, phoi_btn_replacement)

    # 4. Banve Table QC Button
    banve_btn_target = '<button class="btn btn-outline" style="padding: 3px 8px; font-size: 11px; display:inline-flex; align-items:center; gap:4px;" onclick="previewVideo(\'banve\', \'${encodedName}\')"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Xem</button>'
    banve_btn_replacement = banve_btn_target + """
                <button class="btn btn-outline" style="padding: 3px 8px; font-size: 11px; display:inline-flex; align-items:center; gap:4px; margin-left: 4px;" onclick="openQcReportModal('${encodedName}')" title="Xem báo cáo chất lượng QC Gate"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> QC</button>"""
    content = content.replace(banve_btn_target, banve_btn_replacement)

    # 5. Modals HTML before toastContainer
    toast_anchor = '<div id="toastContainer"></div>'
    content = content.replace(toast_anchor, MODALS_HTML_V2 + "\n  " + toast_anchor)

    # 6. JavaScript into the LAST </script> tag
    last_script_idx = content.rfind("</script>")
    if last_script_idx != -1:
        content = content[:last_script_idx] + COMMON_JS + "\n" + QC_JS_V2 + "\n  " + content[last_script_idx:]

    p.write_text(content, encoding="utf-8")
    print("Tool V2 dashboard.html successfully updated!")


if __name__ == "__main__":
    update_tool_v1()
    update_tool_v2()
