"use client";

import { useEffect, useState } from "react";
import {
  Download,
  Loader2,
  CheckCircle2,
  AlertCircle,
  RotateCcw,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  cancelDownload,
  getDownloadFileUrl,
  getDownloadStatus,
  getVideoActiveDownload,
  requestVideoDownload,
} from "@/lib/api";
import type { DownloadJobOut, Video } from "@/types";

interface VideoDownloadControlsProps {
  video: Video;
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || isNaN(bytes) || bytes <= 0) return "0 MB";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) {
    return `${mb.toFixed(1)} MB`;
  }
  const kb = bytes / 1024;
  return `${kb.toFixed(0)} KB`;
}

export function VideoDownloadControls({ video }: VideoDownloadControlsProps) {
  const [job, setJob] = useState<DownloadJobOut | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [localError, setLocalError] = useState<string | null>(null);

  // Check on mount if this video has an active or completed download
  useEffect(() => {
    if (video.is_mock) return;
    let isMounted = true;

    getVideoActiveDownload(video.id)
      .then((active) => {
        if (isMounted && active) {
          setJob(active);
        }
      })
      .catch(() => {
        // Silently ignore initial fetch error
      });

    return () => {
      isMounted = false;
    };
  }, [video.id, video.is_mock]);

  // Polling when active
  const jobId = job?.id;
  const jobStatus = job?.status;

  useEffect(() => {
    if (!jobId || !jobStatus || !["queued", "resolving", "downloading"].includes(jobStatus)) {
      return;
    }

    const intervalId = setInterval(async () => {
      try {
        const current = await getDownloadStatus(jobId);
        setJob(current);
      } catch {
        // Silently continue polling on transient network errors
      }
    }, 1500);

    return () => clearInterval(intervalId);
  }, [jobId, jobStatus]);

  const handleStartDownload = async (force: boolean = false) => {
    if (video.is_mock || busy) return;
    setBusy(true);
    setLocalError(null);
    try {
      const newJob = await requestVideoDownload(video.id, force);
      setJob(newJob);
    } catch (err) {
      setLocalError(
        err instanceof Error ? err.message : "Không thể bắt đầu tải video."
      );
    } finally {
      setBusy(false);
    }
  };

  const handleCancelDownload = async () => {
    if (!job || busy) return;
    setBusy(true);
    try {
      const cancelled = await cancelDownload(job.id);
      setJob(cancelled);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Không thể hủy tải.");
    } finally {
      setBusy(false);
    }
  };

  // Mock video handling
  if (video.is_mock) {
    return (
      <div className="video-download-area" data-testid="download-controls">
        <Button
          variant="outline"
          size="sm"
          disabled
          className="download-trigger-btn opacity-60 cursor-not-allowed"
          title="Video mẫu không hỗ trợ tải về"
        >
          <Download size={13} className="mr-1" />
          <span>Tải video (mẫu không khả dụng)</span>
        </Button>
      </div>
    );
  }

  // Active states: queued, resolving, downloading
  const isActive =
    job && ["queued", "resolving", "downloading"].includes(job.status);

  return (
    <div className="video-download-area" data-testid="download-controls">
      {localError && (
        <div className="download-error-inline text-danger text-xs mb-1 flex items-center gap-1">
          <AlertCircle size={12} />
          <span>{localError}</span>
        </div>
      )}

      {/* State 1: Idle (no job or not started yet) */}
      {!job && (
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => handleStartDownload(false)}
          className="download-trigger-btn w-full"
          title="Tải video về máy tính"
        >
          {busy ? (
            <Loader2 size={13} className="animate-spin mr-1" />
          ) : (
            <Download size={13} className="mr-1" />
          )}
          <span>{busy ? "Đang gửi yêu cầu..." : "Tải video"}</span>
        </Button>
      )}

      {/* State 2: Queued or Resolving */}
      {isActive && (job.status === "queued" || job.status === "resolving") && (
        <div className="download-active-box">
          <div className="download-status-line">
            <Loader2 size={13} className="animate-spin text-accent" />
            <span className="download-status-text">
              {job.status === "queued"
                ? "Đang chờ hàng đợi..."
                : "Đang lấy địa chỉ media..."}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCancelDownload}
              disabled={busy}
              className="download-cancel-btn"
              title="Hủy tải"
            >
              <X size={13} />
              <span>Hủy</span>
            </Button>
          </div>
        </div>
      )}

      {/* State 3: Downloading with progress */}
      {isActive && job.status === "downloading" && (
        <div className="download-active-box">
          <div className="download-progress-track">
            {job.progress_percent != null ? (
              <div
                className="download-progress-fill"
                style={{
                  transform: `scaleX(${Math.max(0.03, Math.min(1, job.progress_percent / 100))})`,
                }}
              />
            ) : (
              <div className="download-progress-fill download-progress-indeterminate animate-pulse w-full opacity-80" />
            )}
          </div>
          <div className="download-info-row">
            <span className="download-bytes-text">
              {job.total_bytes && job.progress_percent != null
                ? `${Math.round(job.progress_percent)}% · ${formatBytes(job.bytes_downloaded)} / ${formatBytes(job.total_bytes)}`
                : `${formatBytes(job.bytes_downloaded)} đã tải`}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCancelDownload}
              disabled={busy}
              className="download-cancel-btn"
              title="Hủy tải"
            >
              <X size={13} />
              <span>Hủy</span>
            </Button>
          </div>
        </div>
      )}

      {/* State 4: Completed - Direct Save File Link */}
      {job && job.status === "completed" && (
        <div className="download-completed-box">
          <a
            href={getDownloadFileUrl(job.id)}
            download
            className="button button-primary button-sm download-save-link w-full"
            title="Lưu video về máy tính"
          >
            <CheckCircle2 size={14} className="mr-1 text-green-300" />
            <span>
              Lưu file về máy (
              {formatBytes(job.total_bytes || job.file_size || job.bytes_downloaded)})
            </span>
          </a>
          <button
            type="button"
            onClick={() => handleStartDownload(true)}
            disabled={busy}
            className="download-re-trigger-btn"
            title="Tải lại từ đầu"
          >
            Tải lại
          </button>
        </div>
      )}

      {/* State 5: Failed, Interrupted, Cancelled */}
      {job &&
        (job.status === "failed" ||
          job.status === "interrupted" ||
          job.status === "cancelled") && (
          <div className="download-failed-box">
            <div className="download-error-row">
              <AlertCircle size={13} className="text-danger shrink-0" />
              <span className="download-error-text" title={job.error_message || ""}>
                {job.error_message ||
                  (job.status === "cancelled"
                    ? "Đã hủy tải video."
                    : "Tải video thất bại.")}
              </span>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleStartDownload(true)}
              disabled={busy}
              className="download-retry-btn"
              title="Thử lại tải video"
            >
              <RotateCcw size={12} className="mr-1" />
              <span>Thử lại</span>
            </Button>
          </div>
        )}
    </div>
  );
}
