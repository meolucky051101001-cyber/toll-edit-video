export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...options?.headers },
      signal: options?.signal ?? AbortSignal.timeout(10000),
    });
  } catch {
    throw new Error(
      "Không kết nối được backend. Hãy chạy start.bat và thử lại.",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : "Backend chưa sẵn sàng. Kiểm tra logs/backend.log và thử lại.",
    );
  }
  return response.json() as Promise<T>;
}
export const platformName = (value: string) =>
  value === "douyin" ? "Douyin" : "Xiaohongshu";
export const statusName: Record<string, string> = {
  new: "Mới",
  saved: "Đã lưu",
  skipped: "Bỏ qua",
  used: "Đã dùng",
  favorite: "Yêu thích",
  pending: "Đang chờ",
  expanding_query: "Đang tạo từ khóa AI",
  waiting_for_login: "Chờ đăng nhập",
  waiting_for_user: "Cần xác minh / giải CAPTCHA",
  searching: "Đang tìm kiếm",
  ranking: "Đang xếp hạng",
  completed: "Hoàn tất",
  cancelled: "Đã hủy",
  interrupted: "Bị gián đoạn",
  failed: "Có lỗi",
};
export const count = (value: number | null) =>
  value === null
    ? "—"
    : new Intl.NumberFormat("vi-VN", { notation: "compact" }).format(value);

export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || isNaN(seconds) || !isFinite(seconds) || seconds < 0) return null;
  const totalSec = Math.round(seconds);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

export function isTrustedPlatformUrl(url: string, platform: string): boolean {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") {
      return false;
    }
    const host = parsed.hostname.toLowerCase();
    if (platform === "xiaohongshu") {
      return (
        host === "xiaohongshu.com" ||
        host.endsWith(".xiaohongshu.com") ||
        host === "xhslink.com" ||
        host.endsWith(".xhslink.com") ||
        host === "rednote.com" ||
        host.endsWith(".rednote.com")
      );
    }
    if (platform === "douyin") {
      return (
        host === "douyin.com" ||
        host.endsWith(".douyin.com") ||
        host === "iesdouyin.com" ||
        host.endsWith(".iesdouyin.com")
      );
    }
    return false;
  } catch {
    return false;
  }
}

export function extractXhsToken(urlStr: string | null | undefined): string | null {
  if (!urlStr) return null;
  try {
    const parsed = new URL(urlStr);
    const tokens = parsed.searchParams.getAll("xsec_token");
    for (let i = tokens.length - 1; i >= 0; i--) {
      const trimmed = tokens[i].trim();
      if (trimmed.length > 0) {
        return trimmed;
      }
    }
    return null;
  } catch {
    return null;
  }
}

export function hasValidXhsToken(urlStr: string | null | undefined): boolean {
  return extractXhsToken(urlStr) !== null;
}

export async function requestVideoDownload(
  videoId: string,
  force: boolean = false
): Promise<import("@/types").DownloadJobOut> {
  const query = force ? "?force=true" : "";
  return api<import("@/types").DownloadJobOut>(
    `/videos/${encodeURIComponent(videoId)}/downloads${query}`,
    {
      method: "POST",
    }
  );
}

export async function getVideoActiveDownload(
  videoId: string
): Promise<import("@/types").DownloadJobOut | null> {
  return api<import("@/types").DownloadJobOut | null>(
    `/videos/${encodeURIComponent(videoId)}/downloads/active`
  );
}

export async function getDownloadStatus(
  downloadId: string
): Promise<import("@/types").DownloadJobOut> {
  return api<import("@/types").DownloadJobOut>(`/downloads/${encodeURIComponent(downloadId)}`);
}

export async function cancelDownload(
  downloadId: string
): Promise<import("@/types").DownloadJobOut> {
  return api<import("@/types").DownloadJobOut>(
    `/downloads/${encodeURIComponent(downloadId)}/cancel`,
    {
      method: "POST",
    }
  );
}

export function getDownloadFileUrl(downloadId: string): string {
  return `/api/downloads/${encodeURIComponent(downloadId)}/file`;
}

export async function batchDownloadVideos(
  videoIds: string[],
  force: boolean = false
): Promise<import("@/types").DownloadJobOut[]> {
  return api<import("@/types").DownloadJobOut[]>("/downloads/batch", {
    method: "POST",
    body: JSON.stringify({ video_ids: videoIds, force }),
  });
}

export async function getCookieSettings(): Promise<import("@/types").CookieSettingsData> {
  return api<import("@/types").CookieSettingsData>("/settings/cookies");
}

export async function saveCookieSettings(data: {
  xhs_cookie?: string;
  douyin_cookie?: string;
}): Promise<import("@/types").CookieSettingsData> {
  return api<import("@/types").CookieSettingsData>("/settings/cookies", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function syncBrowserCookies(platform: "xiaohongshu" | "douyin" | "all" = "all"): Promise<{
  ok: boolean;
  synced_platforms: string[];
  message: string;
  cookies: import("@/types").CookieSettingsData;
}> {
  return api("/settings/cookies/sync-browser", {
    method: "POST",
    body: JSON.stringify({ platform }),
  });
}

export async function analyzeVideoScript(
  videoId: string,
  forceRefresh: boolean = false
): Promise<import("@/types").ScriptAnalysis> {
  return api<import("@/types").ScriptAnalysis>(`/videos/${encodeURIComponent(videoId)}/script-analysis`, {
    method: "POST",
    body: JSON.stringify({ force_refresh: forceRefresh }),
  });
}

