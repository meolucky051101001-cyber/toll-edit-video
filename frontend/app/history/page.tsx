"use client";
import Link from "next/link";
import { useState } from "react";
import { History, ArrowUpRight, RotateCcw } from "lucide-react";
import { useResource } from "@/lib/use-resource";
import { statusName, platformName } from "@/lib/api";
import type { Job, Page } from "@/types";
import { ErrorBanner } from "@/components/error-banner";
import { Button } from "@/components/ui/button";
export default function HistoryPage() {
  const [page, setPage] = useState(1);
  const jobs = useResource<Page<Job>>(`/search/jobs?page=${page}`, 3000);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Lịch sử tìm kiếm</h1>
          <p>Mở lại kết quả hoặc bắt đầu một lượt tìm mới.</p>
        </div>
      </div>
      <ErrorBanner message={jobs.error} />
      <div className="table-panel">
        <table>
          <thead>
            <tr>
              <th>Chủ đề</th>
              <th>Nền tảng</th>
              <th>Ngày tìm</th>
              <th>Video</th>
              <th>Trạng thái</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody>
            {jobs.data?.items.map((job) => (
              <tr key={job.id}>
                <td>
                  <strong>{job.original_query}</strong>
                  <small>
                    {job.import_source
                      ? "Nhập MediaCrawler"
                      : job.is_mock
                        ? "Dữ liệu mẫu"
                        : "Dữ liệu nền tảng"}
                  </small>
                </td>
                <td>{job.platforms.map(platformName).join(" / ")}</td>
                <td>{new Date(job.created_at).toLocaleString("vi-VN")}</td>
                <td>{job.processed_count}</td>
                <td>
                  <span className="count-badge">{statusName[job.status]}</span>
                </td>
                <td>
                  <div className="row">
                    {(() => {
                      const mode = job.is_mock
                        ? "mock"
                        : (job.platforms && job.platforms[0]) || "xiaohongshu";
                      return (
                        <>
                          <Link
                            href={
                              job.import_source
                                ? `/imports/${job.id}`
                                : `/?job=${job.id}&mode=${mode}`
                            }
                            className="text-link"
                          >
                            Kết quả
                            <ArrowUpRight size={15} />
                          </Link>
                          {!job.import_source && (
                            <Link
                              href={`/?q=${encodeURIComponent(job.original_query)}&mode=${mode}`}
                              aria-label={`Chạy lại ${job.original_query}`}
                            >
                              <RotateCcw size={16} />
                            </Link>
                          )}
                        </>
                      );
                    })()}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!jobs.data?.items.length && (
          <div className="empty-state">
            <History size={30} />
            <h3>Chưa có lượt tìm kiếm</h3>
            <p>Lịch sử sẽ được lưu tự động khi bạn bắt đầu tìm.</p>
          </div>
        )}
      </div>
      {(jobs.data?.total ?? 0) > 20 && (
        <div className="pagination">
          <Button
            variant="outline"
            onClick={() => setPage((p) => p - 1)}
            disabled={page === 1}
          >
            Trang trước
          </Button>
          <span>Trang {page}</span>
          <Button
            variant="outline"
            onClick={() => setPage((p) => p + 1)}
            disabled={page * 20 >= (jobs.data?.total ?? 0)}
          >
            Trang sau
          </Button>
        </div>
      )}
    </>
  );
}
