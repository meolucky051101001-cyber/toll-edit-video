"use client";
import { useState } from "react";
import {
  Bookmark,
  Check,
  Copy,
  Heart,
  MessageCircle,
  Play,
  ExternalLink,
  Ban,
  Film,
  Info,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { VideoDownloadControls } from "./video-download-controls";
import { ScriptAnalysisModal } from "./script-analysis-modal";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  count,
  formatDuration,
  hasValidXhsToken,
  isTrustedPlatformUrl,
  platformName,
  statusName,
} from "@/lib/api";
import type { Video, VideoStatus } from "@/types";

export function VideoCard({
  video,
  onChange,
  onError,
  onNotice,
  selectable = false,
  isSelected = false,
  onToggleSelect,
}: {
  video: Video;
  onChange: () => void;
  onError: (e: string) => void;
  onNotice?: (notice: string | null) => void;
  selectable?: boolean;
  isSelected?: boolean;
  onToggleSelect?: (id: string) => void;
}) {
  const sourceLabel = video.is_mock
    ? "DỮ LIỆU MẪU"
    : video.imported_from
      ? "NHẬP MEDIACRAWLER"
      : "DỮ LIỆU NỀN TẢNG";
  const [imageFailed, setImageFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [scriptOpen, setScriptOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const primaryUrl =
    video.platform === "xiaohongshu" && hasValidXhsToken(video.share_url)
      ? (video.share_url as string)
      : video.share_url || video.url;

  function handleOpenPlatform(e?: React.MouseEvent) {
    if (e) e.preventDefault();
    onNotice?.(null);
    if (video.is_mock) {
      setOpen(true);
      return;
    }
    if (!isTrustedPlatformUrl(primaryUrl, video.platform)) {
      onError("Liên kết không thuộc nền tảng hợp lệ hoặc không an toàn.");
      return;
    }
    if (video.platform === "xiaohongshu" && !hasValidXhsToken(primaryUrl)) {
      // Non-blocking advisory notice: inform user that login might be required, but do NOT block window.open
      onNotice?.(
        "Lưu ý: Liên kết mở bằng URL tiêu chuẩn (thiếu xsec_token). Nếu chưa đăng nhập Xiaohongshu trên trình duyệt, nền tảng có thể yêu cầu đăng nhập."
      );
    }
    window.open(primaryUrl, "_blank", "noopener,noreferrer");
  }


  async function update(status: VideoStatus) {
    setBusy(true);
    try {
      await api(`/videos/${video.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      onChange();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(primaryUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      onError(
        "Không sao chép được. Bạn có thể chọn URL trong Chi tiết để copy thủ công.",
      );
    }
  }
  return (
    <>
      <article className="video-card" data-testid="video-card" style={{ position: "relative" }}>
        {selectable && !video.is_mock && (
          <label
            className="video-select-checkbox"
            style={{
              position: "absolute",
              top: "0.5rem",
              right: "0.5rem",
              zIndex: 10,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: isSelected ? "#0f766e" : "rgba(255, 255, 255, 0.92)",
              borderRadius: "5px",
              padding: "3px 4px",
              boxShadow: "0 1px 4px rgba(0,0,0,0.25)",
              cursor: "pointer",
            }}
            onClick={(e) => e.stopPropagation()}
            title={isSelected ? "Bỏ chọn video này" : "Chọn video này để tải hàng loạt"}
          >
            <input
              type="checkbox"
              checked={isSelected}
              onChange={() => onToggleSelect?.(video.id)}
              style={{ width: "16px", height: "16px", cursor: "pointer", accentColor: "#0f766e" }}
              aria-label={`Chọn video ${video.title || video.id}`}
            />
          </label>
        )}
        <button
          className={`thumbnail mock-tone-${(video.title?.length ?? 0) % 4}`}
          onClick={() => handleOpenPlatform()}
          aria-label={!video.is_mock ? `Mở ${video.title} trên nền tảng` : `Chi tiết ${video.title}`}
          title={
            !video.is_mock
              ? video.platform === "xiaohongshu" && !primaryUrl.includes("xsec_token=")
                ? "Mở trên Xiaohongshu (có thể yêu cầu đăng nhập)"
                : `Bấm để mở video trực tiếp trên ${platformName(video.platform)}`
              : "Xem chi tiết"
          }
        >
          <span className={`platform-badge ${video.platform}`}>
            {video.platform === "douyin" ? "♪" : "小红书"}{" "}
            {platformName(video.platform)}
          </span>
          {!video.is_mock && video.thumbnail_url && !imageFailed ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              className="import-cover"
              src={video.thumbnail_url}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              onError={() => setImageFailed(true)}
            />
          ) : (
            <div className="sample-poster">
              <Film size={34} strokeWidth={1.2} />
              <span>{video.is_mock ? "MOCK PREVIEW" : "METADATA VIDEO"}</span>
              <strong>{video.title?.split("·")[0]}</strong>
              <small>
                {video.is_mock
                  ? "Không phải video thật"
                  : "Chưa có ảnh xem trước"}
              </small>
            </div>
          )}
          {video.duration_seconds != null && formatDuration(video.duration_seconds) && (
            <span className="duration">
              <Play size={10} fill="currentColor" />
              {formatDuration(video.duration_seconds)}
            </span>
          )}
          {video.status !== "new" && (
            <span className="saved-badge">{statusName[video.status]}</span>
          )}
        </button>
        <div className="card-body">
          <div className="card-meta">
            <span>{sourceLabel}</span>
            {video.search_query && (
              <span
                className="query-pill"
                title={`Tìm bằng từ khóa: ${video.search_query}`}
              >
                🔍 {video.search_query}
              </span>
            )}
            <span className="relevance">
              {Math.round(video.relevance_score)}% phù hợp
            </span>
          </div>
          <button
            className="card-title"
            onClick={() => handleOpenPlatform()}
            title={
              !video.is_mock
                ? video.platform === "xiaohongshu" && !primaryUrl.includes("xsec_token=")
                  ? "Mở trên Xiaohongshu (có thể yêu cầu đăng nhập)"
                  : `Mở trực tiếp trên ${platformName(video.platform)}`
                : "Xem chi tiết"
            }
          >
            {video.title}
          </button>
          <p className="author">{video.author_name ?? "Chưa rõ tác giả"}</p>
          <div className="hashtags">
            {video.hashtags.slice(0, 3).map((tag) => (
              <span key={tag}>#{tag}</span>
            ))}
          </div>
          <div className="engagement">
            <span>
              <Heart size={14} />
              {count(video.like_count)}
            </span>
            <span>
              <MessageCircle size={14} />
              {count(video.comment_count)}
            </span>
            <span>
              <Bookmark size={14} />
              {count(video.favorite_count)}
            </span>
          </div>
          <div className="card-actions">
            <Button
              variant={video.status === "saved" ? "default" : "outline"}
              size="sm"
              disabled={busy}
              onClick={() => update(video.status === "saved" ? "new" : "saved")}
            >
              <Bookmark size={14} />
              {video.status === "saved" ? "Đã lưu" : "Lưu"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setScriptOpen(true)}
              title="Bóc tách kịch bản & Gợi ý Hook AI"
              aria-label="Kịch bản AI"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "0.25rem",
                background: "linear-gradient(135deg, rgba(99, 102, 241, 0.08) 0%, rgba(168, 85, 247, 0.12) 100%)",
                borderColor: "#c084fc",
                color: "#7e22ce",
                fontWeight: 600,
                fontSize: "0.8rem",
                padding: "0 8px",
              }}
            >
              <Sparkles size={13} style={{ color: "#9333ea" }} />
              <span>Kịch bản AI</span>
            </Button>
            {!video.is_mock && (
              <a
                className="button button-outline button-sm"
                href={primaryUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => handleOpenPlatform(e)}
                title={`Mở trực tiếp trên ${platformName(video.platform)}`}
                aria-label={`Mở trực tiếp trên ${platformName(video.platform)}`}
                style={{ padding: "0 7px", display: "inline-flex", alignItems: "center" }}
              >
                <ExternalLink size={14} />
              </a>
            )}
            <Button
              variant="ghost"
              size="icon"
              disabled={busy}
              onClick={() => update("skipped")}
              aria-label="Bỏ qua video"
              title="Bỏ qua video"
            >
              <Ban size={15} />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={copy}
              aria-label="Sao chép liên kết"
              title="Sao chép liên kết"
            >
              {copied ? <Check size={15} /> : <Copy size={15} />}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setOpen(true)}
              aria-label="Mở chi tiết"
              title="Mở chi tiết"
            >
              <Info size={15} />
            </Button>
          </div>
          <VideoDownloadControls video={video} />
        </div>
      </article>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <span className="eyebrow">
            {platformName(video.platform)} · {sourceLabel}
          </span>
          <DialogTitle className="modal-title">{video.title}</DialogTitle>
          <DialogDescription>{video.caption}</DialogDescription>
          <p className="hashtags">
            {video.hashtags.map((tag) => `#${tag}`).join(" ")}
          </p>
          <dl className="detail-grid">
            <dt>Tác giả</dt>
            <dd>{video.author_name ?? "—"}</dd>
            <dt>Từ khóa tìm kiếm</dt>
            <dd>{video.search_query}</dd>
            <dt>
              {video.is_mock ? "Ngày đăng (mẫu)" : "Ngày đăng trong dữ liệu"}
            </dt>
            <dd>
              {video.published_at
                ? new Date(video.published_at).toLocaleDateString("vi-VN")
                : "—"}
            </dd>
            <dt>Tương tác</dt>
            <dd>
              {count(video.like_count)} thích · {count(video.comment_count)}{" "}
              bình luận · {count(video.favorite_count)} lưu
            </dd>
            <dt>Điểm phù hợp / chất lượng / tổng</dt>
            <dd>
              {video.relevance_score} / {video.quality_score} /{" "}
              {video.final_score}
            </dd>
          </dl>
          <div className="notice">
            {video.is_mock
              ? "Đây là bản ghi mẫu để kiểm tra thao tác. Liên kết mẫu không dẫn đến video trên nền tảng."
              : video.imported_from
                ? "Metadata được nhập từ file (MediaCrawler), chưa xác minh trên nền tảng. Số liệu có thể đã cũ. Liên kết Xiaohongshu có thể yêu cầu đăng nhập trên trình duyệt để xem."
                : video.platform === "xiaohongshu" && !primaryUrl.includes("xsec_token=")
                  ? "Liên kết mở trực tiếp bằng URL tiêu chuẩn (canonical). Nền tảng có thể yêu cầu đăng nhập Xiaohongshu trên trình duyệt để xem."
                  : "Metadata đọc từ trang video. Các trường chưa đọc được để trống. Liên kết có thể yêu cầu đăng nhập Xiaohongshu."}
          </div>
          <input aria-label="URL video" value={primaryUrl} readOnly />
          <div className="modal-download-section" style={{ margin: "10px 0" }}>
            <VideoDownloadControls video={video} />
          </div>
          <div className="row wrap">
            <Button onClick={() => update("saved")} disabled={busy}>
              <Bookmark size={16} />
              Lưu video
            </Button>
            <Button
              variant="outline"
              onClick={() => update("favorite")}
              disabled={busy}
            >
              Yêu thích
            </Button>
            <Button
              variant="outline"
              onClick={() => update("used")}
              disabled={busy}
            >
              Đánh dấu đã dùng
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                setOpen(false);
                setScriptOpen(true);
              }}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "0.25rem",
                borderColor: "#c084fc",
                color: "#7e22ce",
                fontWeight: 600,
              }}
            >
              <Sparkles size={14} style={{ color: "#9333ea" }} />
              Kịch bản AI
            </Button>
            <Button variant="outline" onClick={copy}>
              {copied ? "Đã copy" : "Copy URL"}
            </Button>
            {!video.is_mock && (
              <a
                className="button button-outline"
                href={primaryUrl}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => handleOpenPlatform(e)}
                title={
                  video.platform === "xiaohongshu" && !primaryUrl.includes("xsec_token=")
                    ? "Mở trên Xiaohongshu (có thể yêu cầu đăng nhập)"
                    : undefined
                }
              >
                Mở bản gốc
                <ExternalLink size={16} />
              </a>
            )}
          </div>
        </DialogContent>
      </Dialog>
      <ScriptAnalysisModal
        video={video}
        open={scriptOpen}
        onOpenChange={setScriptOpen}
      />
    </>
  );
}
