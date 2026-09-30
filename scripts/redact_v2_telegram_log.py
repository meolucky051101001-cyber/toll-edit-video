"""Redact URL query secrets in the V2 Telegram log without printing contents."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from social_downloader import sanitize_text


def redact(apply=False):
    folder = (ROOT / "workspace" / "service_logs").resolve()
    target = folder / "telegram.log"
    if not target.is_file():
        return {"exists": False, "changed_lines": 0}
    if target.resolve().parent != folder or target.is_symlink():
        raise ValueError("Refusing a log outside V2 service_logs")
    original = target.read_text(encoding="utf-8", errors="replace")
    cleaned = sanitize_text(original)
    count = sum(a != b for a, b in zip(original.splitlines(), cleaned.splitlines()))
    if apply and original != cleaned:
        target.write_text(cleaned, encoding="utf-8")
    return {"exists": True, "changed_lines": count, "applied": apply}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    print(redact(parser.parse_args().apply))
