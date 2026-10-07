# -*- coding: utf-8 -*-
import sys
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

log_path = r"C:\tool v1\workspace\bot_system\service_logs\telegram.log"

with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    lines = [l for l in f if l.startswith("2026-09-30")]

print(f"Total lines today (2026-09-30): {len(lines)}")

# Search for URLs in the log
found_urls = []
for idx, l in enumerate(lines):
    matches = re.findall(r'https?://[^\s<>"\')]+', l)
    for m in matches:
        m_clean = m.rstrip(".,;:)")
        if any(dom in m_clean for dom in ["xhslink.com", "douyin.com", "kuaishou.com", "tiktok.com", "bilibili.com"]):
            found_urls.append((m_clean, l.strip()[:100]))

# Unique preserving order
seen = set()
unique_urls = []
for u, line in found_urls:
    if u not in seen:
        seen.add(u)
        unique_urls.append((u, line))

print(f"\n=== FOUND UNIQUE VIDEO/SOCIAL URLS ({len(unique_urls)}) ===")
for i, (u, line) in enumerate(unique_urls, 1):
    print(f"[{i}] {u}")
