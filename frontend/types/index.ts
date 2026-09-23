export type Platform = "douyin" | "xiaohongshu" | "tiktok" | "youtube_shorts" | "mock";
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
  velocity_score?: number;
  virality_score?: number;
  media_type?: "video" | "image_text";
  images?: string[];
  status: VideoStatus;
  is_mock: boolean;
  script_analysis?: ScriptAnalysis | null;
  search_query: string;
  search_job_id: string;
}

export type SearchSortBy = "relevance" | "most_liked" | "newest";
export type SearchPublishTime = "all" | "day" | "week" | "six_months";
export type SearchContentType = "all" | "video" | "image_text";

export interface SearchFilters {
  minimum_likes?: number;
  minimum_relevance?: number;
  sort_by?: SearchSortBy;
  publish_time?: SearchPublishTime;
  content_type?: SearchContentType;
  exact_hashtag?: boolean;
  target_hashtag?: string | null;
}

export interface ViralityBreakdown {
  video_id: string;
  virality_score: number;
  hourly_velocity: number;
  acceleration: number;
  age_bucket: string;
  spread_ratio: number;
  snapshot_count: number;
  momentum_bonus: number;
  calculated_at: string;
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
  configured_model?: string | null;
  actual_model?: string | null;
  fallback_occurred?: boolean;
  latency_ms?: number | null;
}
export interface RejectedCandidate {
  video_id: string;
  platform: Platform;
  title: string;
  url: string;
  relevance: number;
  reason: string;
}
export interface JobDiagnostics {
  rejected_candidates?: RejectedCandidate[];
  [key: string]: unknown;
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
  rejected_count?: number;
  min_relevance?: number | null;
  diagnostics?: JobDiagnostics | null;
  actual_ai_model?: string | null;
  ai_fallback_occurred?: boolean;
  ai_latency_ms?: number | null;
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
  | "validating"
  | "completed"
  | "retry_wait"
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
  retry_count?: number;
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

export interface StoryboardScene {
  scene_number: number;
  timestamp_range: string;
  visual_action: string;
  spoken_narration: string;
  on_screen_text?: string;
  sound_music?: string;
}

export interface ScriptAnalysis {
  video_id: string;
  hook_3s: string;
  core_points: string[];
  retention_tactics: string[];
  remake_script_vi: RemakeScriptVI;
  storyboard?: StoryboardScene[];
  analysis_basis?: string;
  analysis_version?: string;
  model_used?: string;
  source: string;
  cached: boolean;
  created_at?: string;
}

export interface VideoMetricSnapshot {
  id: string;
  video_id: string;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  favorite_count: number | null;
  view_count: number | null;
  captured_at: string;
  source: string;
  delta_likes?: number | null;
  delta_comments?: number | null;
  delta_shares?: number | null;
  delta_favorites?: number | null;
  delta_views?: number | null;
  elapsed_seconds?: number | null;
}

export interface MetricHistory {
  video_id: string;
  total_snapshots: number;
  snapshots: VideoMetricSnapshot[];
}

export interface VoiceOption {
  id: string;
  name: string;
  gender: string;
  accent: string;
  provider: string;
  description: string;
}

export interface DubbingRequest {
  video_id: string;
  script_text?: string | null;
  voice_id?: string;
  speed?: number;
  pitch?: number;
  bgm_volume_percent?: number;
  ducking?: boolean;
}

export interface DubbingJobOut {
  id: string;
  video_id: string;
  status: "pending" | "synthesizing" | "mixing" | "completed" | "failed";
  voice_id: string;
  script_text: string;
  audio_url?: string | null;
  video_url?: string | null;
  duration_seconds?: number | null;
  error_message?: string | null;
  created_at: string;
  completed_at?: string | null;
}

export interface DubbingVoicesResponse {
  voices: VoiceOption[];
}

export interface PlatformCapabilities {
  id: string;
  name: string;
  display_name: string;
  supports_headless: boolean;
  supports_video_download: boolean;
  supports_metrics: boolean;
  supports_hashtags: boolean;
  requires_login_for_search: boolean;
  auth_domains: string[];
  icon_name: string;
  is_enabled: boolean;
  description: string;
}

export interface PlatformsResponse {
  platforms: PlatformCapabilities[];
  total: number;
}

export interface QueueStatusResponse {
  queue_name: string;
  backend: string;
  max_size: number;
  current_size: number;
  dead_letter_size: number;
  is_full: boolean;
  active_workers: number;
  max_workers: number;
}

export interface SystemMetricsResponse {
  total_requests: number;
  error_count: number;
  error_rate: number;
  active_jobs: number;
  latencies: Record<
    string,
    {
      count: number;
      p50_ms: number;
      p95_ms: number;
      p99_ms: number;
      avg_ms: number;
      min_ms: number;
      max_ms: number;
    }
  >;
  counters: Record<string, number>;
  uptime_seconds: number;
}

export interface ReleaseReadinessCheckItem {
  status: "PASS" | "WARN" | "FAIL" | "UNINITIALIZED" | "STANDBY";
  detail: string;
}

export interface LiveProbePlatformDetail {
  reachable: boolean;
  state: string;
  http_status?: number | null;
  session_state?: "authenticated" | "verification_required" | "login_required" | "unverified" | "page_available" | "network_error" | "unknown" | string;
}

export interface ReleaseReadinessResponse {
  is_ready: boolean;
  workstation_ready?: boolean;
  retrieval_ready?: boolean;
  status: "READY" | "DEGRADED" | "BLOCKED" | "NOT_READY" | "ready" | "degraded" | "blocked" | "not_ready";
  checks: Record<string, ReleaseReadinessCheckItem>;
  warnings?: string[];
  generated_at?: string;
  live_probe?: {
    douyin?: LiveProbePlatformDetail;
    xiaohongshu?: LiveProbePlatformDetail;
  };
}



