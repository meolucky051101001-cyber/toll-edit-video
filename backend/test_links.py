# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"C:\tool v1\backend")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from social_downloader import extract_video_url

urls = [
    ("Link 6", "http://xhslink.com/o/54UdlcJtSrv"),
    ("Link 7", "https://v.douyin.com/HERZHNx2P8U/"),
    ("Link 8", "https://v.douyin.com/A4K2rz74dVg/"),
    ("Link 9", "https://v.douyin.com/CbtpWIqfC6k/"),
    ("Link 10", "https://v.douyin.com/JZC5CP_8rMk/")
]

for label, u in urls:
    print(f"Testing {label}: {u}")
    try:
        res = extract_video_url(u)
        title = res.get("title", "")
        has_url = bool(res.get("video_url"))
        print(f"  -> SUCCESS! Title: {title[:40]} | Stream found: {has_url}")
    except Exception as e:
        print(f"  -> ERROR: {e}")
