import { Layers3 } from "lucide-react";
import Link from "next/link";
export default function Page() {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Bộ sưu tập</h1>
        </div>
      </div>
      <div className="empty-state bordered">
        <Layers3 size={36} />
        <h3>Gom video theo chủ đề</h3>
        <p>
          Bộ sưu tập nhiều-nhiều sẽ được bổ sung sau Phase 1.
          <br />
          Hiện bạn có thể dùng Đã lưu, Yêu thích và Đã dùng trong Thư viện.
        </p>
        <Link href="/library" className="button button-primary">
          Mở Thư viện
        </Link>
      </div>
    </>
  );
}
