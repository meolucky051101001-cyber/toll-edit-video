"""
run_cloud_node.py - Standalone Runner cho Cloud GPU Dashboard (Tool V1).
Chạy độc lập trên cổng 8089 (hoặc biến môi trường CLOUD_PORT),
phục vụ dashboard riêng với server Local trên 8088/8090.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
import secrets
import socket
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
from backend.cloud.cloud_worker import get_cloud_worker, peek_cloud_worker
from backend.cloud.cloud_telegram_bot import (
    get_cloud_telegram_bot,
    peek_cloud_telegram_bot,
    peek_cloud_telegram_bot_worker,
)
from backend.cloud.cloud_config import WORKSPACE_DIR

NODE_APP_ID = "tool-v1-cloud-node"
NODE_INSTANCE = secrets.token_hex(16)
_LIFESPAN_LOCK = threading.Lock()
_LIFESPAN_WORKERS = {}
SHUTDOWN_TIMEOUT_SECONDS = 15.0
_STARTUP_REPORT = None
_ACTIVE_UVICORN_SERVER = None


def _report_startup(status: str, code: int = 0):
    if _STARTUP_REPORT is None:
        return
    try:
        path = Path(_STARTUP_REPORT)
        temporary = path.with_suffix(".tmp")
        payload = {"status": status, "code": code, "pid": os.getpid(), "ppid": os.getppid()}
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logger.error("Không ghi được trạng thái khởi động Dashboard.")


def _source_revision(root: Path = PROJECT_ROOT) -> str:
    """Fingerprint Cloud Node code at process startup; never read workspace credentials."""
    files = [root / "run_cloud_node.py", root / "start_cloud_dashboard.bat",
             root / "backend" / "templates" / "cloud_dashboard.html"]
    files.extend(path for path in (root / "launch_cloud_dashboard.py", root / "start_cloud_dashboard.vbs")
                 if path.is_file())
    files.extend(sorted((root / "backend" / "cloud").rglob("*.py")))
    scripts = root / "backend" / "cloud" / "gpu_scripts"
    files.extend(path for path in (scripts / "cloud_env_manifest.json", scripts / "requirements_cloud.txt") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


NODE_REVISION = _source_revision()


def _checkout_workspace_id() -> str:
    paths = [os.path.normcase(str(path.resolve())) for path in (PROJECT_ROOT, Path(WORKSPACE_DIR))]
    return hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()


def _node_identity() -> str:
    paths = [os.path.normcase(str(path.resolve())) for path in (PROJECT_ROOT, Path(WORKSPACE_DIR))]
    return hashlib.sha256("\n".join([*paths, NODE_REVISION]).encode("utf-8")).hexdigest()


def _readiness_proof(challenge: str, identity: str, instance: str, ready: bool) -> str:
    message = json.dumps([NODE_APP_ID, challenge, identity, instance, ready], separators=(",", ":"))
    return hmac.new(resolve_local_control_token().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.node_ready = False
    app.state.node_stopping = False
    app.state.node_shutdown_complete = True
    resolve_local_control_token()
    started_bot = None
    if getattr(app.state, "autostart_telegram_bot", False):
        bot_service = get_cloud_telegram_bot()
        if bot_service.start_if_configured():
            started_bot = bot_service

    # Desktop opening stays idle; Start/Enqueue controls own worker startup.
    if not getattr(app.state, "autostart_worker", True):
        app.state.node_ready = True
        try:
            yield
        finally:
            app.state.node_ready = False
            app.state.node_stopping = True
            if started_bot is not None:
                if not started_bot.stop(timeout=min(8.0, SHUTDOWN_TIMEOUT_SECONDS)):
                    app.state.node_shutdown_complete = False
                    logger.warning("[!] Cloud Telegram Bot còn đang dừng.")
            if getattr(app.state, "owns_process_worker", False):
                worker = peek_cloud_worker()
                if worker is not None:
                    result = worker.stop(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                    app.state.node_shutdown_complete = bool(app.state.node_shutdown_complete and result.get("stopped"))
                    if not app.state.node_shutdown_complete:
                        logger.warning("[!] Worker còn đang dừng; chưa xác nhận thu hồi GPU.")
        return

    # Direct CLI usage retains automatic worker startup by default.
    worker = get_cloud_worker()
    with _LIFESPAN_LOCK:
        key = id(worker)
        entry = _LIFESPAN_WORKERS.get(key)
        if entry is None:
            owned = not worker.is_running()
            if owned:
                worker.start()
                logger.info("[+] Cloud Daemon Worker đã khởi động thành công.")
            entry = _LIFESPAN_WORKERS[key] = {"users": 0, "owned": owned}
        entry["users"] += 1

    # Provider availability must not block the local Dashboard from serving.
    app.state.node_ready = True
    try:
        yield
    finally:
        app.state.node_ready = False
        app.state.node_stopping = True
        if started_bot is not None:
            if not started_bot.stop(timeout=min(8.0, SHUTDOWN_TIMEOUT_SECONDS)):
                app.state.node_shutdown_complete = False
                logger.warning("[!] Cloud Telegram Bot còn đang dừng.")
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
        with _LIFESPAN_LOCK:
            entry["users"] -= 1
            last_user = entry["users"] == 0
            if last_user:
                try:
                    result = worker.stop(timeout=max(0., deadline - time.monotonic())) if entry["owned"] else None
                finally:
                    _LIFESPAN_WORKERS.pop(key, None)
        if last_user and entry["owned"]:
            app.state.node_shutdown_complete = bool(result.get("stopped"))
            if app.state.node_shutdown_complete:
                logger.info("[-] Cloud Daemon Worker đã dừng.")
            else:
                logger.warning("[!] Worker còn đang dừng; chưa xác nhận thu hồi GPU.")
        if (last_user and getattr(app.state, "owns_process_worker", False)
                and not entry["owned"] and worker.is_running()):
            app.state.node_shutdown_complete = False
            logger.error("Cloud worker thuộc consumer khác; chưa xác nhận dừng tiến trình.")


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
        if bool(getattr(app.state, "node_stopping", False)):
            return JSONResponse(status_code=503, content={"detail": "Cloud Node đang dừng; chưa nhận thao tác mới."},
                                headers={"Retry-After": "1"})
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


@app.get("/api/cloud/ready", include_in_schema=False)
async def node_readiness(request: Request):
    """Prove this checkout/workspace is ready without sending its token to a busy port."""
    challenge = request.query_params.get("challenge", "")
    if len(challenge) != 32 or any(char not in "0123456789abcdef" for char in challenge):
        return JSONResponse(status_code=400, content={"detail": "Invalid readiness challenge"})
    identity = _node_identity()
    ready = bool(getattr(app.state, "node_ready", False))
    return JSONResponse(status_code=200 if ready else 503, headers={"Cache-Control": "no-store"}, content={
        "app": NODE_APP_ID, "identity": identity, "workspace_id": _checkout_workspace_id(),
        "instance": NODE_INSTANCE, "ready": ready,
        "revision": NODE_REVISION,
        "proof": _readiness_proof(challenge, identity, NODE_INSTANCE, ready),
    })


@app.post("/api/cloud/node/retire", include_in_schema=False)
async def node_retire_if_idle():
    """Allow a newer launcher from the same authenticated checkout to replace an idle hidden node."""
    w = peek_cloud_worker()
    bw = peek_cloud_telegram_bot_worker()
    bot_svc = peek_cloud_telegram_bot()
    if (
        (w is not None and w.get_current_job() is not None)
        or (bw is not None and bw.get_current_job() is not None)
        or (bot_svc is not None and bot_svc.is_busy())
    ):
        return JSONResponse(status_code=409, content={"retired": False, "busy": True})
    srv = _ACTIVE_UVICORN_SERVER
    if srv is None:
        return JSONResponse(status_code=503, content={"retired": False, "busy": False})
    srv.should_exit = True
    return JSONResponse(status_code=200, content={"retired": True, "busy": False})


def _retire_stale_same_checkout_node(host: str, port: int) -> str:
    """Verify HMAC proof of an older same-checkout node before requesting graceful idle handoff."""
    challenge = secrets.token_hex(16)
    conn = http.client.HTTPConnection(host, port, timeout=1.0)
    try:
        conn.request("GET", f"/api/cloud/ready?challenge={challenge}")
        resp = conn.getresponse()
        if resp.status != 200:
            return "unverified"
        raw = resp.read(4097)
        if len(raw) > 4096:
            return "unverified"
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("app") != NODE_APP_ID or payload.get("ready") is not True:
            return "unverified"
        if payload.get("workspace_id") != _checkout_workspace_id():
            return "unverified"
        remote_identity = str(payload.get("identity") or "")
        remote_instance = str(payload.get("instance") or "")
        if not remote_identity or not remote_instance:
            return "unverified"
        expected_proof = _readiness_proof(challenge, remote_identity, remote_instance, True)
        if not secrets.compare_digest(str(payload.get("proof") or ""), expected_proof):
            return "unverified"
    except (OSError, ValueError, http.client.HTTPException):
        return "unverified"
    finally:
        conn.close()

    conn2 = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        conn2.request(
            "POST",
            "/api/cloud/node/retire",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Local-Control-Token": resolve_local_control_token(),
            },
        )
        resp2 = conn2.getresponse()
        _ = resp2.read(1024)
        if resp2.status == 200:
            return "retired"
        if resp2.status == 409:
            return "busy_verified"
        return "unverified"
    except (OSError, http.client.HTTPException):
        return "unverified"
    finally:
        conn2.close()


def _probe_ready(host: str, port: int, *, instance: str | None = None) -> bool:
    challenge = secrets.token_hex(16)
    connection = http.client.HTTPConnection(host, port, timeout=.75)
    try:
        connection.request("GET", f"/api/cloud/ready?challenge={challenge}")
        response = connection.getresponse()
        if response.status != 200:
            return False
        raw = response.read(4097)
        if len(raw) > 4096:
            return False
        payload = json.loads(raw)
        identity = _node_identity()
        if (not isinstance(payload, dict) or payload.get("app") != NODE_APP_ID
                or payload.get("identity") != identity or payload.get("ready") is not True
                or payload.get("revision") != NODE_REVISION
                or not isinstance(payload.get("instance"), str)
                or (instance is not None and payload["instance"] != instance)):
            return False
        expected = _readiness_proof(challenge, identity, payload["instance"], True)
        return secrets.compare_digest(str(payload.get("proof", "")), expected)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _wait_ready(host: str, port: int, *, instance: str | None = None,
                timeout: float = 30., stopped: threading.Event | None = None) -> bool:
    stopped = stopped or threading.Event()
    deadline = time.monotonic() + timeout
    while not stopped.is_set() and time.monotonic() < deadline:
        if _probe_ready(host, port, instance=instance):
            return not stopped.is_set()
        stopped.wait(.2)
    return False


def _open_browser_when_ready(host: str, port: int, url: str, stopped: threading.Event, *, open_browser: bool = True):
    if _wait_ready(host, port, instance=NODE_INSTANCE, stopped=stopped):
        _report_startup("ready")
        if open_browser:
            _open_browser(url)
    elif not stopped.is_set():
        _report_startup("failed", 1)
        logger.error("Dashboard chưa sẵn sàng sau 30 giây; xem nhật ký khởi động. URL: %s", url)


def _open_browser(url: str):
    try:
        if not webbrowser.open(url):
            logger.warning("Không mở được trình duyệt tự động. Mở URL: %s", url)
    except Exception as exc:
        logger.warning("Không mở được trình duyệt (%s). Mở URL: %s", type(exc).__name__, url)


def _bind_listener(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, port))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def main():
    global _STARTUP_REPORT, _ACTIVE_UVICORN_SERVER
    parser = argparse.ArgumentParser(description="Tool V1 Cloud GPU Standalone Node")
    parser.add_argument("--port", type=int, default=int(os.getenv("CLOUD_PORT", "8089")), help="Cổng chạy dịch vụ (mặc định: 8089)")
    parser.add_argument("--host", default="127.0.0.1", help="Host lắng nghe (mặc định: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Không tự động mở trình duyệt")
    parser.add_argument("--no-worker", action="store_true", help="Mở Dashboard, chờ thao tác nhận việc có chủ đích")
    parser.add_argument("--no-telegram-bot", action="store_true", help="Không tự khởi động Telegram Bot Cloud kèm dịch vụ")
    parser.add_argument("--startup-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    _STARTUP_REPORT = args.startup_report
    if os.name == "nt" and not os.getenv("AUTODUB_CLOUD_OUTPUT_DIR", "").strip():
        if Path("D:/").exists():
            os.environ["AUTODUB_CLOUD_OUTPUT_DIR"] = r"D:\ToolV1_Cloud"
    if not 1 <= args.port <= 65535:
        parser.error("Cổng dịch vụ phải nằm trong khoảng 1–65535.")

    host_norm = str(args.host or "").strip().lower()
    if host_norm not in ("127.0.0.1", "localhost", "::1") and os.getenv("AUTODUB_ALLOW_REMOTE_CLOUD_NODE") != "1":
        raise SystemExit(
            f"[SECURITY] Từ chối bind Cloud Node ra host không phải loopback '{args.host}'. "
            "Chỉ cho phép 127.0.0.1 / localhost."
        )

    bind_host = "127.0.0.1" if host_norm == "localhost" else args.host
    probe_host = "::1" if ":" in bind_host else "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    url_host = f"[{probe_host}]" if ":" in probe_host else probe_host
    url = f"http://{url_host}:{args.port}/cloud"
    listener = None
    try:
        listener = _bind_listener(bind_host, args.port)
    except OSError as exc:
        if _wait_ready(probe_host, args.port, timeout=3.0):
            logger.info("Dashboard của cùng bản mã/workspace đã chạy: %s", url)
            _report_startup("ready")
            if not args.no_browser:
                _open_browser(url)
            return 0
        retire_state = _retire_stale_same_checkout_node(probe_host, args.port)
        if retire_state == "busy_verified":
            logger.info("Cloud Node cùng workspace đang xử lý job; mở giao diện hiện hành: %s", url)
            _report_startup("ready")
            if not args.no_browser:
                _open_browser(url)
            return 0
        if retire_state == "retired":
            rebind_deadline = time.monotonic() + 12.0
            while time.monotonic() < rebind_deadline:
                time.sleep(0.25)
                try:
                    listener = _bind_listener(bind_host, args.port)
                    logger.info("Đã tự động nâng cấp phiên bản Cloud Node nền trên cổng %s.", args.port)
                    break
                except OSError:
                    continue
        if listener is None:
            logger.error("Không mở được cổng %s (%s). Dịch vụ tại cổng này chưa được xác minh là cùng Cloud Node; không khởi động worker.", args.port, exc)
            _report_startup("failed", 2)
            return 2
    print("\n" + "=" * 70)
    print("   TOOL V1 // CLOUD GPU AUTODUB NODE (STANDALONE RUNNER)")
    print("=" * 70)
    print(f"   Dashboard URL:  {url}")
    print(f"   API Docs:       http://{url_host}:{args.port}/docs")
    print(f"   Cổng dịch vụ:   {args.port} (Dashboard Cloud riêng)")
    print(f"   Mô hình AI:     Demucs htdemucs + Whisper Large-v3 + CapCut Mai + EBU R128")
    print("   Huỷ job và xác minh thu hồi GPU trước khi nhấn Ctrl+C để đóng dịch vụ.")
    print("=" * 70 + "\n", flush=True)

    stopped = threading.Event()
    browser_thread = None
    app.state.owns_process_worker = True
    app.state.autostart_worker = not args.no_worker
    app.state.autostart_telegram_bot = not args.no_telegram_bot
    app.state.node_shutdown_complete = True
    try:
        if not args.no_browser or _STARTUP_REPORT is not None:
            browser_thread = threading.Thread(target=_open_browser_when_ready, args=(probe_host, args.port, url, stopped),
                                              kwargs={"open_browser": not args.no_browser}, name="CloudDashboardBrowser", daemon=True)
            browser_thread.start()
        server = uvicorn.Server(uvicorn.Config(app, host=bind_host, port=args.port, log_level="info", access_log=False))
        _ACTIVE_UVICORN_SERVER = server
        server.run(sockets=[listener])
        exit_code = 0 if server.started and app.state.node_shutdown_complete else 1
    except Exception:
        logger.exception("Không khởi động được Cloud Dashboard.")
        _report_startup("failed", 1)
        exit_code = 1
    finally:
        _ACTIVE_UVICORN_SERVER = None
        stopped.set()
        listener.close()
        if browser_thread is not None:
            browser_thread.join(timeout=1.5)
            if browser_thread.is_alive():
                logger.error("Cloud browser readiness thread chưa dừng sau deadline.")
                exit_code = 1
        app.state.owns_process_worker = False
        app.state.autostart_telegram_bot = False
        if exit_code:
            _report_startup("failed", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
