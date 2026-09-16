"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useSyncExternalStore } from "react";
import {
  Search,
  Library,
  FileUp,
  History,
  Settings2,
  Layers3,
  ArrowUpRight,
  Moon,
  Sun,
  Clapperboard,
} from "lucide-react";
import { useResource } from "@/lib/use-resource";
import type { Health } from "@/types";
import { Button } from "./ui/button";

function subscribeTheme(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener("research-theme-change", callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener("research-theme-change", callback);
  };
}
const themeSnapshot = () => localStorage.getItem("research-theme") === "dark";
const serverTheme = () => false;
const links = [
  ["/", "Tìm kiếm", Search],
  ["/library", "Thư viện", Library],
  ["/imports", "Nhập dữ liệu", FileUp],
  ["/collections", "Bộ sưu tập", Layers3],
  ["/history", "Lịch sử", History],
  ["/settings", "Cài đặt", Settings2],
] as const;
export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const health = useResource<Health>("/health", 5000);
  const dark = useSyncExternalStore(subscribeTheme, themeSnapshot, serverTheme);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);
  function theme() {
    localStorage.setItem("research-theme", !dark ? "dark" : "light");
    window.dispatchEvent(new Event("research-theme-change"));
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/">
          <span className="brand-icon">
            <Clapperboard size={23} />
          </span>
          <span>
            Video Research<small>YOUR DISCOVERY WORKSPACE</small>
          </span>
        </Link>
        <div className="workspace-label">KHÔNG GIAN CÁ NHÂN</div>
        <nav aria-label="Điều hướng chính">
          {links.map(([url, name, Icon]) => (
            <Link
              key={url}
              href={url}
              className={`nav-link ${pathname === url ? "active" : ""}`}
            >
              <Icon size={19} />
              {name}
              {pathname === url && <span className="nav-dot" />}
            </Link>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <a
            href="http://127.0.0.1:8088/"
            target="_blank"
            rel="noopener noreferrer"
            className="tool-v1-link"
            title="Mở Bảng Điều Khiển & Giám Sát Render Tool Làm Video V1 (http://127.0.0.1:8088)"
          >
            <span className="tool-v1-icon">🎬</span>
            <div className="tool-v1-text">
              <strong>Tool Làm Video (V1)</strong>
              <small>Bảng điều khiển render ↗</small>
            </div>
          </a>
          <div className="local-card">
            <span className="status-dot" />
            Chạy trên máy của bạn<p>Dữ liệu được lưu trong SQLite.</p>
          </div>
          <div className="profile">
            <span className="avatar">ME</span>
            <div>
              Workspace cá nhân<small>Local · Windows</small>
            </div>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <span>
            Workspace <span className="slash">/</span>{" "}
            <strong>
              {links.find((l) => l[0] === pathname)?.[1] ?? "Tìm kiếm"}
            </strong>
          </span>
          <div className="row">
            <Link
              href="/health"
              className={`health ${health.error ? "offline" : ""}`}
            >
              <span className="status-dot" />
              {health.error
                ? "Backend offline"
                : health.data
                  ? "Backend online"
                  : "Đang kết nối"}
            </Link>
            <Button
              variant="ghost"
              size="icon"
              onClick={theme}
              aria-label="Đổi giao diện sáng tối"
            >
              {dark ? <Sun size={18} /> : <Moon size={18} />}
            </Button>
          </div>
        </header>
        <main>{children}</main>
        <footer>
          AI Video Research Tool{" "}
          <span>
            Phase 2 · Local workspace <ArrowUpRight size={13} />
          </span>
        </footer>
      </div>
    </div>
  );
}
