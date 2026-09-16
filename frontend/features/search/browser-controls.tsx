"use client";

import Link from "next/link";
import { useState } from "react";
import { ExternalLink } from "lucide-react";
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
  const status = useResource<BrowserStatus>(endpoint, 5000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function action(actionPath: string) {
    setBusy(true);
    setError("");
    try {
      await api(actionPath, { method: "POST", signal: AbortSignal.timeout(45000) });
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
  const loginUrl =
    status.data?.login_url ||
    (platform === "douyin"
      ? "https://www.douyin.com"
      : "https://www.xiaohongshu.com/explore");

  const isAuthRequired =
    status.data?.state === "login_required" ||
    status.data?.state === "verification_required" ||
    (waiting && !diag?.is_authenticated);

  return (
    <div className="browser-controls">
      <p>{status.data?.message ?? "Đang kiểm tra trình duyệt…"}</p>

      {/* Direct Login Link Banner when auth is required */}
      {isAuthRequired && (
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
            <a
              href={loginUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "0.45rem",
                padding: "0.55rem 1.1rem",
                background: "#0f766e",
                color: "#ffffff",
                borderRadius: "6px",
                fontWeight: 600,
                fontSize: "0.9rem",
                textDecoration: "none",
                boxShadow: "0 2px 6px rgba(15, 118, 110, 0.25)",
              }}
            >
              <ExternalLink size={16} />
              Bấm vào đây để đăng nhập {platformLabel} ({displayHost})
            </a>

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
          <p
            style={{
              fontSize: "0.82rem",
              color: "#475569",
              margin: 0,
              lineHeight: 1.45,
            }}
          >
            💡 Liên kết sẽ mở trang chính của <strong>{platformLabel}</strong> trong tab mới trên trình duyệt của bạn. Sau khi đăng nhập (hoặc lưu Cookie trong Cài đặt), hãy bấm nút <strong>Tiếp tục lượt tìm</strong> để công cụ chạy ngầm cào video.
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
            Chẩn đoán kỹ thuật trình duyệt (Chạy ngầm):
          </div>
          <div>
            <strong>Nền tảng kết nối:</strong>{" "}
            {status.data.base_url || diag.host || displayHost}
          </div>
          <div>
            <strong>Xác thực:</strong>{" "}
            {diag.is_authenticated ? (
              <span style={{ color: "#16a34a", fontWeight: 600 }}>
                Đã đăng nhập ✓
              </span>
            ) : status.data.state === "login_required" ? (
              <span style={{ color: "#dc2626", fontWeight: 600 }}>
                Yêu cầu đăng nhập
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
        {!jobId && status.data?.open && (
          <Button
            type="button"
            variant="outline"
            disabled={busy || status.data?.busy}
            onClick={() => action(`${endpoint}/close`)}
          >
            Đóng trình duyệt riêng
          </Button>
        )}
      </div>
      <p className="muted" style={{ marginTop: "0.5rem" }}>
        Trình duyệt tìm kiếm chạy ngầm trong nền (Headless). Phiên đăng nhập được lưu an toàn trên máy này; tool không hỏi mật khẩu.
      </p>
      <ErrorBanner message={error || status.error} />
    </div>
  );
}
