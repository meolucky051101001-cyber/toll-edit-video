"use client";
import { Database, FlaskConical, Sparkles, Monitor, KeyRound } from "lucide-react";
import { useResource } from "@/lib/use-resource";
import { ErrorBanner } from "@/components/error-banner";
import type { Settings } from "@/types";
import { AISettings } from "@/features/search/ai-settings";
import { BrowserControls } from "@/features/search/browser-controls";
import { CookieSettings } from "@/features/settings/cookie-settings";

export default function Page() {
  const settings = useResource<Settings>("/settings");
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Cài đặt</h1>
          <p>Quản lý Cookie, AI, trình duyệt và dữ liệu trên máy này.</p>
        </div>
      </div>
      <ErrorBanner message={settings.error} />
      <div className="settings-grid">
        <section className="settings-panel cookie-panel-full">
          <KeyRound size={23} />
          <h2>Cấu hình Cookie & Tải Video Chất Lượng Cao (1080p / Gốc)</h2>
          <CookieSettings />
        </section>
        <section className="settings-panel">
          <FlaskConical size={23} />
          <h2>Tìm kiếm mẫu</h2>
          <dl className="detail-grid">
            <dt>Chế độ</dt>
            <dd>
              {settings.data
                ? settings.data.use_mock_provider
                  ? "MockProvider"
                  : "Real (chưa hỗ trợ)"
                : "Đang tải…"}
            </dd>
            <dt>Giới hạn mặc định</dt>
            <dd>{settings.data?.default_result_limit ?? "—"}</dd>
            <dt>Budget query / nền tảng</dt>
            <dd>{settings.data?.max_queries ?? "—"}</dd>
            <dt>Timeout</dt>
            <dd>{settings.data?.search_timeout ?? "—"} giây</dd>
          </dl>
          <p className="muted">
            Mỗi lượt dùng tối đa 10 truy vấn trên mỗi nền tảng. Kết quả được
            loại trùng và có thể ít hơn giới hạn đã chọn.
          </p>
        </section>
        <section className="settings-panel">
          <Database size={23} />
          <h2>Cơ sở dữ liệu</h2>
          <p>SQLite · lưu trên máy này</p>
          <code>{settings.data?.database_location ?? "Đang tải…"}</code>
          <p className="muted">
            Dừng ứng dụng trước khi sao lưu thư mục data. Không xóa thư mục này
            khi cập nhật mã nguồn.
          </p>
        </section>
        <section className="settings-panel">
          <Sparkles size={23} />
          <h2>AI query expansion</h2>
          {settings.data ? (
            <AISettings
              key={`${settings.data.ai_provider}:${settings.data.ai_model}:${settings.data.ai_free_tier_confirmed}`}
              settings={settings.data}
              refresh={settings.refresh}
            />
          ) : (
            <p>Đang tải…</p>
          )}
        </section>
        <section className="settings-panel">
          <Monitor size={23} />
          <h2>Trình duyệt & đăng nhập</h2>
          <BrowserControls />
        </section>
      </div>
    </>
  );
}
