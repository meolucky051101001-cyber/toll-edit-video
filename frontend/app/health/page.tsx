"use client";

import { useState } from "react";
import { useResource } from "@/lib/use-resource";
import { api } from "@/lib/api";
import { ErrorBanner } from "@/components/error-banner";
import type {
  Health,
  PlatformsResponse,
  QueueStatusResponse,
  ReleaseReadinessResponse,
  SystemMetricsResponse,
} from "@/types";
import {
  Activity,
  CheckCircle2,
  Cpu,
  Layers,
  Server,
  ShieldCheck,
  AlertTriangle,
  Clock,
  Gauge,
  ExternalLink,
  RefreshCw,
  Globe,
  Radio,
} from "lucide-react";
import { Button } from "@/components/ui/button";

export default function HealthPage() {
  const health = useResource<Health>("/health", 3000);
  const queue = useResource<QueueStatusResponse>("/system/queue-status", 3000);
  const metrics = useResource<SystemMetricsResponse>("/system/metrics", 3000);
  const readiness = useResource<ReleaseReadinessResponse>("/system/release-readiness", 5000);
  const platforms = useResource<PlatformsResponse>("/platforms", 10000);

  const [isProbingLive, setIsProbingLive] = useState(false);
  const [liveProbeData, setLiveProbeData] = useState<ReleaseReadinessResponse | null>(null);
  const [liveProbeError, setLiveProbeError] = useState<string | null>(null);

  const activeReadiness = liveProbeData || readiness.data;

  async function handleRunLiveProbe() {
    setIsProbingLive(true);
    setLiveProbeError(null);
    setLiveProbeData(null);
    try {
      const res = await api<ReleaseReadinessResponse>("/system/release-readiness?probe=true", {
        signal: AbortSignal.timeout(35000),
      });
      setLiveProbeData(res);
      readiness.refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setLiveProbeError(msg);
      setLiveProbeData(null);
    } finally {
      setIsProbingLive(false);
    }
  }

  function refreshAll() {
    health.refresh();
    queue.refresh();
    metrics.refresh();
    readiness.refresh();
    platforms.refresh();
    setLiveProbeError(null);
    setLiveProbeData(null);
  }

  const formatUptime = (seconds?: number) => {
    if (!seconds) return "—";
    const m = Math.floor(seconds / 60);
    const h = Math.floor(m / 60);
    if (h > 0) return `${h}h ${m % 60}m`;
    return `${m}m ${Math.floor(seconds % 60)}s`;
  };

  return (
    <>
      <div className="page-heading" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: "0.75rem" }}>
        <div>
          <h1>Giám sát hệ thống & Chất lượng (Release Gate)</h1>
          <p>Trạng thái tải, hàng đợi tác vụ nền, độ trễ và ma trận nền tảng thời gian thực.</p>
        </div>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <Button
            variant="default"
            size="sm"
            onClick={handleRunLiveProbe}
            disabled={isProbingLive}
            data-testid="run-live-probe-btn"
            style={{ gap: "0.35rem" }}
          >
            <Globe size={14} className={isProbingLive ? "animate-spin" : ""} />
            {isProbingLive ? "Đang kiểm tra kết nối thật…" : "Kiểm tra kết nối live"}
          </Button>
          <Button variant="outline" size="sm" onClick={refreshAll} style={{ gap: "0.35rem" }}>
            <RefreshCw size={14} />
            Làm mới
          </Button>
        </div>
      </div>

      {(health.error || queue.error || metrics.error || liveProbeError) ? (
        <ErrorBanner message={health.error || queue.error || metrics.error || liveProbeError || ""} />
      ) : null}

      {/* Overview Cards */}
      <div className="stats-strip" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", marginBottom: "1.5rem" }}>
        <div>
          <span>Trạng thái Backend</span>
          <strong style={{ display: "flex", alignItems: "center", gap: "0.35rem", color: health.data ? "#16a34a" : "#dc2626" }}>
            <Server size={18} />
            {health.data ? "Online" : "Đang kết nối…"}
          </strong>
        </div>

        <div>
          <span>Release Readiness</span>
          <strong
            data-testid="release-readiness-status"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.35rem",
              color: (() => {
                const s = activeReadiness?.status?.toUpperCase();
                if (s === "READY") return "#16a34a";
                if (s === "DEGRADED") return "#ca8a04";
                if (s === "BLOCKED" || s === "NOT_READY") return "#dc2626";
                return "#64748b";
              })(),
            }}
          >
            <ShieldCheck size={18} />
            {(() => {
              const s = activeReadiness?.status?.toUpperCase();
              if (s === "READY") return "APPROVED";
              if (s === "DEGRADED") return "DEGRADED";
              if (s === "BLOCKED") return "BLOCKED";
              if (s === "NOT_READY") return "NOT READY";
              return "CHECKING";
            })()}
          </strong>
        </div>

        <div>
          <span>Hàng đợi (Queue Size)</span>
          <strong style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
            <Layers size={18} />
            {queue.data ? `${queue.data.current_size} / ${queue.data.max_size}` : "—"}
          </strong>
        </div>

        <div>
          <span>Worker Pool</span>
          <strong style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
            <Cpu size={18} />
            {queue.data ? `${queue.data.active_workers} / ${queue.data.max_workers}` : "—"}
          </strong>
        </div>

        <div>
          <span>Uptime</span>
          <strong style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
            <Clock size={18} />
            {formatUptime(metrics.data?.uptime_seconds)}
          </strong>
        </div>
      </div>

      {/* Grid: 2 columns */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: "1.5rem" }}>
        {/* Scale Infrastructure & Queue */}
        <section className="settings-panel" style={{ margin: 0 }}>
          <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.1rem" }}>
            <Layers size={20} color="#0f766e" />
            Hạ tầng mở rộng (Phase 10: Scale Infrastructure)
          </h2>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem", marginTop: "0.75rem", fontSize: "0.9rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Cơ chế hàng đợi:</span>
              <strong>{queue.data?.backend || "in_memory_priority"}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Độ đầy hàng đợi:</span>
              <span>{queue.data ? `${Math.round((queue.data.current_size / queue.data.max_size) * 100)}%` : "—"}</span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Dead Letter Queue (Lỗi cần can thiệp):</span>
              <span style={{ color: (queue.data?.dead_letter_size || 0) > 0 ? "#dc2626" : "#16a34a", fontWeight: 600 }}>
                {queue.data?.dead_letter_size ?? 0} jobs
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Trạng thái quá tải (Is Full):</span>
              <span style={{ color: queue.data?.is_full ? "#dc2626" : "#16a34a", fontWeight: 600 }}>
                {queue.data?.is_full ? "Cảnh báo đầy" : "Bình thường"}
              </span>
            </div>
          </div>
        </section>

        {/* Observability & Telemetry */}
        <section className="settings-panel" style={{ margin: 0 }}>
          <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.1rem" }}>
            <Gauge size={20} color="#6366f1" />
            Giám sát độ trễ & Lỗi (Phase 11: Telemetry)
          </h2>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem", marginTop: "0.75rem", fontSize: "0.9rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Tổng số yêu cầu API:</span>
              <strong>{metrics.data?.total_requests ?? 0}</strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Tỷ lệ lỗi (Error Rate):</span>
              <strong style={{ color: (metrics.data?.error_rate || 0) > 0.05 ? "#dc2626" : "#16a34a" }}>
                {metrics.data ? `${(metrics.data.error_rate * 100).toFixed(1)}% (${metrics.data.error_count} lỗi)` : "0%"}
              </strong>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="muted">Tác vụ đang chạy:</span>
              <strong>{metrics.data?.active_jobs ?? 0}</strong>
            </div>
          </div>

          {metrics.data?.latencies && Object.keys(metrics.data.latencies).length > 0 && (
            <div style={{ marginTop: "1rem", borderTop: "1px solid #f1f5f9", paddingTop: "0.75rem" }}>
              <h4 style={{ fontSize: "0.85rem", fontWeight: 600, color: "#64748b", marginBottom: "0.5rem" }}>
                Biểu đồ phân phối độ trễ (Latency Distribution):
              </h4>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                {Object.entries(metrics.data.latencies).map(([op, stat]) => (
                  <div key={op} style={{ fontSize: "0.8rem", background: "#f8fafc", padding: "0.4rem 0.6rem", borderRadius: "4px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontWeight: 600 }}>
                      <span>{op}</span>
                      <span>avg: {stat.avg_ms.toFixed(1)}ms ({stat.count} samples)</span>
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", color: "#64748b", marginTop: "0.15rem" }}>
                      <span>p50: {stat.p50_ms.toFixed(1)}ms</span>
                      <span>p95: {stat.p95_ms.toFixed(1)}ms</span>
                      <span>p99: {stat.p99_ms.toFixed(1)}ms</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>

      {/* Release Readiness Checks */}
      <section className="settings-panel" style={{ marginTop: "1.5rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
          <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.1rem", margin: 0 }}>
            <ShieldCheck size={20} color="#16a34a" />
            Tiêu chuẩn nghiệm thu phát hành (Release Gate Checks)
          </h2>
          {liveProbeData && (
            <span
              data-testid="live-probe-active-badge"
              style={{
                fontSize: "0.75rem",
                padding: "0.25rem 0.6rem",
                borderRadius: "9999px",
                backgroundColor: "#eff6ff",
                color: "#1d4ed8",
                fontWeight: 600,
                display: "inline-flex",
                alignItems: "center",
                gap: "0.3rem",
              }}
            >
              <Radio size={12} color="#2563eb" />
              Dữ liệu Live Probe trực tiếp
            </span>
          )}
        </div>

        {/* Live Network Reachability Breakdown Panel */}
        {activeReadiness?.live_probe && (
          <div
            data-testid="live-probe-breakdown"
            style={{
              marginTop: "1rem",
              padding: "0.85rem 1rem",
              borderRadius: "8px",
              border: "1px solid #bfdbfe",
              background: "#f0f9ff",
            }}
          >
            <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "#1e40af", marginBottom: "0.6rem", display: "flex", alignItems: "center", gap: "0.4rem" }}>
              <Globe size={15} />
              Trạng thái kết nối mạng thực tế & Xác thực người dùng (Live Reachability)
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "0.75rem" }}>
              {/* Douyin Live Status */}
              {activeReadiness.live_probe.douyin && (() => {
                const dy = activeReadiness.live_probe.douyin;
                const isReachable = dy.reachable;
                const sessionState = dy.session_state || (
                  dy.state.includes("verification_required") || dy.state.includes("challenge") || dy.state.includes("CAPTCHA")
                    ? "verification_required"
                    : dy.state.includes("login_required")
                    ? "login_required"
                    : dy.state.includes("authenticated") || dy.state.includes("ready")
                    ? "authenticated"
                    : dy.state.includes("page_available") || dy.state.includes("unverified")
                    ? "unverified"
                    : "unknown"
                );
                const isChallenge = sessionState === "verification_required";
                const isLogin = sessionState === "login_required";
                const isAuthenticated = sessionState === "authenticated";
                const isUnverified = sessionState === "unverified" || sessionState === "page_available";

                const dyStatusBadge = !isReachable
                  ? { label: "MẤT KẾT NỐI", bg: "#fee2e2", color: "#991b1b" }
                  : isAuthenticated
                  ? { label: "ĐÃ ĐĂNG NHẬP (SẴN SÀNG)", bg: "#dcfce7", color: "#166534" }
                  : isChallenge
                  ? { label: "CẦN XÁC MINH BẢO MẬT (CAPTCHA)", bg: "#fef3c7", color: "#92400e" }
                  : isLogin
                  ? { label: "CẦN ĐĂNG NHẬP", bg: "#fee2e2", color: "#991b1b" }
                  : isUnverified
                  ? { label: "CHƯA XÁC MINH PHIÊN", bg: "#fef3c7", color: "#92400e" }
                  : { label: "CHƯA XÁC ĐỊNH PHIÊN", bg: "#fef3c7", color: "#92400e" };

                return (
                  <div
                    data-testid="live-probe-douyin"
                    style={{
                      padding: "0.65rem 0.85rem",
                      borderRadius: "6px",
                      border: "1px solid #e2e8f0",
                      background: "#ffffff",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "0.35rem", flexWrap: "wrap", gap: "0.25rem" }}>
                      <strong style={{ fontSize: "0.85rem" }}>Douyin (Trình duyệt thật)</strong>
                      <span
                        data-testid="live-probe-douyin-badge"
                        style={{
                          fontSize: "0.7rem",
                          fontWeight: 700,
                          padding: "0.15rem 0.5rem",
                          borderRadius: "4px",
                          backgroundColor: dyStatusBadge.bg,
                          color: dyStatusBadge.color,
                        }}
                      >
                        {dyStatusBadge.label}
                      </span>
                    </div>
                    <div style={{ fontSize: "0.75rem", color: "#475569" }}>
                      {isReachable ? (
                        <span>
                          <div><strong style={{ color: "#16a34a" }}>Kết nối mạng:</strong> Đã kết nối ({dy.http_status ? `HTTP ${dy.http_status}` : dy.state.split(":")[0]?.trim() || "HTTP 200"})</div>
                          <div style={{ marginTop: "0.2rem" }}>
                            <strong style={{ color: isAuthenticated ? "#16a34a" : isChallenge || isLogin || isUnverified ? "#b45309" : "#16a34a" }}>Phiên làm việc:</strong>{" "}
                            {isAuthenticated
                              ? "Đã xác thực phiên thành công (Sẵn sàng tìm kiếm)"
                              : isChallenge
                              ? "Phát hiện thử thách xác minh bảo mật (CAPTCHA)"
                              : isLogin
                              ? "Yêu cầu đăng nhập tài khoản"
                              : isUnverified
                              ? "Chưa xác minh phiên đăng nhập (Tìm kiếm thật có thể yêu cầu CAPTCHA/đăng nhập)"
                              : dy.state}
                          </div>
                          {isChallenge && (
                            <div style={{ color: "#b45309", marginTop: "0.2rem", fontStyle: "italic" }}>
                              * Cần mở trình duyệt hoặc giải CAPTCHA trạm làm việc để lấy video trực tiếp.
                            </div>
                          )}
                          {isLogin && (
                            <div style={{ color: "#b45309", marginTop: "0.2rem", fontStyle: "italic" }}>
                              * Cần quét mã QR đăng nhập trạm làm việc để duyệt feed tìm kiếm đầy đủ.
                            </div>
                          )}
                        </span>
                      ) : (
                        <span style={{ color: "#dc2626" }}>Lỗi kết nối mạng: {dy.state}</span>
                      )}
                    </div>
                  </div>
                );
              })()}

              {/* Xiaohongshu Live Status */}
              {activeReadiness.live_probe.xiaohongshu && (() => {
                const xhs = activeReadiness.live_probe.xiaohongshu;
                const isReachable = xhs.reachable;
                const sessionState = xhs.session_state || (
                  xhs.state.includes("verification_required") || xhs.state.includes("challenge") || xhs.state.includes("CAPTCHA")
                    ? "verification_required"
                    : xhs.state.includes("login_required") || xhs.state.includes("Login")
                    ? "login_required"
                    : xhs.state.includes("authenticated") || xhs.state.includes("ready")
                    ? "authenticated"
                    : xhs.state.includes("page_available") || xhs.state.includes("unverified")
                    ? "unverified"
                    : "unknown"
                );
                const isChallenge = sessionState === "verification_required";
                const isLogin = sessionState === "login_required";
                const isUnverified = sessionState === "unverified" || sessionState === "page_available";
                const isAuthenticated = sessionState === "authenticated";

                const xhsStatusBadge = !isReachable
                  ? { label: "MẤT KẾT NỐI", bg: "#fee2e2", color: "#991b1b" }
                  : isAuthenticated
                  ? { label: "ĐÃ ĐĂNG NHẬP (SẴN SÀNG)", bg: "#dcfce7", color: "#166534" }
                  : isChallenge
                  ? { label: "CẦN XÁC MINH BẢO MẬT (CAPTCHA)", bg: "#fef3c7", color: "#92400e" }
                  : isLogin
                  ? { label: "CẦN ĐĂNG NHẬP PHIÊN", bg: "#fee2e2", color: "#991b1b" }
                  : isUnverified
                  ? { label: "CHƯA XÁC MINH PHIÊN (CẦN ĐĂNG NHẬP ĐỂ TÌM KIẾM)", bg: "#fef3c7", color: "#92400e" }
                  : { label: "CHƯA XÁC ĐỊNH PHIÊN", bg: "#fef3c7", color: "#92400e" };

                return (
                  <div
                    data-testid="live-probe-xiaohongshu"
                    style={{
                      padding: "0.65rem 0.85rem",
                      borderRadius: "6px",
                      border: "1px solid #e2e8f0",
                      background: "#ffffff",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "0.35rem", flexWrap: "wrap", gap: "0.25rem" }}>
                      <strong style={{ fontSize: "0.85rem" }}>Xiaohongshu (Trình duyệt thật)</strong>
                      <span
                        data-testid="live-probe-xiaohongshu-badge"
                        style={{
                          fontSize: "0.7rem",
                          fontWeight: 700,
                          padding: "0.15rem 0.5rem",
                          borderRadius: "4px",
                          backgroundColor: xhsStatusBadge.bg,
                          color: xhsStatusBadge.color,
                        }}
                      >
                        {xhsStatusBadge.label}
                      </span>
                    </div>
                    <div style={{ fontSize: "0.75rem", color: "#475569" }}>
                      {isReachable ? (
                        <span>
                          <div><strong style={{ color: "#16a34a" }}>Kết nối mạng:</strong> Đã kết nối ({xhs.http_status ? `HTTP ${xhs.http_status}` : xhs.state.split(":")[0]?.trim() || "HTTP 200"})</div>
                          <div style={{ marginTop: "0.2rem" }}>
                            <strong style={{ color: isAuthenticated ? "#16a34a" : isChallenge || isLogin || isUnverified ? "#b45309" : "#16a34a" }}>Phiên làm việc:</strong>{" "}
                            {isAuthenticated
                              ? "Đã xác thực phiên thành công (Sẵn sàng tìm kiếm)"
                              : isChallenge
                              ? "Phát hiện thử thách xác minh bảo mật (CAPTCHA - cần giải trên trạm làm việc)"
                              : isLogin
                              ? "Cần đăng nhập phiên (Login required)"
                              : isUnverified
                              ? "Chưa xác minh phiên đăng nhập (Tìm kiếm thật có thể bị chặn)"
                              : xhs.state}
                          </div>
                          {isChallenge && (
                            <div style={{ color: "#b45309", marginTop: "0.2rem", fontStyle: "italic" }}>
                              * Cần mở trình duyệt hoặc giải CAPTCHA trạm làm việc để duyệt tìm kiếm.
                            </div>
                          )}
                          {(isLogin || isUnverified) && (
                            <div style={{ color: "#b45309", marginTop: "0.2rem", fontStyle: "italic" }}>
                              * Cần quét mã QR đăng nhập trạm làm việc để duyệt feed tìm kiếm đầy đủ.
                            </div>
                          )}
                        </span>
                      ) : (
                        <span style={{ color: "#dc2626" }}>Lỗi kết nối mạng: {xhs.state}</span>
                      )}
                    </div>
                  </div>
                );
              })()}
            </div>
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "0.75rem", marginTop: "0.75rem" }}>
          {activeReadiness?.checks &&
            Object.entries(activeReadiness.checks).map(([name, chk]) => {
              const isPassed = chk.status === "PASS";
              const isWarn = chk.status === "WARN" || chk.status === "STANDBY";
              return (
                <div
                  key={name}
                  data-testid={`readiness-check-${name}`}
                  style={{
                    padding: "0.75rem 1rem",
                    borderRadius: "8px",
                    border: "1px solid #e2e8f0",
                    background: isPassed ? "#f0fdf4" : isWarn ? "#fefce8" : "#fef2f2",
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "0.6rem",
                  }}
                >
                  {isPassed ? (
                    <CheckCircle2 size={18} color="#16a34a" style={{ marginTop: "0.15rem", flexShrink: 0 }} />
                  ) : isWarn ? (
                    <AlertTriangle size={18} color="#ca8a04" style={{ marginTop: "0.15rem", flexShrink: 0 }} />
                  ) : (
                    <AlertTriangle size={18} color="#dc2626" style={{ marginTop: "0.15rem", flexShrink: 0 }} />
                  )}
                  <div>
                    <strong
                      style={{
                        fontSize: "0.85rem",
                        color: isPassed ? "#15803d" : isWarn ? "#a16207" : "#b91c1c",
                        display: "block",
                        textTransform: "capitalize",
                      }}
                    >
                      {name.replace(/_/g, " ")} ({chk.status})
                    </strong>
                    <span style={{ fontSize: "0.75rem", color: "#64748b" }}>{chk.detail}</span>
                  </div>
                </div>
              );
            })}
        </div>
      </section>

      {/* Multi-Platform Capabilities Matrix */}
      <section className="settings-panel" style={{ marginTop: "1.5rem" }}>
        <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.1rem" }}>
          <Activity size={20} color="#2563eb" />
          Ma trận năng lực đa nền tảng (Phase 9: Multi-Platform Architecture)
        </h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "1rem", marginTop: "0.75rem" }}>
          {platforms.data?.platforms?.map((p) => (
            <div
              key={p.id}
              style={{
                padding: "1rem",
                borderRadius: "8px",
                border: "1px solid #e2e8f0",
                background: "#fff",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
                <strong style={{ fontSize: "0.95rem" }}>{p.display_name}</strong>
                <span
                  style={{
                    fontSize: "0.7rem",
                    padding: "0.15rem 0.4rem",
                    borderRadius: "4px",
                    background: p.is_enabled ? "#dcfce7" : "#fee2e2",
                    color: p.is_enabled ? "#16a34a" : "#dc2626",
                    fontWeight: 600,
                  }}
                >
                  {p.is_enabled ? "SẴN SÀNG" : "TẮT"}
                </span>
              </div>
              <p style={{ fontSize: "0.8rem", color: "#64748b", margin: "0.3rem 0 0.6rem 0", lineHeight: 1.4 }}>
                {p.description}
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem" }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>Tìm kiếm ẩn danh (Headless):</span>
                  <strong>{p.supports_headless ? "✓ Có" : "✗"}</strong>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>Tải video không watermark:</span>
                  <strong>{p.supports_video_download ? "✓ Có" : "✗"}</strong>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>Lịch sử & Tốc độ viral:</span>
                  <strong>{p.supports_metrics ? "✓ Có" : "✗"}</strong>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Docs link */}
      <div style={{ marginTop: "1rem", textAlign: "right" }}>
        <a
          className="text-link"
          href="http://127.0.0.1:8010/docs"
          target="_blank"
          rel="noreferrer"
          style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem", fontSize: "0.9rem" }}
        >
          <span>Mở tài liệu tương tác Swagger API (/docs)</span>
          <ExternalLink size={14} />
        </a>
      </div>
    </>
  );
}
