# -*- coding: utf-8 -*-
import sys
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

log_path = r"C:\tool v1\workspace\bot_system\service_logs\telegram.log"

with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    text = f.read()

# Look for received messages
print("--- Searching for received messages / URLs ---")
found_urls = []
for m in re.finditer(r'(https?://(?:v\.douyin\.com|xhslink\.com|www\.xiaohongshu\.com|kuaishou\.com)[^\s<>"\']+)', text):
    u = m.group(1).rstrip('.,;:')
    if u not in found_urls:
        found_urls.append(u)

print(f"Total unique URLs found: {len(found_urls)}")
for idx, u in enumerate(found_urls, 1):
    print(f"{idx}: {u}")
