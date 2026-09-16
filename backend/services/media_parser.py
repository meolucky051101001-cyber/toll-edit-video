import re


def parse_duration_seconds(duration_str: str | float | int | None) -> int | None:
    """Parse various duration string formats into integer seconds.

    Supports:
    - "00:45" -> 45
    - "02:15" -> 135
    - "01:10:05" -> 4205
    - "45s", "45秒" -> 45
    - "1分30秒" -> 90
    - Float/int seconds directly (e.g. 45.2 -> 45)
    """
    if duration_str is None:
        return None

    if isinstance(duration_str, (int, float)):
        if duration_str < 0:
            return None
        return int(round(duration_str))

    text = str(duration_str).strip()
    if not text:
        return None

    # Handle standard digital clock format: HH:MM:SS or MM:SS
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                return mins * 60 + secs
            if len(parts) == 3:
                hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
                return hours * 3600 + mins * 60 + secs
        except (ValueError, TypeError):
            pass

    # Handle units: X小时Y分Z秒 or X分Y秒 or Z秒, or Xh Ym Zs
    hour_match = re.search(r"(\d+)\s*(?:小时|h)", text, re.IGNORECASE)
    min_match = re.search(r"(\d+)\s*(?:分|分钟|m)", text, re.IGNORECASE)
    sec_match = re.search(r"(\d+)\s*(?:秒|s)", text, re.IGNORECASE)
    if hour_match or min_match or sec_match:
        hours = int(hour_match.group(1)) if hour_match else 0
        mins = int(min_match.group(1)) if min_match else 0
        secs = int(sec_match.group(1)) if sec_match else 0
        total = hours * 3600 + mins * 60 + secs
        if total > 0:
            return total

    # Handle plain numeric strings
    try:
        val = float(text)
        if val >= 0:
            return int(round(val))
    except (ValueError, TypeError):
        pass

    return None


def extract_hashtags(text: str | None) -> list[str]:
    """Extract unique hashtag tags from a title, description or caption.

    Supports both #hashtag and Chinese/Vietnamese Unicode characters.
    """
    if not text:
        return []

    tags = re.findall(r"#([^\s#]+)", text)
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        t = tag.strip().strip(".,;:!?，。！？")
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    return cleaned
