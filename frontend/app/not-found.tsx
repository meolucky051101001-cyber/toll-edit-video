import Link from "next/link";
export default function NotFound() {
  return (
    <section className="empty-state">
      <h1>Không tìm thấy trang</h1>
      <Link className="button button-primary" href="/">
        Về trang Tìm kiếm
      </Link>
    </section>
  );
}
