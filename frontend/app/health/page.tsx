"use client";

import { useResource } from "@/lib/use-resource";
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
} from "lucide-react";
import { Button } from "@/components/ui/button";

export default function HealthPage() {
  const health = useResource<Health>("/health", 3000);
  const queue = useResource<QueueStatusResponse>("/system/queue-status", 3000);
  const metrics = useResource<SystemMetricsResponse>("/system/metrics", 3000);
  const readiness = useResource<ReleaseReadinessResponse>("/system/release-readiness", 5000);
  const platforms = useResource<PlatformsResponse>("/platforms", 10000);

  function refreshAll() {
    health.refresh();
    queue.refresh();
    metrics.refresh();
    readiness.refresh();
    platforms.refresh();
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
      <div className="page-heading" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h1>Giám sát hệ thống & Chất lượng (Release Gate)</h1>
          <p>Trạng thái tải, hàng đợi tác vụ nền, độ trễ và ma trận nền tảng thời gian thực.</p>
        </div>
        <Button variant="outline" size="sm" onClick={refreshAll} style={{ gap: "0.35rem" }}>
          <RefreshCw size={14} />
          Làm mới
        </Button>
      </div>

      <ErrorBanner message={health.error || queue.error || metrics.error} />

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
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.35rem",
              color: readiness.data?.is_ready ? "#16a34a" : "#ca8a04",
            }}
          >
            <ShieldCheck size={18} />
            {readiness.data?.status === "ready"
              ? "APPROVED"
              : readiness.data?.status === "degraded"
              ? "DEGRADED"
              : "CHECKING"}
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
        <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.1rem" }}>
          <ShieldCheck size={20} color="#16a34a" />
          Tiêu chuẩn nghiệm thu phát hành (Release Gate Checks)
        </h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "0.75rem", marginTop: "0.75rem" }}>
          {readiness.data?.checks?.map((chk) => (
            <div
              key={chk.name}
              style={{
                padding: "0.75rem 1rem",
                borderRadius: "8px",
                border: "1px solid #e2e8f0",
                background: chk.passed ? "#f0fdf4" : "#fef2f2",
                display: "flex",
                alignItems: "flex-start",
                gap: "0.6rem",
              }}
            >
              {chk.passed ? (
                <CheckCircle2 size={18} color="#16a34a" style={{ marginTop: "0.15rem", flexShrink: 0 }} />
              ) : (
                <AlertTriangle size={18} color="#dc2626" style={{ marginTop: "0.15rem", flexShrink: 0 }} />
              )}
              <div>
                <strong style={{ fontSize: "0.85rem", color: chk.passed ? "#15803d" : "#b91c1c", display: "block" }}>
                  {chk.name}
                </strong>
                <span style={{ fontSize: "0.75rem", color: "#64748b" }}>{chk.details}</span>
              </div>
            </div>
          ))}
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
