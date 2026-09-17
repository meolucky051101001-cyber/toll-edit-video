"""Small Douyin web-API adapter used by the V2 social downloader.

The request flow and media-candidate selection are adapted from
https://github.com/jiji262/douyin-downloader (commit
9b3b6f1fec09c847f94a95aae16b52ab3bad5f12, MIT).  The X-Bogus signer is
derived from Evil0ctal/Douyin_TikTok_Download_API and remains under Apache-2.0.
See ``docs/douyin-downloader-third-party.md`` for notices.

This module deliberately has no browser/database/Playwright dependency.  It
uses the bot's existing ``requests`` dependency and accepts a dedicated Douyin
cookie through environment configuration.  Cookie values are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import os
import random
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests

try:
    from .douyin_abogus import ABogus, BrowserFingerprintGenerator
except ImportError:  # Running social_downloader.py directly from backend/.
    try:
        from douyin_abogus import ABogus, BrowserFingerprintGenerator
    except ImportError:  # gmssl remains an install-time dependency.
        ABogus = None
        BrowserFingerprintGenerator = None


DOUYIN_BASE_URL = "https://www.douyin.com"
DOUYIN_DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
DOUYIN_PLAY_PATH = "/aweme/v1/play/"
DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


class DouyinDirectError(RuntimeError):
    """A safe, user-facing error from the direct Douyin resolver."""


@dataclass(frozen=True)
class DouyinVideoInfo:
    title: str
    media_urls: Tuple[str, ...]
    download_headers: Mapping[str, str]


def _parse_cookie_header(value: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in (value or "").split(";"):
        if "=" not in item:
            continue
        name, cookie_value = item.split("=", 1)
        name = name.strip()
        if name and not re.search(r"[\s;,]", name):
            parsed[name] = cookie_value.strip()
    return parsed


def _is_douyin_cookie_domain(domain: str) -> bool:
    normalized = str(domain or "").lower().lstrip(".")
    return any(
        normalized == allowed or normalized.endswith("." + allowed)
        for allowed in ("douyin.com", "iesdouyin.com")
    )


def _read_cookie_file(path: Path) -> Dict[str, str]:
    """Read a raw Cookie header or a Netscape/yt-dlp cookie file."""
    cookies: Dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            domain, name, value = fields[0], fields[5], fields[6]
            if _is_douyin_cookie_domain(domain):
                cookies[name.strip()] = value.strip()
            continue
        cookies.update(_parse_cookie_header(line))
    return cookies


def load_douyin_cookies(environment: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Load only the explicitly configured Douyin cookies.

    ``DOUYIN_COOKIE_FILE`` is preferred because it avoids putting a cookie in a
    process command line or configuration screenshot. ``DOUYIN_COOKIE`` remains
    available for a manually copied Cookie header. Nothing is persisted here.
    """
    env = environment if environment is not None else os.environ
    configured_file = str(env.get("DOUYIN_COOKIE_FILE", "") or "").strip().strip('"')
    if configured_file:
        path = Path(configured_file).expanduser()
        if path.is_file():
            return _read_cookie_file(path)
    return _parse_cookie_header(str(env.get("DOUYIN_COOKIE", "") or "").strip())


