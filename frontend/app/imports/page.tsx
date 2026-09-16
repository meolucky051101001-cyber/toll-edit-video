"use client";
import Link from "next/link";
import { useRef, useState } from "react";
import { FileUp } from "lucide-react";
import { api, count, platformName } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { VideoGrid } from "@/features/video/video-grid";

interface Report {
  total: number;
  accepted: number;
  existing: number;
  new: number;
  skipped: number;
  issues: { row: number; reason: string }[];
  sample: {
    title: string | null;
    platform: string;
    like_count: number | null;
  }[];
  job_id?: string;
  imported?: number;
}
interface Upload {
  content: string;
  format: "json" | "jsonl";
}
export default function ImportPage() {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [filename, setFilename] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selection = useRef(0);
  async function choose(file?: File) {
    const id = ++selection.current;
    setUpload(null);
    setReport(null);
    setError("");
    setFilename(file?.name ?? "");
    if (!file) return;
    if (file.size > 1048576 || !/\.(json|jsonl)$/i.test(file.name)) {
      setError("Chọn file .json hoặc .jsonl, tối đa 1 MB và 200 bản ghi.");
      return;
    }
    try {
      const content = await file.text();
      if (id === selection.current)
        setUpload({
          content,
          format: file.name.toLowerCase().endsWith(".jsonl") ? "jsonl" : "json",
        });
    } catch {
      if (id === selection.current)
        setError("Không đọc được file. Hãy chọn lại.");
    }
  }
  async function send(commit = false) {
    if (!upload) return;
    setBusy(true);
    setError("");
    try {
      const response = await api<Report>(
        `/imports/mediacrawler/${commit ? "commit" : "preview"}`,
        {
          method: "POST",
          body: JSON.stringify(upload),
          signal: AbortSignal.timeout(30000),
        },
      );
      setReport(response);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Nhập từ MediaCrawler</h1>
          <p>
            Đưa metadata video đã xuất vào thư viện và kiểm tra trước khi lưu.
          </p>
        </div>
        <FileUp size={28} />
      </div>
      <section className="settings-panel import-panel">
        <h2>Chọn file content</h2>
        <p>
          Hỗ trợ JSON hoặc JSONL của Xiaohongshu và Douyin, tối đa 200 bản
          ghi/lần. Bài ảnh, bình luận và loại nội dung chưa hỗ trợ sẽ được liệt
          kê để bỏ qua.
        </p>
        <label className="import-file">
          File MediaCrawler
          <input
            aria-label="File MediaCrawler"
            type="file"
            accept=".json,.jsonl"
            disabled={busy}
            onChange={(e) => choose(e.target.files?.[0])}
          />
        </label>
        <p className="muted">
          File được xử lý trên backend của máy này. Chức năng nhập không gọi
          Gemini hoặc crawler. Chỉ metadata được lưu; không lưu file gốc,
          cookie, token hay tải video.
        </p>
        <div className="row wrap">
          <Button disabled={!upload || busy} onClick={() => send(false)}>
            {busy ? "Đang xử lý…" : "Xem trước dữ liệu"}
          </Button>
          {report && !report.job_id && (
            <Button
              variant="outline"
              disabled={busy || !report.accepted}
              onClick={() => send(true)}
            >
              Nhập {report.accepted} video vào thư viện
            </Button>
          )}
        </div>
        <ErrorBanner message={error} />
      </section>
      {report && (
        <section className="settings-panel import-report" aria-live="polite">
          <h2>{report.job_id ? "Đã nhập metadata" : "Kết quả kiểm tra"}</h2>
          <p className="muted">{filename}</p>
          <div className="stats-strip">
            {[
              ["Bản ghi trong file", report.total],
              ["Video hợp lệ", report.accepted],
              ["Video mới", report.new],
              ["Đã có trong thư viện", report.existing],
              ["Bỏ qua", report.skipped],
            ].map(([name, value]) => (
              <div key={name}>
                <span>{name}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <p>
            Dữ liệu mang nhãn “Nhập MediaCrawler”; chưa được xác minh lại trên
            nền tảng. Khi trùng, tool giữ metadata và trạng thái video đang có.
            Video mới ở trạng thái Mới.
          </p>
          {!!report.sample.length && (
            <div className="table-panel">
              <table>
                <thead>
                  <tr>
                    <th>Video xem trước</th>
                    <th>Nền tảng</th>
                    <th>Lượt thích trong file</th>
                  </tr>
                </thead>
                <tbody>
                  {report.sample.map((row, i) => (
                    <tr key={i}>
                      <td>{row.title ?? "Chưa có tiêu đề"}</td>
                      <td>{platformName(row.platform)}</td>
                      <td>{count(row.like_count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!!report.issues.length && (
            <details>
              <summary>{report.issues.length} bản ghi bị bỏ qua</summary>
              <ul>
                {report.issues.map((issue) => (
                  <li key={issue.row}>
                    Bản ghi {issue.row}: {issue.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {report.job_id && (
            <p role="status">
              Đã lưu {report.imported} video vào lượt nhập.{" "}
              <Link className="text-link" href={`/imports/${report.job_id}`}>
                Mở lại lượt nhập
              </Link>{" "}
              ·{" "}
              <Link className="text-link" href="/library">
                Thư viện
              </Link>
            </p>
          )}
        </section>
      )}
      {report?.job_id && (
        <VideoGrid key={report.job_id} jobId={report.job_id} />
      )}
    </>
  );
}
