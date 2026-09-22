"use client";
import Link from "next/link";
import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowUpRight,
  Search,
  Sparkles,
  FlaskConical,
  X,
  Check,
  Clock3,
  ArrowRight,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { api, platformName, statusName } from "@/lib/api";
import { useResource } from "@/lib/use-resource";
import { useSSEListener } from "@/lib/use-event-source";
import type { Job, Page, Platform, Settings } from "@/types";
import { VideoGrid } from "@/features/video/video-grid";

import {
  QueryExpansionPanel,
  selectedTerms,
  type QueryChoice,
} from "./query-expansion";

import { BrowserControls } from "./browser-controls";

const terminal = ["completed", "failed", "cancelled", "interrupted"];

export type SearchMode = "mock" | "xiaohongshu" | "douyin" | "tiktok" | "youtube_shorts";

export function parseSearchMode(raw: string | null | undefined): SearchMode {
  if (!raw) return "xiaohongshu";
  const normalized = raw.trim().toLowerCase();
  if (normalized === "xiaohongshu") return "xiaohongshu";
  if (normalized === "douyin") return "douyin";
  if (normalized === "tiktok") return "tiktok";
  if (normalized === "youtube_shorts" || normalized === "youtube") return "youtube_shorts";
  return "mock";
}

export function SearchWorkspace() {
  const params = useSearchParams();
  const initialMode = parseSearchMode(params.get("mode"));
  return (
    <SearchSession
      key={params.toString()}
      initialQuery={params.get("q") ?? ""}
      initialJob={params.get("job")}
      initialMode={initialMode}
    />
  );
}
export function normalizeLimitForMode(currentLimit: number, targetMode: string): number {
  const allowed =
    targetMode === "mock"
      ? [20, 50, 100, 200]
      : [5, 10, 20, 50, 100];
  if (allowed.includes(currentLimit)) {
    return currentLimit;
  }
  return 20;
}

