"""Small, dependency-free helpers for Telegram URL messages."""

import re


_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;!?，。；！？)]}"
_XHS_TIMESTAMP_RE = re.compile(
    r"^(https?://(?:www\.)?xhslink\.com/o/[A-Za-z0-9]+?)(?:[0-2]\d:[0-5]\d)$",
    re.IGNORECASE,
)


def extract_http_urls(text):
    """Extract unique HTTP URLs while removing copied chat timestamps."""
    urls = []
    seen = set()
    for raw_url in _HTTP_URL_RE.findall(text or ""):
        candidate = raw_url.rstrip(_TRAILING_PUNCTUATION)
        timestamp_match = _XHS_TIMESTAMP_RE.fullmatch(candidate)
        if timestamp_match:
            candidate = timestamp_match.group(1)
        if candidate and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls
