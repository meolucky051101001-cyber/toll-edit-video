"use client";

import { useEffect, useState } from "react";
import {
  KeyRound,
  ShieldCheck,
  CheckCircle2,
  AlertTriangle,
  RotateCcw,
  Save,
  Check,
  Sparkles,
  Info,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  getCookieSettings,
  saveCookieSettings,
  syncBrowserCookies,
} from "@/lib/api";
import type { CookiePlatformStatus, CookieSettingsData } from "@/types";

export function CookieSettings() {
  const [data, setData] = useState<CookieSettingsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyXhs, setBusyXhs] = useState(false);
  const [busyDy, setBusyDy] = useState(false);
  const [busySyncAll, setBusySyncAll] = useState(false);

  const [editXhs, setEditXhs] = useState(false);
  const [editDy, setEditDy] = useState(false);
  const [inputXhs, setInputXhs] = useState("");
  const [inputDy, setInputDy] = useState("");

  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const loadSettings = async () => {
    try {
      setLoading(true);
      const res = await getCookieSettings();
      setData(res);
    } catch {
      setMessage({
        type: "error",
        text: "Không tải được trạng thái Cookie. Hãy kiểm tra kết nối backend.",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSettings();
  }, []);

  const handleSaveXhs = async () => {
    setBusyXhs(true);
    setMessage(null);
    try {
      const res = await saveCookieSettings({ xhs_cookie: inputXhs });
      setData(res);
      setEditXhs(false);
      setInputXhs("");
      setMessage({
        type: "success",
        text: "Đã lưu Cookie Xiaohongshu thành công!",
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err instanceof Error ? err.message : "Lỗi khi lưu Cookie Xiaohongshu.",
      });
    } finally {
      setBusyXhs(false);
    }
  };

  const handleSaveDy = async () => {
    setBusyDy(true);
    setMessage(null);
    try {
      const res = await saveCookieSettings({ douyin_cookie: inputDy });
      setData(res);
      setEditDy(false);
      setInputDy("");
      setMessage({
        type: "success",
        text: "Đã lưu Cookie Douyin và đồng bộ sang downloader thành công!",
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err instanceof Error ? err.message : "Lỗi khi lưu Cookie Douyin.",
      });
    } finally {
      setBusyDy(false);
    }
  };

  const handleSyncPlatform = async (platform: "xiaohongshu" | "douyin") => {
    if (platform === "xiaohongshu") setBusyXhs(true);
    else setBusyDy(true);
    setMessage(null);
    try {
      const res = await syncBrowserCookies(platform);
      setData(res.cookies);
      setMessage({
        type: res.ok ? "success" : "error",
        text: res.message,
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err instanceof Error ? err.message : "Lỗi khi đồng bộ từ trình duyệt.",
      });
    } finally {
      if (platform === "xiaohongshu") setBusyXhs(false);
      else setBusyDy(false);
    }
  };

  const handleSyncAll = async () => {
    setBusySyncAll(true);
    setMessage(null);
    try {
      const res = await syncBrowserCookies("all");
      setData(res.cookies);
      setMessage({
        type: res.ok ? "success" : "error",
        text: res.message,
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err instanceof Error ? err.message : "Lỗi khi đồng bộ từ trình duyệt.",
      });
    } finally {
      setBusySyncAll(false);
    }
  };

  if (loading && !data) {
    return (
      <div className="cookie-settings-loading">
        <Loader2 size={20} className="animate-spin text-accent" />
        <span>Đang kiểm tra trạng thái Cookie...</span>
      </div>
    );
  }

  const xhs = data?.xiaohongshu;
  const douyin = data?.douyin;

  return (
    <div className="cookie-settings-container">
      {/* Educational Notice based on open source findings */}
      <div className="cookie-notice-banner">
        <div className="notice-icon">
          <ShieldCheck size={20} />
        </div>
        <div className="notice-body">
          <strong>Cơ chế bảo vệ bản quyền & độ phân giải tối đa (1080p / Gốc)</strong>
          <p>
            Cả Douyin và Xiaohongshu đều áp dụng thuật toán quét bot khắt khe. Có Cookie từ tài khoản
            thực tế giúp mở khóa luồng video gốc uncompressed trực tiếp từ CDN (
            <code>originVideoKey</code> của Xiaohongshu và <code>1080p HQ</code> không watermark của
            Douyin), đồng thời tránh tình trạng bị nghẽn IP hoặc chặn tìm kiếm.
          </p>
        </div>
      </div>

      {message && (
        <div
          className={`cookie-feedback-alert ${
            message.type === "success" ? "feedback-success" : "feedback-error"
          }`}
        >
          {message.type === "success" ? (
            <CheckCircle2 size={16} />
          ) : (
            <AlertTriangle size={16} />
          )}
          <span>{message.text}</span>
        </div>
      )}

      {/* Global Quick Action */}
      <div className="cookie-quick-sync-strip">
        <div className="quick-sync-info">
          <Sparkles size={16} className="text-accent" />
          <span>
            Đã đăng nhập ở cửa sổ trình duyệt? Bấm để trích xuất tự động vào cấu hình mà không cần copy tay.
          </span>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleSyncAll}
          disabled={busySyncAll}
          className="sync-all-btn"
        >
          {busySyncAll ? (
            <Loader2 size={14} className="animate-spin mr-1" />
          ) : (
            <RotateCcw size={14} className="mr-1" />
          )}
          <span>Đồng bộ từ trình duyệt (Cả 2 nền tảng)</span>
        </Button>
      </div>

      <div className="cookie-platforms-grid">
        {/* Xiaohongshu Card */}
        <div className="cookie-platform-card">
          <div className="platform-card-header">
            <div className="platform-title-row">
              <span className="platform-tag tag-xhs">Xiaohongshu / RedNote</span>
              <span
                className={`cookie-grade-badge ${
                  xhs?.grade === "high_quality"
                    ? "grade-high"
                    : xhs?.grade === "basic"
                      ? "grade-basic"
                      : "grade-missing"
                }`}
              >
                {xhs?.grade === "high_quality"
                  ? "✓ Video Gốc (originVideoKey / 1080p)"
                  : xhs?.grade === "basic"
                    ? "Cơ bản"
                    : "Chưa cấu hình"}
              </span>
            </div>
            <p className="platform-desc">{xhs?.status_text}</p>
          </div>

          <div className="cookie-tokens-row">
            <span className="tokens-label">Token phát hiện:</span>
            <div className="tokens-pills">
              {["web_session", "a1", "webId"].map((tok) => {
                const detected = xhs?.detected_essential.includes(tok);
                return (
                  <span
                    key={tok}
                    className={`token-pill ${detected ? "token-active" : "token-inactive"}`}
                    title={detected ? `Đã có token ${tok}` : `Thiếu token ${tok}`}
                  >
                    {detected ? <Check size={11} /> : "—"} {tok}
                  </span>
                );
              })}
            </div>
          </div>

          <div className="cookie-preview-box">
            <div className="preview-label-row">
              <span>Giá trị hiện tại:</span>
              <span className="token-count">{xhs?.token_count ?? 0} cookies</span>
            </div>
            <code className="cookie-masked-code">{xhs?.preview || "Chưa có"}</code>
          </div>

          <div className="platform-card-actions">
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleSyncPlatform("xiaohongshu")}
              disabled={busyXhs}
              title="Lấy cookie tự động từ phiên trình duyệt Xiaohongshu"
            >
              {busyXhs ? (
                <Loader2 size={13} className="animate-spin mr-1" />
              ) : (
                <RotateCcw size={13} className="mr-1" />
              )}
              <span>Đồng bộ từ Trình duyệt</span>
            </Button>
            <Button
              variant={editXhs ? "default" : "outline"}
              size="sm"
              onClick={() => setEditXhs(!editXhs)}
            >
              <KeyRound size={13} className="mr-1" />
              <span>{editXhs ? "Đóng nhập" : "Dán Cookie thủ công"}</span>
            </Button>
          </div>

          {editXhs && (
            <div className="cookie-edit-area">
              <label htmlFor="xhs-cookie-input">
                Dán Cookie Xiaohongshu (chuỗi <code>a1=...; web_session=...</code>):
              </label>
              <textarea
                id="xhs-cookie-input"
                rows={3}
                placeholder="Dán toàn bộ Cookie từ trình duyệt hoặc DevTools vào đây..."
                value={inputXhs}
                onChange={(e) => setInputXhs(e.target.value)}
              />
              <div className="edit-actions-row">
                <Button
                  size="sm"
                  onClick={handleSaveXhs}
                  disabled={busyXhs || !inputXhs.trim()}
                >
                  {busyXhs ? (
                    <Loader2 size={13} className="animate-spin mr-1" />
                  ) : (
                    <Save size={13} className="mr-1" />
                  )}
                  <span>Lưu Cookie Xiaohongshu</span>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setEditXhs(false);
                    setInputXhs("");
                  }}
                >
                  Hủy
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Douyin Card */}
        <div className="cookie-platform-card">
          <div className="platform-card-header">
            <div className="platform-title-row">
              <span className="platform-tag tag-douyin">Douyin (抖音)</span>
              <span
                className={`cookie-grade-badge ${
                  douyin?.grade === "high_quality"
                    ? "grade-high"
                    : douyin?.grade === "basic"
                      ? "grade-basic"
                      : "grade-missing"
                }`}
              >
                {douyin?.grade === "high_quality"
                  ? "✓ 1080p Không Watermark"
                  : douyin?.grade === "basic"
                    ? "Cơ bản"
                    : "Chưa cấu hình"}
              </span>
            </div>
            <p className="platform-desc">{douyin?.status_text}</p>
          </div>

          <div className="cookie-tokens-row">
            <span className="tokens-label">Token phát hiện:</span>
            <div className="tokens-pills">
              {["ttwid", "s_v_web_id", "sessionid"].map((tok) => {
                const detected = douyin?.detected_essential.includes(tok);
                return (
                  <span
                    key={tok}
                    className={`token-pill ${detected ? "token-active" : "token-inactive"}`}
                    title={detected ? `Đã có token ${tok}` : `Thiếu token ${tok}`}
                  >
                    {detected ? <Check size={11} /> : "—"} {tok}
                  </span>
                );
              })}
            </div>
          </div>

          <div className="cookie-preview-box">
            <div className="preview-label-row">
              <span>Giá trị hiện tại:</span>
              <span className="token-count">{douyin?.token_count ?? 0} cookies</span>
            </div>
            <code className="cookie-masked-code">{douyin?.preview || "Chưa có"}</code>
          </div>

          <div className="platform-card-actions">
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleSyncPlatform("douyin")}
              disabled={busyDy}
              title="Lấy cookie tự động từ phiên trình duyệt Douyin"
            >
              {busyDy ? (
                <Loader2 size={13} className="animate-spin mr-1" />
              ) : (
                <RotateCcw size={13} className="mr-1" />
              )}
              <span>Đồng bộ từ Trình duyệt</span>
            </Button>
            <Button
              variant={editDy ? "default" : "outline"}
              size="sm"
              onClick={() => setEditDy(!editDy)}
            >
              <KeyRound size={13} className="mr-1" />
              <span>{editDy ? "Đóng nhập" : "Dán Cookie thủ công"}</span>
            </Button>
          </div>

          {editDy && (
            <div className="cookie-edit-area">
              <label htmlFor="dy-cookie-input">
                Dán Cookie Douyin (chuỗi <code>ttwid=...; s_v_web_id=...</code>):
              </label>
              <textarea
                id="dy-cookie-input"
                rows={3}
                placeholder="Dán toàn bộ Cookie từ trình duyệt Douyin hoặc DevTools vào đây..."
                value={inputDy}
                onChange={(e) => setInputDy(e.target.value)}
              />
              <div className="edit-actions-row">
                <Button
                  size="sm"
                  onClick={handleSaveDy}
                  disabled={busyDy || !inputDy.trim()}
                >
                  {busyDy ? (
                    <Loader2 size={13} className="animate-spin mr-1" />
                  ) : (
                    <Save size={13} className="mr-1" />
                  )}
                  <span>Lưu & Đồng bộ Douyin</span>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setEditDy(false);
                    setInputDy("");
                  }}
                >
                  Hủy
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
