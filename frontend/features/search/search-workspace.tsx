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
import type { Job, Page, Platform, Settings } from "@/types";
import { VideoGrid } from "@/features/video/video-grid";

import {
  QueryExpansionPanel,
  selectedTerms,
  type QueryChoice,
} from "./query-expansion";

import { BrowserControls } from "./browser-controls";

const terminal = ["completed", "failed", "cancelled", "interrupted"];

export type SearchMode = "mock" | "xiaohongshu" | "douyin";

export function parseSearchMode(raw: string | null | undefined): SearchMode {
  if (!raw) return "mock";
  const normalized = raw.trim().toLowerCase();
  if (normalized === "xiaohongshu") return "xiaohongshu";
  if (normalized === "douyin") return "douyin";
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
    targetMode === "xiaohongshu" || targetMode === "douyin"
      ? [5, 10, 20, 50, 100]
      : [20, 50, 100, 200];
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
    initialMode === "douyin"
      ? ["douyin"]
      : initialMode === "xiaohongshu"
        ? ["xiaohongshu"]
        : ["douyin", "xiaohongshu"],
  );
  const [limit, setLimit] = useState(() => normalizeLimitForMode(20, initialMode));
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
  const isRunning = Boolean(
    jobId && job.data && ["searching", "ranking", "expanding_query"].includes(job.data.status),
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
    const isReal = mode === "xiaohongshu" || mode === "douyin";
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
            mode === "xiaohongshu"
              ? ["xiaohongshu"]
              : mode === "douyin"
                ? ["douyin"]
                : platforms,
          limit: normalizeLimitForMode(limit, mode),
          use_ai: useAI,
          selected_queries: terms.length ? terms : undefined,
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
              : "Xiaohongshu thật"}
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
                if (newMode === "douyin") {
                  setPlatforms(["douyin"]);
                } else if (newMode === "xiaohongshu") {
                  setPlatforms(["xiaohongshu"]);
                } else {
                  setPlatforms(["douyin", "xiaohongshu"]);
                }
              }}
            >
              <option value="xiaohongshu">Xiaohongshu thật (Nền tảng trực tiếp)</option>
              <option value="douyin">Douyin thật (Nền tảng trực tiếp)</option>
              <option value="mock">Dữ liệu mẫu (Thử nghiệm offline)</option>
            </select>
          </label>
          <div className="search-input-row">
            <Search size={21} />
            <input
              aria-label="Chủ đề tìm kiếm"
              placeholder="Ví dụ: unbox đồ cute, dụng cụ bóc sticker…"
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
                    : mode === "douyin"
                      ? "Tìm trên Douyin thật"
                      : "Tìm trên Xiaohongshu thật"}
              <ArrowRight size={17} />
            </Button>
          </div>
          <div className="search-options">
            <div className="row wrap">
              <span className="muted" style={{ fontWeight: 500 }}>Tìm trên:</span>
              {(["douyin", "xiaohongshu"] as Platform[]).map((p) => {
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
                    <span className={p === "douyin" ? "dy-mark" : "red-mark"}>
                      {p === "douyin" ? "♪" : "小红书"}
                    </span>
                    <span>{platformName(p)} {mode !== "mock" && "thật"}</span>
                  </button>
                );
              })}
            </div>
            <label className="row muted">
              Số kết quả
              <select
                aria-label="Số kết quả"
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
              >
                {(mode === "xiaohongshu" || mode === "douyin"
                  ? [5, 10, 20, 50, 100]
                  : [20, 50, 100, 200]
                ).map((n) => (
                  <option key={n} value={n}>
                    {n} video
                  </option>
                ))}
              </select>
            </label>
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
          {job.data && job.data.queries?.length > 0 && (
            <p className="job-queries">
              Từ khóa đã dùng: {job.data.queries.join(" · ")}
            </p>
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
                      (job.data.platforms && job.data.platforms[0]) ||
                      (mode === "douyin" ? "douyin" : "xiaohongshu")
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