def configured_cookie_file(environment: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return an existing cookie file path suitable for yt-dlp, if configured."""
    env = environment if environment is not None else os.environ
    raw = str(env.get("DOUYIN_COOKIE_FILE", "") or "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw).expanduser()
    return str(path) if path.is_file() else None


# Copyright (C) 2021 Evil0ctal
# Adapted from Douyin_TikTok_Download_API under the Apache License 2.0.
class _XBogus:
    _character = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
    _ua_key = b"\x00\x01\x0c"

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._hex = {char: index for index, char in enumerate("0123456789abcdef")}

    def _md5_array(self, value: Any) -> List[int]:
        if isinstance(value, str):
            if len(value) > 32:
                data = [ord(char) for char in value]
            else:
                data = [
                    (self._hex[value[index]] << 4) | self._hex[value[index + 1]]
                    for index in range(0, len(value), 2)
                ]
        else:
            data = list(value)
        return list(hashlib.md5(bytes(data)).digest())

    @staticmethod
    def _rc4(key: bytes, data: bytes) -> bytearray:
        state = list(range(256))
        cursor = 0
        for index in range(256):
            cursor = (cursor + state[index] + key[index % len(key)]) % 256
            state[index], state[cursor] = state[cursor], state[index]
        left = right = 0
        encrypted = bytearray()
        for byte in data:
            left = (left + 1) % 256
            right = (right + state[left]) % 256
            state[left], state[right] = state[right], state[left]
            encrypted.append(byte ^ state[(state[left] + state[right]) % 256])
        return encrypted

    def _encode_triplet(self, first: int, second: int, third: int) -> str:
        value = ((first & 255) << 16) | ((second & 255) << 8) | (third & 255)
        return "".join(
            self._character[index]
            for index in (
                (value & 16515072) >> 18,
                (value & 258048) >> 12,
                (value & 4032) >> 6,
                value & 63,
            )
        )

    @staticmethod
    def _encoding_conversion(values: List[int]) -> bytes:
        # Preserve the parameter permutation used by Douyin's web signer.
        a, b, c, e, d, t, f, r, n, o, i, underscore, x, u, s, ell, v, h, p = values
        payload = [
            a,
            int(i),
            b,
            underscore,
            c,
            x,
            e,
            u,
            d,
            s,
            t,
            ell,
            f,
            v,
            r,
            h,
            n,
            p,
            o,
        ]
        return bytes(payload)

    def build(self, url: str) -> str:
        ua_payload = base64.b64encode(
            self._rc4(self._ua_key, self.user_agent.encode("ISO-8859-1"))
        ).decode("ISO-8859-1")
        ua_md5 = self._md5_array(ua_payload)
        empty_md5 = self._md5_array(hashlib.md5(b"").hexdigest())
        url_md5 = self._md5_array(hashlib.md5(url.encode("ISO-8859-1")).hexdigest())
        timer = int(time.time())
        constant = 536919696
        payload: List[int] = [
            64,
            0,
            1,
            12,
            url_md5[14],
            url_md5[15],
            empty_md5[14],
            empty_md5[15],
            ua_md5[14],
            ua_md5[15],
            (timer >> 24) & 255,
            (timer >> 16) & 255,
            (timer >> 8) & 255,
            timer & 255,
            (constant >> 24) & 255,
            (constant >> 16) & 255,
            (constant >> 8) & 255,
            constant & 255,
        ]
        checksum = 0
        for value in payload:
            checksum ^= int(value)
        payload.append(checksum)
        merged = payload[::2] + payload[1::2]
        raw = self._encoding_conversion(merged).decode("ISO-8859-1")
        garbled = chr(2) + chr(255) + self._rc4(
            "ÿ".encode("ISO-8859-1"), raw.encode("ISO-8859-1")
        ).decode("ISO-8859-1")
        signature = "".join(
            self._encode_triplet(ord(garbled[i]), ord(garbled[i + 1]), ord(garbled[i + 2]))
            for i in range(0, len(garbled), 3)
        )
        return f"{url}&X-Bogus={signature}"


def _default_query(cookies: Mapping[str, str]) -> Dict[str, str]:
    ms_token = str(cookies.get("msToken", "") or "").strip()
    if not ms_token:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        ms_token = "".join(secrets.choice(alphabet) for _ in range(182)) + "=="
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "139.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "139.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "support_h265": "1",
        "support_dash": "1",
        "msToken": ms_token,
    }


def _signed_url(path: str, params: Mapping[str, str]) -> str:
    query = urlencode(params)
    if ABogus is not None and BrowserFingerprintGenerator is not None:
        fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
        signer = ABogus(fp=fingerprint, user_agent=DOUYIN_USER_AGENT)
        signed_query, _signature, _user_agent, _body = signer.generate_abogus(query, "")
        return f"{DOUYIN_BASE_URL}{path}?{signed_query}"
    unsigned = f"{DOUYIN_BASE_URL}{path}?{query}"
    return _XBogus(DOUYIN_USER_AGENT).build(unsigned)


def _safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolution_score(entry: Mapping[str, Any], play_addr: Mapping[str, Any]) -> Tuple[int, int]:
    def positive_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    width = positive_int(play_addr.get("width") or entry.get("width"))
    height = positive_int(play_addr.get("height") or entry.get("height"))
    bitrate = positive_int(entry.get("bit_rate"))
    return width * height, bitrate


def _iter_play_addresses(video: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    bitrate_entries = video.get("bit_rate")
    ranked: List[Tuple[Tuple[int, int], Mapping[str, Any]]] = []
    if isinstance(bitrate_entries, list):
        for entry in bitrate_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("play_addr"), dict):
                continue
            play_addr = entry["play_addr"]
            ranked.append((_resolution_score(entry, play_addr), play_addr))
    for _, play_addr in sorted(ranked, key=lambda item: item[0], reverse=True):
        yield play_addr
    for key in ("play_addr_h264", "play_addr", "play_addr_265", "play_addr_256"):
        candidate = video.get(key)
        if isinstance(candidate, dict):
            yield candidate


def _is_watermarked(url: str) -> bool:
    lowered = url.lower()
    return any(
        marker in lowered
        for marker in ("tplv-dy-water", "dy-water", "owner_watermark", "watermark=1", "playwm")
    )


def _video_candidates(detail: Mapping[str, Any]) -> List[str]:
    video = detail.get("video")
    if not isinstance(video, dict):
        return []
    clean_direct: List[str] = []
    clean_other: List[str] = []
    seen = set()
    first_uri = ""
    for play_addr in _iter_play_addresses(video):
        first_uri = first_uri or str(play_addr.get("uri") or "")
        urls = play_addr.get("url_list") or play_addr.get("urlList") or []
        if not isinstance(urls, list):
            continue
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not url or url in seen or _is_watermarked(url):
                continue
            seen.add(url)
            if not urlparse(url).netloc.endswith("douyin.com"):
                clean_direct.append(url)
            else:
                clean_other.append(url)
    if not (clean_direct or clean_other):
        uri = first_uri or str(video.get("vid") or "")
        if uri:
            params = {
                "video_id": uri,
                "ratio": "1080p",
                "line": "0",
                "is_play_url": "1",
                "watermark": "0",
                "source": "PackSourceEnum_PUBLISH",
            }
            clean_other.append(_signed_url(DOUYIN_PLAY_PATH, params))
    return clean_direct + clean_other


def resolve_douyin_video(
    aweme_id: str,
    *,
    session: Optional[requests.Session] = None,
    environment: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
) -> DouyinVideoInfo:
    """Resolve one numeric Douyin aweme id into no-watermark media URLs."""
    if not str(aweme_id).isdigit():
        raise DouyinDirectError("Link Douyin không chứa mã video hợp lệ")
    env = environment if environment is not None else os.environ
    cookies = load_douyin_cookies(env)
    if timeout_seconds is None:
        try:
            timeout_seconds = float(env.get("DOUYIN_API_TIMEOUT_SECONDS", "12") or 12)
        except (TypeError, ValueError):
            timeout_seconds = 12.0
    timeout_seconds = max(3.0, min(float(timeout_seconds), 60.0))
    headers = {
        "User-Agent": DOUYIN_USER_AGENT,
        "Referer": f"{DOUYIN_BASE_URL}/?recommend=1",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    client = session or requests.Session()
    last_status = 0
    for aid in ("6383", "1128"):
        params = _default_query(cookies)
        params.update({"aweme_id": str(aweme_id), "aid": aid})
        try:
            response = client.get(
                _signed_url(DOUYIN_DETAIL_PATH, params),
                headers=headers,
                cookies=cookies,
                timeout=(8, max(8.0, float(timeout_seconds))),
            )
        except requests.RequestException as exc:
            raise DouyinDirectError(f"Kết nối API Douyin thất bại: {type(exc).__name__}") from exc
        last_status = int(response.status_code)
        if response.status_code != 200:
            if response.status_code in (403, 429):
                continue
            raise DouyinDirectError(f"API Douyin trả về HTTP {response.status_code}")
        payload = _safe_json(response)
        detail = payload.get("aweme_detail")
        if isinstance(detail, dict):
            candidates = _video_candidates(detail)
            if not candidates:
                raise DouyinDirectError("Douyin không trả về luồng video sạch")
            title = str(detail.get("desc") or "douyin_video").strip() or "douyin_video"
            download_headers = {
                "User-Agent": DOUYIN_USER_AGENT,
                "Referer": f"{DOUYIN_BASE_URL}/",
                "Origin": DOUYIN_BASE_URL,
            }
            return DouyinVideoInfo(title, tuple(candidates), download_headers)
    if last_status in (403, 429):
        suffix = "; cần cookie Douyin mới trong DOUYIN_COOKIE_FILE" if not cookies else "; cookie đã hết hạn hoặc bị giới hạn"
        raise DouyinDirectError(f"Douyin chặn request (HTTP {last_status}){suffix}")
    raise DouyinDirectError("Douyin không trả về metadata video; hãy cập nhật cookie")
