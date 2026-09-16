"use client";
import { Button } from "@/components/ui/button";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <section className="empty-state bordered" role="alert">
      <h1>Không thể mở trang này</h1>
      <p>
        Hãy thử tải lại. Nếu vẫn gặp lỗi, kiểm tra logs/frontend-error.log trong
        thư mục dự án.
      </p>
      <Button onClick={reset}>Thử lại</Button>
    </section>
  );
}
