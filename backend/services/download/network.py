"""Secure networking transport and address resolution for download services.

Guarantees:
- Pinning TCP connection target to pre-validated IP address (prevents TOCTOU and DNS rebinding).
- Strict TLS SNI and server certificate validation against original request hostname.
- Rejection of all loopback, private, link-local, multicast, and special IP ranges.
- Non-blocking async DNS resolution with thread-safe TTL caching.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.sync import SyncBackend

logger = logging.getLogger("download.network")

# In-memory DNS cache: (host, port) -> (expiry_monotonic, list_of_validated_ips)
_DNS_CACHE: dict[tuple[str, int], tuple[float, list[str]]] = {}
_DNS_CACHE_TTL = 60.0  # 60 seconds TTL


def is_private_or_special_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Returns True if the given IP address is private, loopback, link-local, multicast, or reserved."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_url_syntax(url_str: str) -> tuple[bool, str | None]:
    """Validates URL syntax: requires HTTPS, port 443 (or None), no credentials, and non-empty hostname."""
    if not url_str:
        return False, "URL rỗng."
    try:
        p = urlsplit(url_str)
        if p.scheme != "https":
            return False, f"Giao thức '{p.scheme}' không được phép (chỉ hỗ trợ https)."
        if p.port is not None and p.port != 443:
            return False, f"Cổng {p.port} không được phép (chỉ hỗ trợ cổng 443)."
        if p.username or p.password:
            return False, "URL chứa thông tin xác thực (username/password) không được phép."
        hostname = p.hostname
        if not hostname:
            return False, "URL thiếu tên miền (hostname)."
        host_lower = hostname.lower()
        if host_lower in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False, f"Tên miền nội bộ '{hostname}' bị cấm."
        return True, None
    except Exception as e:
        return False, f"Lỗi phân tích cú pháp URL: {e}"


def resolve_and_validate_host_sync(host: str, port: int = 443) -> list[str]:
    """Synchronously resolves host and ensures ALL returned IP addresses are public and safe."""
    now = time.monotonic()
    cached = _DNS_CACHE.get((host, port))
    if cached and now < cached[0]:
        return cached[1]

    # Check if host is already an IP literal
    try:
        ip_obj = ipaddress.ip_address(host)
        if is_private_or_special_ip(ip_obj):
            raise httpcore.ConnectError(f"Địa chỉ IP '{host}' thuộc dải mạng nội bộ hoặc đặc biệt bị cấm.")
        return [str(ip_obj)]
    except ValueError:
        pass

    if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise httpcore.ConnectError(f"Tên máy chủ '{host}' bị cấm.")

    try:
        addr_infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"Phân giải DNS thất bại cho '{host}': {e}") from e

    if not addr_infos:
        raise httpcore.ConnectError(f"Không nhận được địa chỉ IP nào cho '{host}'.")

    validated: list[str] = []
    for info in addr_infos:
        ip_str = str(info[4][0])
        ip_obj = ipaddress.ip_address(ip_str)
        if is_private_or_special_ip(ip_obj):
            raise httpcore.ConnectError(
                f"Tên miền '{host}' phân giải ra địa chỉ IP nội bộ bị cấm: {ip_str}."
            )
        if ip_str not in validated:
            validated.append(ip_str)

    _DNS_CACHE[(host, port)] = (now + _DNS_CACHE_TTL, validated)
    return validated


async def resolve_and_validate_host_async(host: str, port: int = 443) -> list[str]:
    """Asynchronously resolves host and ensures ALL returned IP addresses are public and safe."""
    now = time.monotonic()
    cached = _DNS_CACHE.get((host, port))
    if cached and now < cached[0]:
        return cached[1]

    try:
        ip_obj = ipaddress.ip_address(host)
        if is_private_or_special_ip(ip_obj):
            raise httpcore.ConnectError(f"Địa chỉ IP '{host}' thuộc dải mạng nội bộ hoặc đặc biệt bị cấm.")
        return [str(ip_obj)]
    except ValueError:
        pass

    if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise httpcore.ConnectError(f"Tên máy chủ '{host}' bị cấm.")

    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"Phân giải DNS thất bại cho '{host}': {e}") from e

    if not addr_infos:
        raise httpcore.ConnectError(f"Không nhận được địa chỉ IP nào cho '{host}'.")

    validated: list[str] = []
    for info in addr_infos:
        ip_str = str(info[4][0])
        ip_obj = ipaddress.ip_address(ip_str)
        if is_private_or_special_ip(ip_obj):
            raise httpcore.ConnectError(
                f"Tên miền '{host}' phân giải ra địa chỉ IP nội bộ bị cấm: {ip_str}."
            )
        if ip_str not in validated:
            validated.append(ip_str)

    _DNS_CACHE[(host, port)] = (now + _DNS_CACHE_TTL, validated)
    return validated


class SafeAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Async network backend that resolves destination IP asynchronously and binds TCP to that IP."""

    def __init__(self) -> None:
        self._backend = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        validated_ips = await resolve_and_validate_host_async(host, port)
        target_ip = validated_ips[0]
        return await self._backend.connect_tcp(
            host=target_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError("Unix sockets not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class SafeSyncNetworkBackend(httpcore.NetworkBackend):
    """Sync network backend that resolves destination IP and binds TCP to that IP."""

    def __init__(self) -> None:
        self._backend = SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        validated_ips = resolve_and_validate_host_sync(host, port)
        target_ip = validated_ips[0]
        return self._backend.connect_tcp(
            host=target_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise NotImplementedError("Unix sockets not allowed")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    """Async HTTP transport configured with SafeAsyncNetworkBackend."""

    def __init__(
        self,
        verify: ssl.SSLContext | str | bool = True,
        retries: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(verify=verify, retries=retries, **kwargs)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._pool._ssl_context,
            network_backend=SafeAsyncNetworkBackend(),
            retries=retries,
        )


class SafeSyncTransport(httpx.HTTPTransport):
    """Sync HTTP transport configured with SafeSyncNetworkBackend."""

    def __init__(
        self,
        verify: ssl.SSLContext | str | bool = True,
        retries: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(verify=verify, retries=retries, **kwargs)
        self._pool = httpcore.ConnectionPool(
            ssl_context=self._pool._ssl_context,
            network_backend=SafeSyncNetworkBackend(),
            retries=retries,
        )


def create_safe_async_client(
    timeout: float | httpx.Timeout = 30.0,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Creates an httpx.AsyncClient with pinned IP connection and follow_redirects=False."""
    t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout, connect=10.0)
    return httpx.AsyncClient(
        transport=SafeAsyncTransport(),
        timeout=t,
        follow_redirects=False,
        headers=headers,
    )


def create_safe_sync_client(
    timeout: float | httpx.Timeout = 10.0,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Creates an httpx.Client with pinned IP connection and follow_redirects=False."""
    t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout, connect=10.0)
    return httpx.Client(
        transport=SafeSyncTransport(),
        timeout=t,
        follow_redirects=False,
        headers=headers,
    )


def is_safe_media_url_sync(url_str: str) -> bool:
    """Synchronously checks if a media URL has valid HTTPS syntax and resolves to public IP."""
    ok, _ = is_safe_url_syntax(url_str)
    if not ok:
        return False
    try:
        p = urlsplit(url_str)
        assert p.hostname is not None
        resolve_and_validate_host_sync(p.hostname, p.port or 443)
        return True
    except Exception:
        return False


async def is_safe_media_url_async(url_str: str) -> bool:
    """Asynchronously checks if a media URL has valid HTTPS syntax and resolves to public IP."""
    ok, _ = is_safe_url_syntax(url_str)
    if not ok:
        return False
    try:
        p = urlsplit(url_str)
        assert p.hostname is not None
        await resolve_and_validate_host_async(p.hostname, p.port or 443)
        return True
    except Exception:
        return False
