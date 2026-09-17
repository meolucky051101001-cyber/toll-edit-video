"""Export only Douyin cookies from a local browser into a Netscape jar.

Cookie values are never printed. The resulting file is sensitive and must stay
outside the Git repository; ``D:\\autodub_secrets`` is the recommended location.
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import tempfile
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any, Dict, Iterable, List

from yt_dlp.cookies import extract_cookies_from_browser


ALLOWED_COOKIE_DOMAINS = ("douyin.com", "iesdouyin.com")
RECOMMENDED_COOKIE_NAMES = {"ttwid", "odin_tt", "passport_csrf_token"}
PLAYWRIGHT_COOKIE_URLS = ("https://www.douyin.com", "https://www.iesdouyin.com")


def _validated_profile_name(profile: str) -> str:
    """Return a safe direct-child browser profile directory name."""
    candidate = Path(profile)
    if (
        candidate.anchor
        or len(candidate.parts) != 1
        or candidate.parts[0] in {".", ".."}
    ):
        raise ValueError("Browser profile must be a direct child directory name")
    return candidate.parts[0]


def is_douyin_cookie(cookie: Cookie) -> bool:
    domain = str(cookie.domain or "").lower().lstrip(".")
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_COOKIE_DOMAINS)


def filtered_cookie_jar(cookies: Iterable[Cookie], output: Path) -> MozillaCookieJar:
    jar = MozillaCookieJar(str(output))
    for cookie in cookies:
        if is_douyin_cookie(cookie):
            jar.set_cookie(copy.copy(cookie))
    return jar


def _playwright_cookie(raw: Dict[str, Any]) -> Cookie:
    domain = str(raw.get("domain") or "")
    expires = raw.get("expires")
    try:
        expires_value = int(float(expires)) if expires and float(expires) > 0 else None
    except (TypeError, ValueError):
        expires_value = None
    rest: Dict[str, Any] = {}
    if raw.get("httpOnly"):
        rest["HTTPOnly"] = True
    if raw.get("sameSite"):
        rest["SameSite"] = str(raw["sameSite"])
    return Cookie(
        version=0,
        name=str(raw.get("name") or ""),
        value=str(raw.get("value") or ""),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=str(raw.get("path") or "/"),
        path_specified=True,
        secure=bool(raw.get("secure")),
        expires=expires_value,
        discard=expires_value is None,
        comment=None,
        comment_url=None,
        rest=rest,
        rfc2109=False,
    )


def _copy_minimal_browser_profile(user_data: Path, profile: str, target: Path) -> None:
    """Copy only decryption metadata and the cookie DB into a temporary profile."""
    profile = _validated_profile_name(profile)
    local_state = user_data / "Local State"
    cookie_database = user_data / profile / "Network" / "Cookies"
    if not local_state.is_file() or not cookie_database.is_file():
        raise FileNotFoundError("Browser Local State or cookie database is missing")
    shutil.copy2(local_state, target / "Local State")
    target_network = target / profile / "Network"
    target_network.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cookie_database, target_network / "Cookies")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(cookie_database) + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(str(target_network / "Cookies") + suffix))


def _read_scoped_cookies_with_playwright(
    executable: Path, user_data: Path, profile_name: str
) -> List[Cookie]:
    from playwright.sync_api import sync_playwright

    print("Opening browser profile through local pipe...", flush=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(user_data),
            executable_path=str(executable),
            headless=True,
            args=[f"--profile-directory={profile_name}"],
            timeout=20000,
        )
        print("Browser profile opened; reading scoped Douyin cookies...", flush=True)
        try:
            raw_cookies = context.cookies(list(PLAYWRIGHT_COOKIE_URLS))
            print(f"Collected {len(raw_cookies)} scoped cookie records", flush=True)
        finally:
            print("Closing browser profile...", flush=True)
            context.close()
            print("Browser profile closed", flush=True)
    return [_playwright_cookie(raw) for raw in raw_cookies]


def extract_with_playwright(
    browser: str, profile: str | None, user_data_override: Path | None = None
) -> List[Cookie]:
    from playwright.sync_api import sync_playwright  # noqa: F401 - validates optional dependency

    local_app_data = Path(os.environ["LOCALAPPDATA"])
    if browser == "chrome":
        user_data = local_app_data / "Google" / "Chrome" / "User Data"
        executable = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    elif browser == "edge":
        user_data = local_app_data / "Microsoft" / "Edge" / "User Data"
        executable = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    else:
        raise ValueError("Playwright profile export currently supports Chrome or Edge")
    if not executable.is_file() or not user_data.is_dir():
        raise FileNotFoundError(f"Browser profile not found for {browser}")

    profile_name = _validated_profile_name(profile or "Default")
    if user_data_override is not None:
        dedicated_user_data = user_data_override.expanduser().resolve()
        if not dedicated_user_data.is_dir():
            raise FileNotFoundError(f"Dedicated browser profile not found: {dedicated_user_data}")
        return _read_scoped_cookies_with_playwright(
            executable, dedicated_user_data, profile_name
        )

    with tempfile.TemporaryDirectory(prefix="autodub-douyin-profile-") as temp_directory:
        isolated_user_data = Path(temp_directory).resolve()
        _copy_minimal_browser_profile(user_data, profile_name, isolated_user_data)
        return _read_scoped_cookies_with_playwright(
            executable, isolated_user_data, profile_name
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="chrome", choices=("chrome", "edge", "firefox"))
    parser.add_argument("--profile", default=None)
    parser.add_argument("--method", default="browser-db", choices=("browser-db", "playwright"))
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="Dedicated non-default Chrome/Edge profile created for AutoDub",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.user_data_dir is not None and args.method != "playwright":
        parser.error("--user-data-dir requires --method playwright")
    if args.method == "playwright" and args.browser == "firefox":
        parser.error("--method playwright supports only chrome or edge")

    if args.method == "playwright":
        source = extract_with_playwright(args.browser, args.profile, args.user_data_dir)
    else:
        source = extract_cookies_from_browser(args.browser, profile=args.profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    jar = filtered_cookie_jar(source, args.output)
    jar.save(ignore_discard=True, ignore_expires=True)

    names = sorted({cookie.name for cookie in jar})
    missing = sorted(RECOMMENDED_COOKIE_NAMES.difference(names))
    print(f"Exported {len(list(jar))} Douyin cookies to {args.output}")
    print("Cookie names: " + ", ".join(names))
    if missing:
        print("Warning - recommended cookies not found: " + ", ".join(missing))
    return 0 if names else 2


if __name__ == "__main__":
    raise SystemExit(main())