function SearchSession({
  initialQuery,
  initialJob,
  initialMode,
}: {
  initialQuery: string;
  initialJob: string | null;
  initialMode: SearchMode;
}) {
  const router = useRouter();
  const [mode, setMode] = useState<SearchMode>(initialMode);
  const [useAI, setUseAI] = useState(false);
  const [choice, setChoice] = useState<QueryChoice | null>(null);
  const settings = useResource<Settings>("/settings");
  const [query, setQuery] = useState(initialQuery);
  const [platforms, setPlatforms] = useState<Platform[]>(
    initialMode === "mock"
      ? ["douyin", "xiaohongshu"]
      : [initialMode as Platform],
  );
  const [limit, setLimit] = useState(() => normalizeLimitForMode(20, initialMode));
  const [sortBy, setSortBy] = useState<"relevance" | "most_liked" | "newest">("relevance");
  const [publishTime, setPublishTime] = useState<"all" | "day" | "week" | "six_months">("all");
  const [contentType, setContentType] = useState<"all" | "video" | "image_text">("all");
  const [exactHashtag, setExactHashtag] = useState(false);
  const [jobId, setJobId] = useState<string | null>(initialJob);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const job = useResource<Job>(jobId ? `/search/jobs/${jobId}` : null, 1000);
  const stats = useResource<{
    total: number;
    saved: number;
    used: number;
    jobs: number;
  }>("/stats", 5000);
  const recent = useResource<Page<Job>>("/search/jobs", 5000);

  useSSEListener("search:update", (data: { job_id?: string; status?: string }) => {
    if (jobId && data?.job_id === jobId) {
      job.refresh();
      if (data.status && terminal.includes(data.status)) {
        stats.refresh();
        recent.refresh();
      }
    }
  });

  const isRunning = Boolean(
    jobId && job.data && ["searching", "ranking", "expanding_query", "resuming"].includes(job.data.status),
  );
  const active = Boolean(
    jobId && (!job.data || !terminal.includes(job.data.status)),
  );
  async function start(event?: React.FormEvent) {
    event?.preventDefault();
    if (!query.trim() || !platforms.length) return;
    const terms =
      choice?.topic === query.trim().replace(/\s+/g, " ")
        ? selectedTerms(choice.text)
        : [];
    const isReal = mode !== "mock";
    if (
      terms.length > (isReal ? 3 : 10) ||
      terms.some((v) => v.length > 300)
    ) {
      setError(
        isReal
          ? "Tìm thật dùng tối đa 3 truy vấn, mỗi truy vấn không quá 300 ký tự."
          : "Tối đa 10 truy vấn, mỗi truy vấn không quá 300 ký tự.",
      );
      return;
    }
    setBusy(true);
    setError("");

    // Automatically cancel any active or waiting job before launching the new search
    if (jobId && job.data && !terminal.includes(job.data.status)) {
      try {
        await api(`/search/jobs/${jobId}/cancel`, { method: "POST" });
      } catch {
        // Non-blocking
      }
    }

    try {
      const result = await api<Job>("/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          mode,
          platforms:
            mode === "mock"
              ? platforms
              : [mode as Platform],
          limit: normalizeLimitForMode(limit, mode),
          use_ai: useAI,
          selected_queries: terms.length ? terms : undefined,
          sort_by: sortBy,
          publish_time: publishTime,
          content_type: contentType,
          exact_hashtag: exactHashtag || query.trim().startsWith("#"),
          target_hashtag: query.trim().startsWith("#")
            ? query.trim().replace(/^#+/, "").split(/\s+/)[0]
            : undefined,
        }),
      });
      setJobId(result.id);
      window.history.replaceState(
        null,
        "",
        `/?job=${result.id}&q=${encodeURIComponent(query)}&mode=${mode}`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    try {
      await api(`/search/jobs/${jobId}/cancel`, { method: "POST" });
      job.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function selectPlatform(p: Platform) {
    if (mode === "mock") {
      setPlatforms((current) =>
        current.includes(p)
          ? current.length > 1
            ? current.filter((item) => item !== p)
            : current
          : [...current, p],
      );
    } else {
      setMode(p);
      setPlatforms([p]);
      setLimit((prev) => normalizeLimitForMode(prev, p));
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Ý tưởng tiếp theo, bắt đầu từ đây.</h1>
          <p>Tìm, chọn lọc và giữ lại những video bạn quan tâm trên Douyin & Xiaohongshu.</p>
        </div>
        <span className="mode-pill">
          <FlaskConical size={15} />
          {mode === "mock"
            ? "Dữ liệu mẫu"
            : mode === "douyin"
              ? "Douyin thật"
              : mode === "xiaohongshu"
                ? "Xiaohongshu thật"
                : mode === "tiktok"
                  ? "TikTok Global"
                  : "YouTube Shorts"}
        </span>
      </div>
      <div className="stats-strip">
        {[
          ["Video trong workspace", stats.data?.total],
          ["Video đã lưu", stats.data?.saved],
          ["Video đã dùng", stats.data?.used],
          ["Lượt tìm kiếm", stats.data?.jobs],
        ].map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>
              {value ?? "—"}
              <ArrowUpRight size={17} />
            </strong>
          </div>
        ))}
      </div>
      <section className="search-panel">
        <div className="search-panel-title">
          <span className="spark-icon">
            <Sparkles size={20} />
          </span>
          <div>
            <h2>Bạn muốn tìm gì hôm nay?</h2>
            <p>Mô tả chủ đề bằng tiếng Việt hoặc nhập từ khóa.</p>
          </div>
        </div>
        <form onSubmit={start}>
          <label className="row search-mode">
            Nguồn tìm kiếm{" "}
            <select
              aria-label="Nguồn tìm kiếm"
              value={mode}
              disabled={busy}
              onChange={(e) => {
                const newMode = parseSearchMode(e.target.value);
                setMode(newMode);
                setLimit((prev) => normalizeLimitForMode(prev, newMode));
                if (newMode === "mock") {
                  setPlatforms(["douyin", "xiaohongshu"]);
                } else {
                  setPlatforms([newMode as Platform]);
                }
              }}
            >
              <option value="xiaohongshu">Xiaohongshu thật (Nền tảng trực tiếp)</option>
              <option value="douyin">Douyin thật (Nền tảng trực tiếp)</option>
              <option value="mock">Dữ liệu mẫu (Thử nghiệm offline & Mock Multi-Platform)</option>
            </select>
          </label>
          <div className="search-input-row">
            <Search size={21} />
            <input
              aria-label="Chủ đề tìm kiếm"
              placeholder="Ví dụ: unbox đồ cute, #decorbanhoc, #ootd, dụng cụ bóc sticker…"
              value={query}
              maxLength={300}
              onChange={(e) => setQuery(e.target.value)}
              required
            />
            <Button
              disabled={busy || !query.trim() || !platforms.length}
              type="submit"
            >
              <Search size={17} />
              {busy
                ? "Đang gửi yêu cầu…"
                : isRunning
                  ? "Đang tìm kiếm…"
                  : mode === "mock"
                    ? "Tìm video mẫu"
                    : `Tìm trên ${platformName(mode)} thật`}
              <ArrowRight size={17} />
            </Button>
          </div>
          {query.trim().startsWith("#") && (
            <div style={{ margin: "-0.5rem 0 0.5rem 0.25rem", fontSize: "0.82rem", color: "#0f766e", display: "flex", alignItems: "center", gap: "0.35rem" }}>
              <span>🏷️ Đang tìm theo Hashtag chính xác:</span>
              <strong style={{ background: "#ccfbf1", padding: "0.1rem 0.4rem", borderRadius: "4px" }}>
                {query.trim()}
              </strong>
            </div>
          )}
          <div className="search-options">
            <div className="row wrap">
              <span className="muted" style={{ fontWeight: 500 }}>Tìm trên:</span>
              {(mode === "mock"
                ? (["xiaohongshu", "douyin", "tiktok", "youtube_shorts"] as Platform[])
                : (["xiaohongshu", "douyin"] as Platform[])
              ).map((p) => {
                const isSelected =
                  mode === "mock"
                    ? platforms.includes(p)
                    : mode === p;
                return (
                  <button
                    key={p}
                    type="button"
                    className={`platform-option ${isSelected ? "checked" : ""}`}
                    style={{
                      cursor: "pointer",
                      border: isSelected ? "2px solid #0f766e" : "1px solid #cbd5e1",
                      background: isSelected ? "rgba(15, 118, 110, 0.08)" : "#fff",
                      color: isSelected ? "#0f766e" : "inherit",
                      fontWeight: isSelected ? 600 : 400,
                      padding: "0.35rem 0.75rem",
                      borderRadius: "6px",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.45rem",
                    }}
                    onClick={() => selectPlatform(p)}
                  >
                    <input
                      type="checkbox"
                      checked={isSelected}
                      readOnly
                      style={{ cursor: "pointer", pointerEvents: "none" }}
                    />
                    <span className={p === "douyin" ? "dy-mark" : p === "xiaohongshu" ? "red-mark" : "platform-mark"}>
                      {p === "douyin" ? "♪" : p === "xiaohongshu" ? "小红书" : p === "tiktok" ? "TT" : "YT"}
                    </span>
                    <span>{platformName(p)} {mode !== "mock" && "thật"}</span>
                  </button>
                );
              })}
            </div>
            <div className="row wrap" style={{ gap: "10px", alignItems: "center" }}>
              <label className="row muted">
                Số kết quả
                <select
                  aria-label="Số kết quả"
                  value={limit}
                  onChange={(e) => setLimit(Number(e.target.value))}
                >
                  {(mode !== "mock"
                    ? [5, 10, 20, 50, 100]
                    : [20, 50, 100, 200]
                  ).map((n) => (
                    <option key={n} value={n}>
                      {n} kết quả
                    </option>
                  ))}
                </select>
              </label>
              {mode === "douyin" && (
                <>
                  <label className="row muted">
                    Sắp xếp
                    <select
                      aria-label="Thứ tự sắp xếp"
                      value={sortBy}
                      disabled={busy}
                      onChange={(e) => setSortBy(e.target.value as "relevance" | "most_liked" | "newest")}
                    >
                      <option value="relevance">Tổng hợp (Chuẩn)</option>
                      <option value="most_liked">Nhiều like nhất</option>
                      <option value="newest">Mới nhất</option>
                    </select>
                  </label>
                  <label className="row muted">
                    Thời gian
                    <select
                      aria-label="Thời gian đăng"
                      value={publishTime}
                      disabled={busy}
                      onChange={(e) => setPublishTime(e.target.value as "all" | "day" | "week" | "six_months")}
                    >
                      <option value="all">Mọi lúc</option>
                      <option value="day">24h qua</option>
                      <option value="week">1 tuần qua</option>
                      <option value="six_months">6 tháng qua</option>
                    </select>
                  </label>
                  <label className="row muted">
                    Định dạng
                    <select
                      aria-label="Loại nội dung"
                      value={contentType}
                      disabled={busy}
                      onChange={(e) => setContentType(e.target.value as "all" | "video" | "image_text")}
                    >
                      <option value="all">Tất cả</option>
                      <option value="video">Chỉ Video</option>
                      <option value="image_text">Chỉ 图文 (Ảnh)</option>
                    </select>
                  </label>
                </>
              )}
            </div>
          </div>
          <div className="row wrap" style={{ alignItems: "center", gap: "1rem" }}>
            <label className="row ai-toggle">
              <input
                type="checkbox"
                checked={useAI}
                onChange={(e) => setUseAI(e.target.checked)}
                disabled={busy}
              />
              Tự tạo từ khóa AI khi tìm
            </label>
            <label className="row ai-toggle" title="Chỉ giữ lại video có gắn đúng thẻ hashtag được yêu cầu">
              <input
                type="checkbox"
                checked={exactHashtag || query.trim().startsWith("#")}
                onChange={(e) => setExactHashtag(e.target.checked)}
                disabled={busy || query.trim().startsWith("#")}
              />
              Khớp chính xác Hashtag (#)
            </label>
            {mode !== "mock" && (
              <span style={{ fontSize: "0.82rem", color: "#0f766e", background: "#f0fdfa", padding: "0.2rem 0.6rem", borderRadius: "999px", border: "1px solid #ccfbf1" }}>
                💡 Tự động chuyển đổi tiếng Việt sang tiếng Trung chuẩn xác để tìm đúng video
              </span>
            )}
          </div>
          <QueryExpansionPanel
            query={query}
            enabled={Boolean(settings.data?.ai_available)}
            disabled={busy}
            choice={choice}
            onChange={setChoice}
          />
        </form>
        <div className="suggestions">
          <span>Thử một chủ đề</span>
          {[
            "unbox đồ cute",
            "dụng cụ bóc sticker",
            "decor bàn học",
            "BJD makeup",
          ].map((q) => (
            <button key={q} type="button" onClick={() => setQuery(q)}>
              {q}
              <ArrowUpRight size={12} />
            </button>
          ))}
        </div>
      </section>
      {mode === "xiaohongshu" && !active && (!job.data || terminal.includes(job.data.status)) && (
        <section className="job-progress">
          <p>
            Tìm thật trên Xiaohongshu: tối đa 3 truy vấn và 100 video. Các số
            liệu chưa đọc được sẽ để trống.
          </p>
          <BrowserControls platform="xiaohongshu" />
        </section>
      )}
      {mode === "douyin" && !active && (!job.data || terminal.includes(job.data.status)) && (
        <section className="job-progress">
          <p>
            Tìm thật trên Douyin: tối đa 3 truy vấn và 100 video. Các số liệu
            chưa đọc được sẽ để trống.
          </p>
          <BrowserControls platform="douyin" />
        </section>
      )}
      <div className="mock-notice">
        <FlaskConical size={17} />
        <span>
          <strong>
            {mode === "mock"
              ? "Đang thử quy trình với dữ liệu mẫu."
              : mode === "douyin"
                ? "Đang dùng trình duyệt Douyin riêng."
                : "Đang dùng trình duyệt Xiaohongshu riêng."}
          </strong>{" "}
          {mode === "mock"
            ? "Dữ liệu mẫu để thử nghiệm quy trình tìm kiếm offline."
            : "Dữ liệu trích xuất trực tiếp từ nền tảng thật."}
        </span>
      </div>
      <ErrorBanner message={error || job.error} />
      {(busy || active || job.data) && (
        <section className="job-progress" aria-live="polite">
          <div className="progress-top">
            <div className="row">
              {job.data?.status === "completed" ? (
                <Check size={18} />
              ) : (
                <Clock3 size={18} />
              )}
              <strong>
                {job.data
                  ? statusName[job.data.status] ?? job.data.status
                  : "Đang khởi tạo lượt tìm kiếm…"}
              </strong>
              <span className="muted">{job.data?.original_query ?? query}</span>
            </div>
            {active && (
              <Button size="sm" variant="outline" onClick={cancel}>
                <X size={14} />
                Hủy tìm kiếm
              </Button>
            )}
          </div>
          <div className="progress-track">
            <div
              style={{
                transform: `scaleX(${(!job.data ? 15 : terminal.includes(job.data.status) ? 100 : Math.min(95, 8 + ((job.data.processed_count ?? 0) / (job.data.requested_limit || 1)) * 87)) / 100})`,
              }}
            />
          </div>
          {job.data && (
            <div className="progress-meta">
              <span>
                Đã lưu: <strong>{job.data.processed_count ?? 0}</strong> / {job.data.requested_limit ?? 0} video
              </span>
              <span>
                Đã quét: {job.data.found_count ?? 0} thẻ
              </span>
              <span>
                Bị loại trừ: <strong style={{ color: "#b91c1c" }}>{job.data.rejected_count ?? 0}</strong> video
              </span>
              <span>
                Douyin: {job.data.provider_counts?.douyin ?? 0} · RED:{" "}
                {job.data.provider_counts?.xiaohongshu ?? 0}
              </span>
              <span>{job.data.duplicate_count ?? 0} video trùng</span>
            </div>
          )}
          {job.data && job.data.status === "completed" && job.data.processed_count < job.data.requested_limit && (
            <p className="muted" style={{ fontSize: "0.83rem", marginTop: "0.4rem" }}>
              ℹ️ Đã quét hết kết quả phù hợp trên trang ({job.data.processed_count} video). Số lượng {job.data.requested_limit} là trần mục tiêu tối đa.
            </p>
          )}
          {job.data && (job.data.ai_source || job.data.actual_ai_model) && (
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.4rem", flexWrap: "wrap" }}>
              <span className="muted" style={{ fontSize: "0.82rem" }}>Nguồn dịch:</span>
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.3rem",
                  fontSize: "0.78rem",
                  padding: "0.15rem 0.5rem",
                  borderRadius: "999px",
                  background: job.data.actual_ai_model || job.data.ai_source === "gemini" ? "#eff6ff" : "#f0fdf4",
                  color: job.data.actual_ai_model || job.data.ai_source === "gemini" ? "#1d4ed8" : "#15803d",
                  border: `1px solid ${job.data.actual_ai_model || job.data.ai_source === "gemini" ? "#bfdbfe" : "#bbf7d0"}`,
                  fontWeight: 600,
                }}
              >
                {job.data.actual_ai_model
                  ? `Gemini AI: ${job.data.actual_ai_model}${job.data.ai_fallback_occurred ? " (fallback)" : ""}`
                  : job.data.ai_source === "dictionary"
                  ? "Từ điển chuyên ngành"
                  : job.data.ai_source === "original"
                  ? "Query gốc"
                  : "Từ điển / Fallback"}
                {typeof job.data.ai_latency_ms === "number" && (
                  <span style={{ opacity: 0.75, fontWeight: 400 }}>({Math.round(job.data.ai_latency_ms)}ms)</span>
                )}
              </span>
            </div>
          )}
          {job.data && job.data.queries?.length > 0 && (
            <p className="job-queries" style={{ marginTop: "0.4rem" }}>
              Truy vấn thực tế gửi nền tảng:{" "}
              {job.data.queries.map((q, i) => (
                <span key={i} style={{ display: "inline-block", background: "#f1f5f9", padding: "0.1rem 0.45rem", borderRadius: "4px", margin: "0 0.2rem", fontWeight: 500, fontSize: "0.82rem" }}>
                  {q}
                </span>
              ))}
            </p>
          )}
          {job.data?.diagnostics?.rejected_candidates && job.data.diagnostics.rejected_candidates.length > 0 && (
            <details style={{ marginTop: "0.6rem", border: "1px solid #fed7aa", borderRadius: "8px", background: "#fff7ed", padding: "0.5rem 0.75rem" }}>
              <summary style={{ cursor: "pointer", fontWeight: 600, color: "#c2410c", fontSize: "0.85rem" }}>
                🛡️ Video bị loại trừ bởi Precision Gate ({job.data.diagnostics.rejected_candidates.length})
              </summary>
              <div style={{ marginTop: "0.5rem", maxHeight: "200px", overflowY: "auto", fontSize: "0.8rem" }}>
                {job.data.diagnostics.rejected_candidates.map((cand, idx) => (
                  <div key={idx} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "0.3rem 0", borderBottom: "1px dashed #fdba74", gap: "0.5rem" }}>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={cand.title}>
                      [{cand.platform === "douyin" ? "Douyin" : "RED"}] {cand.title}
                    </span>
                    <span style={{ color: "#9a3412", fontWeight: 500, fontSize: "0.78rem", whiteSpace: "nowrap" }}>
                      {cand.reason}
                    </span>
                  </div>
                ))}
              </div>
            </details>
          )}
          <ErrorBanner message={job.data?.ai_warning ?? ""} />
          <ErrorBanner message={job.data?.error_message ?? ""} />
          {job.data &&
            ["waiting_for_login", "waiting_for_user"].includes(
              job.data.status,
            ) && (() => {
              const jobPlatform =
                (job.data?.platforms && job.data.platforms[0]) ||
                (mode === "douyin" ? "douyin" : "xiaohongshu");
              const jobPlatformLabel =
                jobPlatform === "douyin" ? "Douyin" : "Xiaohongshu";
              const defaultLoginUrl =
                jobPlatform === "douyin"
                  ? "https://www.douyin.com"
                  : "https://www.xiaohongshu.com/explore";
              const isLogin = job.data.status === "waiting_for_login";

              return (
                <div
                  style={{
                    margin: "1rem 0",
                    padding: "1.25rem",
                    background: "#f0fdfa",
                    border: "2px solid #0f766e",
                    borderRadius: "10px",
                    boxShadow: "0 4px 16px rgba(15, 118, 110, 0.1)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      flexWrap: "wrap",
                      gap: "0.5rem",
                      marginBottom: "0.6rem",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "0.5rem",
                        color: "#0f766e",
                        fontWeight: 700,
                        fontSize: "1.05rem",
                      }}
                    >
                      <span>
                        {isLogin
                          ? `🔐 Nền tảng ${jobPlatformLabel} yêu cầu đăng nhập tài khoản`
                          : `⚠️ Nền tảng ${jobPlatformLabel} yêu cầu xác minh bảo mật`}
                      </span>
                    </div>

                    <a
                      href={defaultLoginUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "0.4rem",
                        background: "#0f766e",
                        color: "#ffffff",
                        padding: "0.45rem 0.9rem",
                        borderRadius: "6px",
                        fontSize: "0.85rem",
                        fontWeight: 600,
                        textDecoration: "none",
                        boxShadow: "0 2px 6px rgba(15, 118, 110, 0.25)",
                      }}
                    >
                      <ExternalLink size={15} />
                      Đăng nhập trên {jobPlatformLabel}
                    </a>
                  </div>
                  <p
                    style={{
                      color: "#134e4a",
                      fontSize: "0.88rem",
                      lineHeight: 1.5,
                      marginBottom: "0.75rem",
                    }}
                  >
                    Nền tảng <strong>{jobPlatformLabel}</strong> yêu cầu đăng nhập hoặc xác minh tài khoản. Bạn hãy nhấp vào nút đăng nhập bên cạnh để mở trang web trên trình duyệt của bạn (hoặc cấu hình Cookie trong Cài đặt), sau đó bấm <strong>Tiếp tục lượt tìm</strong>.
                  </p>
                  <BrowserControls
                    jobId={job.data.id}
                    waiting
                    refresh={job.refresh}
                    platform={
                      job.data.platforms?.[0] === "douyin" ? "douyin" : "xiaohongshu"
                    }
                  />
                </div>
              );
            })()}
        </section>
      )}
      {jobId ? (
        <VideoGrid key={jobId} jobId={jobId} />
      ) : (
        <>
          <div className="section-heading">
            <h2>Tìm kiếm gần đây</h2>
            <Link href="/history" className="text-link">
              Xem lịch sử
              <ArrowRight size={15} />
            </Link>
          </div>
          {recent.data?.items.length ? (
            <div className="recent-grid">
              {recent.data.items.slice(0, 3).map((item) => (
                <button
                  className="recent-card"
                  key={item.id}
                  onClick={() => {
                    if (item.import_source) {
                      router.push(`/imports/${item.id}`);
                      return;
                    }
                    const itemPlatform =
                      (item.platforms && item.platforms[0]) || "xiaohongshu";
                    setMode(item.is_mock ? "mock" : itemPlatform);
                    setJobId(item.id);
                    setQuery(item.original_query);
                  }}
                >
                  <span>
                    <Clock3 size={17} />
                    {new Date(item.created_at).toLocaleDateString("vi-VN")}
                  </span>
                  <strong>{item.original_query}</strong>
                  <span>
                    {item.processed_count} video · {statusName[item.status]}
                    <ArrowRight size={16} />
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="first-search">
              <div className="first-icon">
                <Search size={27} />
              </div>
              <h3>Khám phá đầu tiên của bạn</h3>
              <p>
                Nhập một chủ đề ở trên để thử tìm kiếm.
                <br />
                Sau đó lưu những video bạn muốn xem lại vào Thư viện.
              </p>
            </div>
          )}
        </>
      )}
    </>
  );
}
