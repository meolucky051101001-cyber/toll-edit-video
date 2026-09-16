export type Platform = "douyin" | "xiaohongshu";
export type VideoStatus = "new" | "saved" | "skipped" | "used" | "favorite";
export interface Video {
  imported_from?: string | null;
  id: string;
  platform: Platform;
  url: string;
  share_url?: string | null;
  title: string | null;
  caption: string | null;
  thumbnail_url: string | null;
  author_name: string | null;
  hashtags: string[];
  keywords: string[];
  like_count: number | null;
  comment_count: number | null;
  favorite_count: number | null;
  share_count: number | null;
  duration_seconds: number | null;
  published_at: string | null;
  relevance_score: number;
  quality_score: number;
  final_score: number;
  status: VideoStatus;
  is_mock: boolean;
  script_analysis?: ScriptAnalysis | null;
  search_query: string;
  search_job_id: string;
}
export interface QueryExpansion {
  original_query: string;
  translated_query: string;
  primary_keywords: string[];
  related_keywords: string[];
  hashtags: string[];
  negative_keywords: string[];
  topics: string[];
}
export interface QueryPlan {
  subject: string;
  action?: string | null;
  mandatory_attributes: string[];
  modifiers: string[];
  tiered_queries: string[];
}
export interface ExpansionOutcome {
  original_query: string;
  source: "gemini" | "fallback" | "original" | "manual" | "dictionary";
  expansion: QueryExpansion | null;
  plan?: QueryPlan | null;
  queries: string[];
  warning: string | null;
  error_code: string | null;
  cached: boolean;
}
export interface Job {
  import_source?: string | null;
  queries: string[];
  ai_source: string | null;
  ai_warning: string | null;
  query_expansion: QueryExpansion | null;
  id: string;
  original_query: string;
  platforms: Platform[];
  status: string;
  requested_limit: number;
  found_count: number;
  processed_count: number;
  duplicate_count: number;
  provider_counts: Record<string, number>;
  created_at: string;
  error_message: string | null;
  is_mock: boolean;
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size?: number;
}
export interface Health {
  status: string;
  mode: string;
  database: string;
}
export interface Settings {
  use_mock_provider: boolean;
  default_result_limit: number;
  max_queries: number;
  search_timeout: number;
  database_location: string;
  ai_available: boolean;
  ai_provider: string;
  ai_model: string;
  ai_key_configured: boolean;
  ai_free_tier_confirmed: boolean;
  ai_min_interval: number;
  browser_available: boolean;
}

export type DownloadJobStatus =
  | "queued"
  | "resolving"
  | "downloading"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface DownloadJobOut {
  id: string;
  video_id: string;
  platform: Platform;
  status: DownloadJobStatus;
  bytes_downloaded: number;
  total_bytes: number | null;
  progress_percent: number | null;
  file_size: number | null;
  error_code: string | null;
  error_message: string | null;
  adapter_version: string | null;
  download_url?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CookiePlatformStatus {
  has_cookie: boolean;
  token_count: number;
  preview: string;
  detected_essential: string[];
  missing_essential: string[];
  grade: "missing" | "basic" | "high_quality";
  status_text: string;
  is_custom: boolean;
  has_browser_session: boolean;
}

export interface CookieSettingsData {
  xiaohongshu: CookiePlatformStatus;
  douyin: CookiePlatformStatus;
}

export interface RemakeScriptVI {
  intro: string;
  body: string[];
  cta: string;
}

export interface ScriptAnalysis {
  video_id: string;
  hook_3s: string;
  core_points: string[];
  retention_tactics: string[];
  remake_script_vi: RemakeScriptVI;
  source: string;
  cached: boolean;
}

