"use client";
import { useState } from "react";
import { SlidersHorizontal, Search, Film, Download, CheckSquare } from "lucide-react";
import { useResource } from "@/lib/use-resource";
import { batchDownloadVideos } from "@/lib/api";
import type { Page, Video } from "@/types";
import { VideoCard } from "./video-card";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";

export function VideoGrid({
  jobId,
  library = false,
}: {
  jobId?: string;
  library?: boolean;
}) {
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState(library ? "saved" : "");
  const [sort, setSort] = useState("best");
  const [relevance, setRelevance] = useState("0");
  const [likes, setLikes] = useState("0");
  const [duration, setDuration] = useState("");
  const [q, setQ] = useState("");
  const [since, setSince] = useState("");
  const [page, setPage] = useState(1);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [batchDownloading, setBatchDownloading] = useState(false);

  const params = new URLSearchParams({
    page: String(page),
    sort,
    min_relevance: relevance,
    min_likes: likes,
    q,
  });
  if (jobId) params.set("job_id", jobId);
  if (platform) params.set("platform", platform);
  if (status) params.set("status", status);
  if (since) params.set("since", since);
  if (duration === "short") {
    params.set("max_duration", "30");
  } else if (duration === "medium") {
    params.set("min_duration", "30");
    params.set("max_duration", "60");
  } else if (duration === "long") {
    params.set("min_duration", "60");
  }
  const resource = useResource<Page<Video>>(
    `/videos?${params}`,
    jobId ? 1000 : 0,
  );
  // Parent remounts on job switch. Progress polling keeps incremental results visible.
  function filter(setter: (s: string) => void, value: string) {
    setter(value);
    setPage(1);
  }

  function toggleSelect(id: string) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function selectAll() {
    const valid = (resource.data?.items || [])
      .filter((v) => !v.is_mock)
      .map((v) => v.id);
    setSelectedIds(valid);
  }

  function selectTop10() {
    const valid = (resource.data?.items || [])
      .filter((v) => !v.is_mock)
      .slice(0, 10)
      .map((v) => v.id);
    setSelectedIds(valid);
  }

  function clearSelection() {
    setSelectedIds([]);
  }

  async function handleBatchDownload() {
    if (!selectedIds.length) return;
    setBatchDownloading(true);
    try {
      const jobs = await batchDownloadVideos(selectedIds);
      setNotice(`Đã thêm ${jobs.length} video vào hàng đợi tải xuống thành công.`);
      setSelectedIds([]);
      resource.refresh();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBatchDownloading(false);
    }
  }

  return (
    <section className="results-section">
      {library && (
        <div className="library-tabs">
          {[
            ["saved", "Đã lưu"],
            ["favorite", "Yêu thích"],
            ["used", "Đã dùng"],
            ["skipped", "Bỏ qua"],
            ["", "Tất cả"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={status === value ? "selected" : ""}
              onClick={() => filter(setStatus, value)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      <div className="section-heading">
        <h2>
          {library ? "Video của bạn" : "Kết quả khám phá"}{" "}
          <span className="count-badge">{resource.data?.total ?? 0}</span>
        </h2>
        <span className="muted">
          {library
            ? "Lưu lại ý tưởng cho lần tới"
            : "Xếp hạng theo mức độ phù hợp"}
        </span>
      </div>
      <div className="filter-bar">
        <SlidersHorizontal size={17} />
        <select
          aria-label="Lọc nền tảng"
          value={platform}
          onChange={(e) => filter(setPlatform, e.target.value)}
        >
          <option value="">Mọi nền tảng</option>
          <option value="douyin">Douyin</option>
          <option value="xiaohongshu">Xiaohongshu</option>
        </select>
        <select
          aria-label="Lọc thời lượng"
          value={duration}
          onChange={(e) => filter(setDuration, e.target.value)}
        >
          <option value="">Mọi thời lượng</option>
          <option value="short">Dưới 30 giây</option>
          <option value="medium">30s - 1 phút</option>
          <option value="long">Trên 1 phút</option>
        </select>
        <select
          aria-label="Mức phù hợp tối thiểu"
          value={relevance}
          onChange={(e) => filter(setRelevance, e.target.value)}
        >
          <option value="0">Mọi mức phù hợp</option>
          <option value="50">Phù hợp ≥ 50%</option>
          <option value="80">Phù hợp ≥ 80%</option>
        </select>
        <select
          aria-label="Lượt thích tối thiểu"
          value={likes}
          onChange={(e) => filter(setLikes, e.target.value)}
        >
          <option value="0">Mọi lượt thích</option>
          <option value="1000">≥ 1.000 lượt thích</option>
          <option value="5000">≥ 5.000 lượt thích</option>
        </select>
        <select
          aria-label="Sắp xếp"
          value={sort}
          onChange={(e) => filter(setSort, e.target.value)}
        >
          <option value="best">Phù hợp nhất</option>
          <option value="likes">Nhiều lượt thích</option>
          <option value="newest">Mới nhất</option>
          <option value="comments">Nhiều bình luận</option>
          <option value="favorites">Nhiều lượt lưu</option>
        </select>
      </div>
      <div className="secondary-filters">
        <label className="library-search">
          <Search size={16} />
          <input
            aria-label="Tìm trong kết quả"
            placeholder="Tìm caption, hashtag, tác giả, chủ đề…"
            value={q}
            onChange={(e) => filter(setQ, e.target.value)}
          />
        </label>
        <label className="date-filter">
          Từ ngày{" "}
          <input
            type="date"
            aria-label="Ngày đăng từ"
            value={since}
            onChange={(e) => filter(setSince, e.target.value)}
          />
        </label>
        {!library && (
          <select
            aria-label="Lọc trạng thái"
            value={status}
            onChange={(e) => filter(setStatus, e.target.value)}
          >
            <option value="">Mọi trạng thái</option>
            <option value="new">Mới</option>
            <option value="saved">Đã lưu</option>
            <option value="skipped">Bỏ qua</option>
          </select>
        )}
      </div>
      <ErrorBanner message={resource.error || actionError} />
      {notice && (
        <div
          role="status"
          className="notice-banner"
          style={{
            margin: "0.5rem 0",
            padding: "0.6rem 0.9rem",
            background: "#eff6ff",
            color: "#1e40af",
            border: "1px solid #bfdbfe",
            borderRadius: "6px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            fontSize: "0.85rem",
          }}
        >
          <span>ℹ️ {notice}</span>
          <button
            type="button"
            aria-label="Đóng thông báo"
            onClick={() => setNotice(null)}
            style={{
              background: "transparent",
              border: "none",
              color: "#1e40af",
              cursor: "pointer",
              fontWeight: 600,
              padding: "0 4px",
              marginLeft: "0.75rem",
            }}
          >
            ✕
          </button>
        </div>
      )}
      {resource.data?.items && resource.data.items.some((v) => !v.is_mock) && (
        <div
          className="batch-action-bar"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "0.75rem",
            padding: "0.55rem 0.9rem",
            background: "#f0fdfa",
            border: "1px solid #ccfbf1",
            borderRadius: "7px",
            margin: "0.5rem 0",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600, color: "#0f766e", fontSize: "0.85rem" }}>
              Tải hàng loạt ({selectedIds.length}/{resource.data.items.filter((v) => !v.is_mock).length} đã chọn):
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={selectAll}
              style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem", height: "auto" }}
            >
              Chọn tất cả
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={selectTop10}
              style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem", height: "auto" }}
            >
              Top 10 video
            </Button>
            {selectedIds.length > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={clearSelection}
                style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem", height: "auto", color: "#64748b" }}
              >
                Bỏ chọn
              </Button>
            )}
          </div>
          <div>
            <Button
              disabled={selectedIds.length === 0 || batchDownloading}
              onClick={handleBatchDownload}
              style={{
                background: selectedIds.length > 0 ? "#0f766e" : "#94a3b8",
                color: "#fff",
                fontWeight: 600,
                fontSize: "0.85rem",
                padding: "0.35rem 0.9rem",
                cursor: selectedIds.length > 0 ? "pointer" : "not-allowed",
              }}
            >
              <Download size={15} style={{ marginRight: "0.35rem" }} />
              {batchDownloading ? "Đang xếp hàng…" : `Tải ${selectedIds.length} video`}
            </Button>
          </div>
        </div>
      )}
      {resource.loading && !resource.data ? (
        <div className="loading-state">Đang tải video…</div>
      ) : resource.data?.items.length ? (
        <div className="video-grid">
          {resource.data.items.map((video) => (
            <VideoCard
              key={video.id}
              video={video}
              onChange={resource.refresh}
              onError={setActionError}
              onNotice={setNotice}
              selectable={!video.is_mock}
              isSelected={selectedIds.includes(video.id)}
              onToggleSelect={toggleSelect}
            />
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <Film size={35} />
          <h3>
            {library && status === "saved"
              ? "Một nơi cho những ý tưởng đáng giữ"
              : "Chưa có video phù hợp"}
          </h3>
          <p>
            {library && status === "saved"
              ? "Bấm Lưu trên một video ở trang Tìm kiếm. Video sẽ xuất hiện tại đây."
              : "Kết quả mới sẽ xuất hiện ở đây. Bạn cũng có thể thử nới bộ lọc."}
          </p>
        </div>
      )}
      {(resource.data?.total ?? 0) > 24 && (
        <div className="pagination">
          <Button
            variant="outline"
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
          >
            Trang trước
          </Button>
          <span>
            Trang {page} / {Math.ceil((resource.data?.total ?? 0) / 24)}
          </span>
          <Button
            variant="outline"
            disabled={page * 24 >= (resource.data?.total ?? 0)}
            onClick={() => setPage((p) => p + 1)}
          >
            Trang sau
          </Button>
        </div>
      )}
    </section>
  );
}
