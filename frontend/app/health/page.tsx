"use client";
import { useResource } from "@/lib/use-resource";
import { ErrorBanner } from "@/components/error-banner";
import type { Health } from "@/types";
export default function Page() {
  const health = useResource<Health>("/health", 3000);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Trạng thái ứng dụng</h1>
          <p>Kiểm tra kết nối local.</p>
        </div>
      </div>
      <ErrorBanner message={health.error} />
      <section className="settings-panel">
        <h2>{health.data ? "Backend online" : "Đang kết nối…"}</h2>
        <p>Database: {health.data?.database ?? "—"}</p>
        <p>Chế độ: {health.data?.mode ?? "—"}</p>
        <a
          className="text-link"
          href="http://127.0.0.1:8000/docs"
          target="_blank"
          rel="noreferrer"
        >
          Mở tài liệu API
        </a>
      </section>
    </>
  );
}
