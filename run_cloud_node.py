"""
run_cloud_node.py - Standalone Runner cho Cloud GPU Dashboard (Tool V1).
Chạy độc lập trên cổng 8089 (hoặc biến môi trường CLOUD_PORT),
hoàn toàn cách ly khỏi server production 8088/8090.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

# Thêm đường dẫn project vào sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cloud_node")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn

from backend.cloud.cloud_routes import router as cloud_router
from backend.cloud.cloud_security import (
    allowed_loopback_origins,
    is_loopback_client,
    is_valid_loopback_host,
    is_valid_loopback_origin,
    resolve_local_control_token,
)
from backend.cloud.cloud_worker import get_cloud_worker
from backend.cloud.colab_bridge import get_sessions, get_usage


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Đảm bảo control token đã tồn tại trước khi phục vụ dashboard
    resolve_local_control_token()

    # Khởi động worker daemon ngầm
    worker = get_cloud_worker()
    if not worker.is_running():
        worker.start()
        logger.info("[+] Cloud Daemon Worker đã khởi động thành công.")

    # Kiểm tra nhanh số dư và trạng thái Colab
    try:
        usage = get_usage()
        sessions = get_sessions()
        balance = usage.get("balance", 0.0)
        logger.info("[*] Colab Balance: %.2f CU | Active Sessions: %d", balance, len(sessions))
    except Exception as exc:
        logger.warning("[!] Không thể kiểm tra Colab CLI: %s", exc)

    yield

    # Dọn dẹp khi tắt server
    if worker.is_running():
        worker.stop()
        logger.info("[-] Cloud Daemon Worker đã dừng an toàn.")


app = FastAPI(
    title="Tool V1 - Cloud GPU AutoDub Node",
    description="Máy chủ độc lập điều khiển Cloud GPU Pipeline trên Colab",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_loopback_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Local-Control-Token"],
)


@app.middleware("http")
async def cloud_security_middleware(request: Request, call_next):
    """Enforce loopback-only access, Host/Origin validation, and X-Local-Control-Token."""
    host_header = request.headers.get("host", "")
    if not is_valid_loopback_host(host_header):
        return JSONResponse(status_code=403, content={"detail": f"Host không hợp lệ: '{host_header}'"})

    client_host = request.client.host if request.client else ""
    if not is_loopback_client(client_host):
        return JSONResponse(status_code=403, content={"detail": "Chỉ hỗ trợ truy cập nội bộ (localhost)"})

    method = request.method.upper()
    path = request.url.path
    if method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api/"):
        origin_header = request.headers.get("origin", "").strip()
        if origin_header and not is_valid_loopback_origin(origin_header):
            return JSONResponse(status_code=403, content={"detail": f"Origin không hợp lệ: '{origin_header}'"})

        token = request.headers.get("X-Local-Control-Token", "")
        expected_token = resolve_local_control_token()
        if not token or not secrets.compare_digest(str(token), str(expected_token)):
            return JSONResponse(status_code=403, content={"detail": "Yêu cầu thiếu hoặc sai X-Local-Control-Token"})

    return await call_next(request)


# Mount Cloud Router
app.include_router(cloud_router)


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Chuyển hướng từ root sang giao diện /cloud."""
    return RedirectResponse(url="/cloud", status_code=302)


def main():
    parser = argparse.ArgumentParser(description="Tool V1 Cloud GPU Standalone Node")
    parser.add_argument("--port", type=int, default=int(os.getenv("CLOUD_PORT", "8089")), help="Cổng chạy dịch vụ (mặc định: 8089)")
    parser.add_argument("--host", default="127.0.0.1", help="Host lắng nghe (mặc định: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Không tự động mở trình duyệt")
    args = parser.parse_args()

    host_norm = str(args.host or "").strip().lower()
    if host_norm not in ("127.0.0.1", "localhost", "::1") and os.getenv("AUTODUB_ALLOW_REMOTE_CLOUD_NODE") != "1":
        raise SystemExit(
            f"[SECURITY] Từ chối bind Cloud Node ra host không phải loopback '{args.host}'. "
            "Chỉ cho phép 127.0.0.1 / localhost."
        )

    url = f"http://{args.host}:{args.port}/cloud"
    print("\n" + "=" * 70)
    print("   TOOL V1 // CLOUD GPU AUTODUB NODE (STANDALONE RUNNER)")
    print("=" * 70)
    print(f"   Dashboard URL:  {url}")
    print(f"   API Docs:       http://{args.host}:{args.port}/docs")
    print(f"   Cổng dịch vụ:   {args.port} (Cách ly 100% với cổng 8088)")
    print(f"   Mô hình AI:     Demucs htdemucs + Whisper Large-v3 + CapCut Mai + EBU R128")
    print("   Nhấn Ctrl+C để dừng dịch vụ khi không sử dụng.")
    print("=" * 70 + "\n", flush=True)

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
