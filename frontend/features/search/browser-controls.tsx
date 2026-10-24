"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  QrCode,
  Monitor,
  RotateCcw,
  X,
  Smartphone,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { api } from "@/lib/api";
import { useResource } from "@/lib/use-resource";

interface BrowserDiagnostics {
  host?: string;
  path?: string;
  query_param_names?: string[];
  title?: string;
  input_count?: number;
  search_input_value?: string;
  login_modal_visible?: boolean;
  captcha_visible?: boolean;
  captcha_detection_reason?: string | null;
  has_session_cookie?: boolean;
  has_user_element?: boolean;
  has_login_button?: boolean;
  is_authenticated?: boolean;
  result_links_count?: number;
  video_elements_count?: number;
}

interface BrowserStatus {
  state: string;
  message: string;
  profile_location: string;
  base_url?: string;
  login_url?: string;
  open: boolean;
  busy: boolean;
  diagnostics?: BrowserDiagnostics;
}

export function BrowserControls({
  jobId,
  waiting = false,
  refresh,
  platform = "xiaohongshu",
}: {
  jobId?: string;
  waiting?: boolean;
  refresh?: () => void;
  platform?: "xiaohongshu" | "douyin";
}) {
  const endpoint =
    platform === "douyin" ? "/browser/douyin" : "/browser/xiaohongshu";
  const status = useResource<BrowserStatus>(endpoint, 4000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showVerificationPreview, setShowVerificationPreview] = useState(false);
  const [showQrModal, setShowQrModal] = useState(false);
  const [qrTimestamp, setQrTimestamp] = useState(0);
  const [qrUnavailable, setQrUnavailable] = useState(false);
  const [solvingCaptcha, setSolvingCaptcha] = useState(false);
  const [solveResult, setSolveResult] = useState<string | null>(null);
  const [dragStart, setDragStart] = useState<{ x: number; y: number } | null>(null);
  const [currentDrag, setCurrentDrag] = useState<{ x: number; y: number } | null>(null);

  async function handleAutoSolve() {
    setSolvingCaptcha(true);
    setError("");
    setSolveResult(null);
    try {
      const res = await api<{
        ok: boolean;
        state: string;
        drag_distance?: number;
      }>(`${endpoint}/auto_solve_slider`, {
        method: "POST",
        signal: AbortSignal.timeout(45000),
      });
      setQrTimestamp(Date.now());
      status.refresh();
      refresh?.();
      if (res.ok) {
        setSolveResult("✅ Giải CAPTCHA thành công! Trình duyệt đã sẵn sàng.");
      } else {
        setSolveResult(
          `Đã thực hiện kéo trượt (${Math.round(res.drag_distance || 0)}px). Nếu chưa vượt qua, bạn có thể bấm giải lại hoặc kéo trượt trực tiếp trên hình bên dưới.`
        );
      }
    } catch (e) {
      setError(`Không thể tự động giải CAPTCHA: ${(e as Error).message}`);
    } finally {
      setSolvingCaptcha(false);
    }
  }

  const handleImageMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 1280;
    const y = ((e.clientY - rect.top) / rect.height) * 900;
    setDragStart({ x, y });
    setCurrentDrag({ x, y });
  };

  const handleImageMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!dragStart) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 1280;
    const y = ((e.clientY - rect.top) / rect.height) * 900;
    setCurrentDrag({ x, y });
  };

  const handleImageMouseUp = async (e: React.MouseEvent<HTMLDivElement>) => {
    if (!dragStart) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const endX = ((e.clientX - rect.left) / rect.width) * 1280;
    const endY = ((e.clientY - rect.top) / rect.height) * 900;
    const dist = Math.hypot(endX - dragStart.x, endY - dragStart.y);

    const start = dragStart;
    setDragStart(null);
    setCurrentDrag(null);

    setBusy(true);
    setError("");
    try {
      if (dist < 8) {
        await api(`${endpoint}/click`, {
          method: "POST",
          body: JSON.stringify({ x: Math.round(start.x), y: Math.round(start.y) }),
          headers: { "Content-Type": "application/json" },
        });
      } else {
        await api(`${endpoint}/drag`, {
          method: "POST",
          body: JSON.stringify({
            from_x: Math.round(start.x),
            from_y: Math.round(start.y),
            to_x: Math.round(endX),
            to_y: Math.round(endY),
          }),
          headers: { "Content-Type": "application/json" },
        });
      }
      setQrTimestamp(Date.now());
      status.refresh();
      refresh?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  async function action(actionPath: string) {
    setBusy(true);
    setError("");
    try {
      await api(actionPath, { method: "POST", signal: AbortSignal.timeout(45000) });
      setQrTimestamp(Date.now());
      status.refresh();
      refresh?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const diag = status.data?.diagnostics;
  const platformLabel = platform === "douyin" ? "Douyin" : "Xiaohongshu";
  const displayHost = platform === "douyin" ? "douyin.com" : "xiaohongshu.com";
  const isAuthenticated = Boolean(diag?.is_authenticated);

  const isAuthRequired =
    status.data?.state === "login_required" ||
    status.data?.state === "verification_required" ||
    (waiting && !isAuthenticated);

  // Auto-refresh QR code or check login status periodically when QR modal is open
  useEffect(() => {
    if (!showQrModal) return;
    const interval = setInterval(() => {
      setQrTimestamp(Date.now());
      status.refresh();
      if (status.data?.diagnostics?.is_authenticated) {
        setShowQrModal(false);
        refresh?.();
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [showQrModal, status, refresh]);

  const handleOpenQr = async () => {
    setQrUnavailable(false);
    // Wait for Chromium to open/refresh before mounting the image request.
    // Otherwise the screenshot can race the browser startup and leave a false error state.
    if (!status.data?.open) {
      await action(`${endpoint}/open`);
    } else {
      await action(`${endpoint}/refresh_qr`);
    }
    setQrTimestamp(Date.now());
    setShowQrModal(true);
  };

  return (
    <div className="browser-controls">
      <p>{status.data?.message ?? "Đang kiểm tra trình duyệt…"}</p>

      {/* QR Code Modal / Popup */}
      {showQrModal && (
        <div
          style={{
            margin: "0.85rem 0",
            padding: "1.2rem",
            background: "#ffffff",
            borderRadius: "10px",
            border: "2px solid #0f766e",
            boxShadow: "0 4px 20px rgba(15, 118, 110, 0.15)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "0.8rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <Smartphone size={18} style={{ color: "#0f766e" }} />
              <strong style={{ fontSize: "0.95rem", color: "#0f766e" }}>
                {qrUnavailable ? `Chưa có mã QR đăng nhập ${platformLabel}` : `Đăng nhập bằng mã QR ${platformLabel}`}
              </strong>
            </div>
            <button
              type="button"
              onClick={() => setShowQrModal(false)}
              style={{
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: "4px",
                color: "#64748b",
              }}
              title="Đóng"
            >
              <X size={18} />
            </button>
          </div>

          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "1.25rem",
              alignItems: "center",
            }}
          >
            <div
              style={{
                width: "210px",
                height: "210px",
                border: "1px solid #cbd5e1",
                borderRadius: "8px",
                overflow: "hidden",
                background: "#f8fafc",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {qrUnavailable ? (
                <div
                  role="status"
                  style={{ padding: "0.8rem", color: "#475569", fontSize: "0.78rem", textAlign: "center" }}
                >
                  Chưa thấy mã QR trên trang {platformLabel}. Hãy mở cửa sổ trình duyệt và đăng nhập trực tiếp.
                </div>
              ) : (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={`/api${endpoint}/screenshot?crop=qr&t=${qrTimestamp}`}
                  alt={`Mã QR ${platformLabel}`}
                  style={{ width: "100%", height: "100%", objectFit: "contain" }}
                  onLoad={() => setQrUnavailable(false)}
                  onError={() => setQrUnavailable(true)}
                />
              )}
            </div>

            <div style={{ flex: 1, minWidth: "240px", fontSize: "0.85rem", lineHeight: 1.6 }}>
              {qrUnavailable ? (
                <p role="status" style={{ margin: "0 0 0.75rem 0", color: "#475569" }}>
                  Trang hiện tại không có QR để quét. Hãy mở cửa sổ {platformLabel}, tự bấm Đăng nhập hoặc xử lý CAPTCHA trên trang đó.
                </p>
              ) : (
                <ol style={{ paddingLeft: "1.2rem", margin: "0 0 0.75rem 0", color: "#334155" }}>
                  <li>Mở app <strong>{platformLabel}</strong> trên điện thoại.</li>
                  <li>Vào phần <strong>Quét mã QR</strong> (ở góc trên cùng hoặc trang cá nhân).</li>
                  <li>Hướng camera vào mã bên cạnh để xác nhận đăng nhập.</li>
                  <li>Hệ thống sẽ <strong>tự động nhận diện khi đăng nhập thành công</strong>.</li>
                </ol>
              )}

              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    setQrUnavailable(false);
                    action(`${endpoint}/refresh_qr`);
                    setQrTimestamp(Date.now());
                  }}
                >
                  <RotateCcw size={14} className="mr-1" />
                  Đổi mã QR mới
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => action(`${endpoint}/open`)}
                >
                  <Monitor size={14} className="mr-1" />
                  Hiện cửa sổ trình duyệt lớn
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Direct Login / CAPTCHA Banner when auth or verification is required */}
      {isAuthRequired && !showQrModal && (
        <div
          style={{
            margin: "0.75rem 0",
            padding: "1rem",
            background: "#ffffff",
            borderRadius: "8px",
            border: "1px solid #ccfbf1",
            boxShadow: "0 2px 10px rgba(15, 118, 110, 0.08)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              flexWrap: "wrap",
              gap: "0.75rem",
              marginBottom: "0.5rem",
            }}
          >
            {/* Primary Action 1: Open Headful Window to solve CAPTCHA or Login directly */}
            <Button
              type="button"
              disabled={busy}
              onClick={() => action(`${endpoint}/open`)}
              style={{
                background: "#0f766e",
                color: "#ffffff",
                fontWeight: 600,
                fontSize: "0.9rem",
                padding: "0.55rem 1.1rem",
                height: "auto",
                boxShadow: "0 2px 6px rgba(15, 118, 110, 0.25)",
              }}
            >
              <Monitor size={16} className="mr-1" />
              Mở cửa sổ giải CAPTCHA / Đăng nhập
            </Button>

            {/* Primary Action 2: Quick QR Login */}
            {status.data?.state !== "verification_required" && <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={handleOpenQr}
              style={{
                borderColor: "#0f766e",
                color: "#0f766e",
                fontWeight: 600,
                fontSize: "0.9rem",
                padding: "0.55rem 1.1rem",
                height: "auto",
              }}
            >
              <QrCode size={16} className="mr-1" />
              Đăng nhập bằng mã QR
            </Button>}

            {status.data?.state === "verification_required" && <>
              <Button
                type="button"
                disabled={busy || solvingCaptcha}
                onClick={handleAutoSolve}
                style={{
                  background: "linear-gradient(135deg, #0d9488 0%, #0891b2 100%)",
                  color: "#ffffff",
                  fontWeight: 600,
                  fontSize: "0.9rem",
                  padding: "0.55rem 1.1rem",
                  height: "auto",
                  boxShadow: "0 2px 8px rgba(13, 148, 136, 0.35)",
                }}
              >
                <Zap size={16} className="mr-1" />
                {solvingCaptcha ? "Đang phân tích & giải…" : "⚡ Tự động giải CAPTCHA"}
              </Button>
              <Button type="button" variant="outline" disabled={busy || solvingCaptcha}
                onClick={() => { setQrTimestamp(Date.now()); setShowVerificationPreview((shown) => !shown); }}>
                {showVerificationPreview ? "Ẩn trang xác minh" : "Xem trang xác minh"}
              </Button>
              <Button type="button" variant="outline" disabled={busy || solvingCaptcha}
                onClick={() => action(`${endpoint}/refresh_verification`)}>
                <RotateCcw size={16} className="mr-1" /> Tải lại CAPTCHA không hiển thị
              </Button>
            </>}

            {jobId && waiting && (
              <Button
                type="button"
                disabled={busy}
                onClick={() => action(`/search/jobs/${jobId}/resume`)}
                style={{
                  background: "#16a34a",
                  color: "#ffffff",
                  fontWeight: 600,
                  fontSize: "0.9rem",
                  padding: "0.55rem 1.1rem",
                  height: "auto",
                }}
              >
                Tiếp tục lượt tìm
              </Button>
            )}
          </div>

          {solveResult && (
            <div style={{
              margin: "0.5rem 0",
              padding: "0.6rem 0.8rem",
              background: solveResult.startsWith("✅") ? "#f0fdf4" : "#fefce8",
              border: `1px solid ${solveResult.startsWith("✅") ? "#bbf7d0" : "#fef08a"}`,
              borderRadius: "6px",
              fontSize: "0.85rem",
              color: solveResult.startsWith("✅") ? "#166534" : "#854d0e",
            }}>
              {solveResult}
            </div>
          )}

          {showVerificationPreview && status.data?.state === "verification_required" && (
            <div style={{ margin: "0.75rem 0" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
                <p style={{ fontSize: "0.82rem", color: "#334155", margin: 0, fontWeight: 500 }}>
                  🎯 <strong>Giải CAPTCHA trực tiếp:</strong> Nhấn <strong>&quot;⚡ Tự động giải CAPTCHA&quot;</strong> hoặc dùng chuột <strong>kéo trượt thanh slider / bấm chọn</strong> ngay trên hình dưới:
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => setQrTimestamp(Date.now())}
                  style={{ fontSize: "0.78rem", height: "1.8rem" }}
                >
                  <RotateCcw size={13} className="mr-1" /> Làm mới ảnh
                </Button>
              </div>

              <div
                style={{
                  position: "relative",
                  display: "inline-block",
                  cursor: "crosshair",
                  border: "2px solid #0f766e",
                  borderRadius: "8px",
                  overflow: "hidden",
                  userSelect: "none",
                  backgroundColor: "#1e293b",
                }}
                onMouseDown={handleImageMouseDown}
                onMouseMove={handleImageMouseMove}
                onMouseUp={handleImageMouseUp}
                onMouseLeave={() => { setDragStart(null); setCurrentDrag(null); }}
              >
                {
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={`/api${endpoint}/screenshot?t=${qrTimestamp}`}
                    alt={`Trang xác minh ${platformLabel}`}
                    style={{
                      display: "block",
                      maxWidth: "100%",
                      maxHeight: 460,
                      objectFit: "contain",
                      pointerEvents: "none",
                    }}
                    draggable={false}
                  />
                }
                {dragStart && currentDrag && (
                  <div
                    style={{
                      position: "absolute",
                      left: `${Math.min(dragStart.x, currentDrag.x) / 12.8}%`,
                      top: `${Math.min(dragStart.y, currentDrag.y) / 9.0}%`,
                      width: `${Math.max(1, Math.abs(currentDrag.x - dragStart.x) / 12.8)}%`,
                      height: `${Math.max(2, Math.abs(currentDrag.y - dragStart.y) / 9.0)}%`,
                      border: "2px dashed #38bdf8",
                      backgroundColor: "rgba(56, 189, 248, 0.25)",
                      pointerEvents: "none",
                    }}
                  />
                )}
              </div>
            </div>
          )}
          <p
            style={{
              fontSize: "0.82rem",
              color: "#475569",
              margin: 0,
              lineHeight: 1.45,
            }}
          >
            💡 Người dùng tự xác thực trong Chromium của công cụ: quét mã QR hoặc giải CAPTCHA trực tiếp trên cửa sổ trình duyệt. Phiên đăng nhập được lưu trong hồ sơ cục bộ của công cụ, có thể hết hạn hoặc kích hoạt kiểm tra bảo mật định kỳ tùy chính sách của nền tảng.
          </p>
        </div>
      )}

      {status.data?.open && diag && (
        <div
          style={{
            margin: "0.75rem 0",
            padding: "0.75rem",
            background: "var(--surface-subtle, rgba(0,0,0,0.03))",
            borderRadius: "6px",
            fontSize: "0.85rem",
            lineHeight: 1.5,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: "0.25rem" }}>
            Chẩn đoán kỹ thuật trình duyệt ({status.data.open ? "Đang mở" : "Chạy ngầm"}):
          </div>
          <div>
            <strong>Nền tảng kết nối:</strong>{" "}
            {status.data.base_url || diag.host || displayHost}
          </div>
          <div>
            <strong>Xác thực:</strong>{" "}
            {isAuthenticated ? (
              <span style={{ color: "#16a34a", fontWeight: 600 }}>
                Đã đăng nhập ✓
              </span>
            ) : status.data.state === "login_required" ? (
              <span style={{ color: "#dc2626", fontWeight: 600 }}>
                Yêu cầu đăng nhập
              </span>
            ) : status.data.state === "verification_required" ? (
              <span style={{ color: "#b45309", fontWeight: 600 }}>
                Cần giải mã CAPTCHA
              </span>
            ) : diag.has_login_button ? (
              <span style={{ color: "#dc2626", fontWeight: 600 }}>
                Chưa đăng nhập trong trình duyệt của công cụ
              </span>
            ) : diag.has_session_cookie && !diag.has_user_element ? (
              <span style={{ color: "#b45309", fontWeight: 600 }}>
                Có cookie phiên nhưng chưa xác minh được tài khoản
              </span>
            ) : (
              "Chưa xác minh"
            )}
          </div>
          {diag.title && (
            <div>
              <strong>Tiêu đề trang:</strong> {diag.title}
            </div>
          )}
          <div>
            <strong>Trang hiện tại:</strong> {diag.path || "/"}
            {diag.query_param_names && diag.query_param_names.length > 0 && (
              <span> (tham số: {diag.query_param_names.join(", ")})</span>
            )}
          </div>
          <div>
            <strong>Liên kết bài viết:</strong> {diag.result_links_count ?? 0} ·{" "}
            <strong>Thẻ video:</strong> {diag.video_elements_count ?? 0}
          </div>
        </div>
      )}

      <div className="row wrap" style={{ marginTop: "0.75rem", gap: "0.5rem" }}>
        {jobId && waiting && !isAuthRequired && (
          <Button
            type="button"
            disabled={busy}
            onClick={() => action(`/search/jobs/${jobId}/resume`)}
            style={{
              background: "#16a34a",
              color: "#ffffff",
              fontWeight: 600,
            }}
          >
            Tiếp tục lượt tìm
          </Button>
        )}
        <Link
          href="/settings"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.35rem",
            padding: "0.4rem 0.8rem",
            borderRadius: "6px",
            border: "1px solid #cbd5e1",
            background: "#ffffff",
            color: "#334155",
            fontSize: "0.85rem",
            textDecoration: "none",
            fontWeight: 500,
          }}
        >
          ⚙️ Cài đặt Cookie
        </Link>
        <Button
          type="button"
          variant="outline"
          disabled={busy || status.data?.busy}
          onClick={() => action(`${endpoint}/open`)}
        >
          {busy ? "Đang xử lý…" : `Mở cửa sổ ${platformLabel}`}
        </Button>
        {status.data?.state !== "verification_required" && <Button
          type="button"
          variant="outline"
          disabled={busy || status.data?.busy}
          onClick={handleOpenQr}
        >
          <QrCode size={14} className="mr-1" />
          Mã QR
        </Button>}
        {!jobId && status.data?.open && (
          <Button
            type="button"
            variant="outline"
            disabled={busy || status.data?.busy}
            onClick={() => action(`${endpoint}/close`)}
          >
            Đóng trình duyệt
          </Button>
        )}
      </div>
      <p className="muted" style={{ marginTop: "0.5rem" }}>
        Trình duyệt tìm kiếm chạy ngầm hoặc hiện cửa sổ khi cần. Phiên đăng nhập được lưu an toàn trên máy này; tool không hỏi mật khẩu.
      </p>
      <ErrorBanner message={error || status.error} />
    </div>
  );
}
