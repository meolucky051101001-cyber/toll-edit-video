"use client";

import { useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  RefreshCw,
  TrendingUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, count } from "@/lib/api";
import { useSSEListener } from "@/lib/use-event-source";
import type { MetricHistory, Video } from "@/types";

export function VideoMetricHistory({ video }: { video: Video }) {
  const [history, setHistory] = useState<MetricHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!video.id) return;
    let isMounted = true;
    api<MetricHistory>(`/videos/${video.id}/metrics/history`)
      .then((data) => {
        if (isMounted) {
          setHistory(data);
          setError(null);
        }
      })
      .catch((e) => {
        if (isMounted) setError((e as Error).message);
      });

    return () => {
      isMounted = false;
    };
  }, [video.id]);

  // Real-time update via SSE when a new snapshot is recorded
  useSSEListener<{ video_id: string }>("video:metrics_update", (data) => {
    if (data.video_id === video.id) {
      api<MetricHistory>(`/videos/${video.id}/metrics/history`)
        .then((fresh) => setHistory(fresh))
        .catch(() => {});
    }
  });

  async function handleRefresh() {
    setRefreshing(true);
    setLoading(true);
    try {
      await api(`/videos/${video.id}/metrics/refresh`, { method: "POST" });
      const fresh = await api<MetricHistory>(`/videos/${video.id}/metrics/history`);
      setHistory(fresh);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
      setLoading(false);
    }
  }

  const snapshots = history?.snapshots ?? [];
  const latest = snapshots.length > 0 ? snapshots[snapshots.length - 1] : null;

  return (
    <div
      style={{
        margin: "12px 0",
        padding: "12px",
        borderRadius: "8px",
        background: "rgba(243, 244, 246, 0.6)",
        border: "1px solid rgba(229, 231, 235, 0.8)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          cursor: "pointer",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <TrendingUp size={16} style={{ color: "#2563eb" }} />
          <span style={{ fontWeight: 600, fontSize: "0.875rem", color: "#1f2937" }}>
            Lịch sử chỉ số tương tác (Metric Snapshots)
          </span>
          <span
            style={{
              fontSize: "0.75rem",
              background: "#e0e7ff",
              color: "#3730a3",
              padding: "2px 6px",
              borderRadius: "9999px",
              fontWeight: 500,
            }}
          >
            {snapshots.length} mốc ghi nhận
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <Button
            variant="ghost"
            size="sm"
            style={{ height: "24px", padding: "0 6px", fontSize: "0.75rem" }}
            onClick={(e) => {
              e.stopPropagation();
              handleRefresh();
            }}
            disabled={refreshing}
            title="Lấy mẫu snapshot số liệu mới ngay bây giờ"
          >
            <RefreshCw size={12} className={refreshing ? "animate-spin" : ""} />
            <span style={{ marginLeft: "4px" }}>Cập nhật mốc</span>
          </Button>
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </div>
      </div>

      {latest && !expanded && (
        <div
          style={{
            marginTop: "8px",
            fontSize: "0.75rem",
            color: "#4b5563",
            display: "flex",
            gap: "12px",
            flexWrap: "wrap",
          }}
        >
          <span>
            Gần nhất: {new Date(latest.captured_at).toLocaleTimeString("vi-VN")}{" "}
            {new Date(latest.captured_at).toLocaleDateString("vi-VN")}
          </span>
          {latest.delta_likes !== undefined && latest.delta_likes !== null && latest.delta_likes > 0 && (
            <span style={{ color: "#16a34a", fontWeight: 600 }}>
              ▲ +{count(latest.delta_likes)} lượt thích mới
            </span>
          )}
        </div>
      )}

      {error && (
        <div style={{ marginTop: "6px", color: "#dc2626", fontSize: "0.75rem" }}>
          {error}
        </div>
      )}

      {expanded && (
        <div style={{ marginTop: "12px" }}>
          {loading && snapshots.length === 0 ? (
            <div style={{ fontSize: "0.8rem", color: "#6b7280" }}>Đang tải lịch sử mốc...</div>
          ) : snapshots.length === 0 ? (
            <div style={{ fontSize: "0.8rem", color: "#6b7280" }}>
              Chưa có mốc dữ liệu nào. Nhấn &quot;Cập nhật mốc&quot; để ghi nhận lần đầu.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table
                style={{
                  width: "100%",
                  fontSize: "0.75rem",
                  borderCollapse: "collapse",
                  textAlign: "left",
                }}
              >
                <thead>
                  <tr style={{ borderBottom: "1px solid #d1d5db", color: "#4b5563" }}>
                    <th style={{ padding: "4px 6px" }}>Thời gian</th>
                    <th style={{ padding: "4px 6px" }}>Nguồn</th>
                    <th style={{ padding: "4px 6px" }}>Likes</th>
                    <th style={{ padding: "4px 6px" }}>Bình luận</th>
                    <th style={{ padding: "4px 6px" }}>Lưu/Yêu thích</th>
                    <th style={{ padding: "4px 6px" }}>Chia sẻ</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.map((snap) => {
                    const timeStr = new Date(snap.captured_at).toLocaleTimeString("vi-VN", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    });
                    const dateStr = new Date(snap.captured_at).toLocaleDateString("vi-VN");
                    return (
                      <tr key={snap.id} style={{ borderBottom: "1px solid #f3f4f6" }}>
                        <td style={{ padding: "4px 6px", whiteSpace: "nowrap" }}>
                          <span style={{ fontWeight: 500 }}>{timeStr}</span>{" "}
                          <span style={{ color: "#9ca3af" }}>{dateStr}</span>
                        </td>
                        <td style={{ padding: "4px 6px" }}>
                          <span
                            style={{
                              fontSize: "0.65rem",
                              padding: "1px 4px",
                              borderRadius: "4px",
                              background:
                                snap.source === "refresh"
                                  ? "#dcfce7"
                                  : snap.source === "import"
                                  ? "#fef3c7"
                                  : "#e0f2fe",
                              color:
                                snap.source === "refresh"
                                  ? "#166534"
                                  : snap.source === "import"
                                  ? "#92400e"
                                  : "#0369a1",
                            }}
                          >
                            {snap.source === "refresh"
                              ? "Làm mới"
                              : snap.source === "import"
                              ? "Import"
                              : "Tìm kiếm"}
                          </span>
                        </td>
                        <td style={{ padding: "4px 6px" }}>
                          {count(snap.like_count)}
                          {snap.delta_likes !== undefined &&
                            snap.delta_likes !== null &&
                            snap.delta_likes !== 0 && (
                              <span
                                style={{
                                  marginLeft: "4px",
                                  fontSize: "0.7rem",
                                  color: snap.delta_likes > 0 ? "#16a34a" : "#dc2626",
                                  fontWeight: 600,
                                }}
                              >
                                {snap.delta_likes > 0 ? `+${count(snap.delta_likes)}` : count(snap.delta_likes)}
                              </span>
                            )}
                        </td>
                        <td style={{ padding: "4px 6px" }}>
                          {count(snap.comment_count)}
                          {snap.delta_comments !== undefined &&
                            snap.delta_comments !== null &&
                            snap.delta_comments !== 0 && (
                              <span
                                style={{
                                  marginLeft: "4px",
                                  fontSize: "0.7rem",
                                  color: snap.delta_comments > 0 ? "#16a34a" : "#dc2626",
                                  fontWeight: 600,
                                }}
                              >
                                {snap.delta_comments > 0
                                  ? `+${count(snap.delta_comments)}`
                                  : count(snap.delta_comments)}
                              </span>
                            )}
                        </td>
                        <td style={{ padding: "4px 6px" }}>
                          {count(snap.favorite_count)}
                          {snap.delta_favorites !== undefined &&
                            snap.delta_favorites !== null &&
                            snap.delta_favorites !== 0 && (
                              <span
                                style={{
                                  marginLeft: "4px",
                                  fontSize: "0.7rem",
                                  color: snap.delta_favorites > 0 ? "#16a34a" : "#dc2626",
                                  fontWeight: 600,
                                }}
                              >
                                {snap.delta_favorites > 0
                                  ? `+${count(snap.delta_favorites)}`
                                  : count(snap.delta_favorites)}
                              </span>
                            )}
                        </td>
                        <td style={{ padding: "4px 6px" }}>
                          {count(snap.share_count)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
