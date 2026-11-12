import os

import httpx


def sanitize_no_proxy_env() -> list[str]:
    """Remove NO_PROXY entries httpx cannot parse (IPv6 literals) in place.

    httpx builds a URL pattern for every NO_PROXY entry (``all://*`` + entry)
    and raises ``InvalidURL`` for entries like ``[::1]``, which aborts every
    ``trust_env=True`` client before it can make a request. Returns the
    entries that were dropped.
    """
    dropped: list[str] = []
    for key in [name for name in os.environ if name.upper() == "NO_PROXY"]:
        entries = [part.strip() for part in os.environ[key].split(",") if part.strip()]
        if not entries:
            continue
        kept: list[str] = []
        for entry in entries:
            pattern = entry if entry.startswith(".") else f"*{entry}"
            try:
                httpx.URL(f"all://{pattern}")
            except httpx.InvalidURL:
                dropped.append(entry)
            else:
                kept.append(entry)
        if len(kept) != len(entries):
            os.environ[key] = ",".join(kept)
    return dropped
