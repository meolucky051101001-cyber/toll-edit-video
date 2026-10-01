import os
import sys
import time
import json
import traceback
from pathlib import Path

# Configure paths and UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"C:\tool v1")
sys.path.insert(0, r"C:\tool v1\backend")

from backend.social_downloader import (
    resolve_douyin_viesnap,
    resolve_douyin_so9,
    download_douyin_tiktok,
    download_social_video,
    download_file_stream,
    probe_downloaded_video,
    clean_filename,
    extract_douyin_video_id
)

TEST_OUTPUT_DIR = r"C:\tool v1\workspace\downloads\verify_test"
os.makedirs(TEST_OUTPUT_DIR, exist_ok=True)

test_cases = [
    {
        "id": "TC1_SHORTLINK_1",
        "url": "https://v.douyin.com/gHSNYqyxwZo/",
        "description": "Shortlink Douyin v.douyin.com (Chủ đề Handmade/Đồ chơi)"
    },
    {
        "id": "TC2_SHORTLINK_2",
        "url": "https://v.douyin.com/QQBcVIYQ3so/",
        "description": "Shortlink Douyin v.douyin.com (Chủ đề Mẹo đời sống/DIY)"
    },
    {
        "id": "TC3_DIRECT_WEB",
        "url": "https://www.douyin.com/video/7668518512943549715",
        "description": "Link web chuẩn Douyin trực tiếp (video_id 7668518512943549715)"
    },
    {
        "id": "TC4_DIRECT_WEB_2",
        "url": "https://www.douyin.com/video/7680511057219046719",
        "description": "Link web chuẩn Douyin trực tiếp (video_id 7680511057219046719)"
    },
    {
        "id": "TC5_INVALID_LINK",
        "url": "https://v.douyin.com/invalid_link_test_404/",
        "description": "Link lỗi không tồn tại (Kiểm tra cơ chế Fast-Fail không làm treo bot)"
    }
]

report = {
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "results": [],
    "summary": {}
}

print("=" * 80)
print("BẮT ĐẦU KIỂM TRA TOÀN DIỆN HỆ THỐNG TẢI VIDEO DOUYIN TOOL V1")
print("=" * 80)

# -------------------------------------------------------------
# PHẦN 1: TEST ĐỘC LẬP TỪNG TẦNG RESOLVER
# -------------------------------------------------------------
print("\n[PHẦN 1] Kiểm tra sức khỏe độc lập của từng tầng Resolver:")

# Test Tầng 1: Viesnap/Montague
t0 = time.time()
sample_url = "https://v.douyin.com/gHSNYqyxwZo/"
v_ok, v_url, v_title, v_hdr, v_err = resolve_douyin_viesnap(sample_url)
v_time = time.time() - t0
print(f"  -> Tầng 1 (Montague/Viesnap): {'PASS' if v_ok else 'FAIL'} ({v_time:.2f}s)")
if v_ok:
    print(f"     Tiêu đề: {v_title[:40]}...")
    print(f"     CDN Host: {v_url.split('/')[2]}")
else:
    print(f"     Lỗi: {v_err}")

# Test Tầng 2: SO9 Downloader
t0 = time.time()
s_ok, s_url, s_title, s_err = resolve_douyin_so9(sample_url)
s_time = time.time() - t0
print(f"  -> Tầng 2 (SO9 Downloader): {'PASS' if s_ok else 'FAIL'} ({s_time:.2f}s)")
if s_ok:
    print(f"     Tiêu đề: {s_title[:40]}...")
    print(f"     CDN Host: {s_url.split('/')[2]}")
else:
    print(f"     Lỗi: {s_err}")

# -------------------------------------------------------------
# PHẦN 2: TEST TOÀN DIỆN QUA ROUTER DOWNLOAD_SOCIAL_VIDEO
# -------------------------------------------------------------
print("\n[PHẦN 2] Kiểm tra tải video thực tế qua Router download_social_video:")

success_count = 0
total_download_tests = len([tc for tc in test_cases if tc["id"] != "TC5_INVALID_LINK"])

for tc in test_cases:
    tc_id = tc["id"]
    tc_url = tc["url"]
    tc_desc = tc["description"]
    is_invalid = (tc_id == "TC5_INVALID_LINK")

    print(f"\n--- Đang test: {tc_id} ---")
    print(f"    Mô tả: {tc_desc}")
    print(f"    URL: {tc_url}")

    t_start = time.time()
    prefix = f"verify_{tc_id.lower()}"
    ok, final_path, title, err = download_social_video(tc_url, TEST_OUTPUT_DIR, prefix)
    elapsed = time.time() - t_start

    tc_res = {
        "id": tc_id,
        "url": tc_url,
        "elapsed_seconds": round(elapsed, 2),
        "success": ok,
        "error": err,
    }

    if is_invalid:
        # Link lỗi kỳ vọng: ok == False và thời gian xử lý nhanh (< 15s, không bị treo 60s)
        fast_fail = (not ok) and (elapsed < 20.0)
        tc_res["fast_fail_verified"] = fast_fail
        print(f"    [FAST-FAIL] Kết quả: {'PASS' if fast_fail else 'FAIL'} (Thời gian phản hồi: {elapsed:.2f}s, Lỗi: '{err[:60]}...')")
    else:
        if ok and final_path and os.path.exists(final_path):
            file_size = os.path.getsize(final_path)
            tc_res["file_size_bytes"] = file_size
            tc_res["file_path"] = final_path
            
            # Kiểm định chất lượng video qua ffprobe
            try:
                probe = probe_downloaded_video(final_path)
                tc_res["probe_passed"] = True
                tc_res["duration"] = round(probe.duration_seconds, 2)
                tc_res["video_streams"] = probe.video_stream_count
                print(f"    [DOWNLOAD] PASS! Kích thước: {file_size / (1024*1024):.2f} MB | Thời lượng: {probe.duration_seconds:.1f}s | Tốc độ: {elapsed:.2f}s")
                success_count += 1
            except Exception as pe:
                tc_res["probe_passed"] = False
                tc_res["probe_error"] = str(pe)
                print(f"    [DOWNLOAD] LỖI FFPROBE: {pe}")
            
            # Xóa file test sau khi xác minh xong
            try:
                os.remove(final_path)
            except Exception:
                pass
        else:
            tc_res["probe_passed"] = False
            print(f"    [DOWNLOAD] THẤT BẠI: {err} ({elapsed:.2f}s)")

    report["results"].append(tc_res)

# -------------------------------------------------------------
# PHẦN 3: TEST CƠ CHẾ AUTO-FAILOVER (KHI TẦNG 1 MẤT KẾT NỐI)
# -------------------------------------------------------------
print("\n[PHẦN 3] Kiểm tra cơ chế tự động chuyển tầng dự phòng (Failover):")
print("  Mô phỏng: Tầng 1 (Viesnap) gặp sự cố mạng -> Tầng 2 (SO9) tự động tiếp quản...")

def mock_viesnap_failing(url, timeout=15):
    return False, "", "", {}, "Simulated Network Timeout"

# Tạm thời vá hàm resolve_douyin_viesnap trong module để test failover
import backend.social_downloader as sd_mod
orig_viesnap = sd_mod.resolve_douyin_viesnap
sd_mod.resolve_douyin_viesnap = mock_viesnap_failing

t_failover_start = time.time()
fo_ok, fo_path, fo_title, fo_err = download_douyin_tiktok("https://v.douyin.com/gHSNYqyxwZo/", TEST_OUTPUT_DIR, "test_failover")
fo_elapsed = time.time() - t_failover_start

# Khôi phục hàm gốc
sd_mod.resolve_douyin_viesnap = orig_viesnap

failover_passed = fo_ok and fo_path and os.path.exists(fo_path)
print(f"  Kết quả Failover: {'PASS' if failover_passed else 'FAIL'} (Thời gian chuyển tầng & tải: {fo_elapsed:.2f}s)")
if failover_passed:
    print(f"  -> Tầng 2 SO9 đã cứu hộ thành công! Video tải về: {os.path.getsize(fo_path)} bytes")
    os.remove(fo_path)
else:
    print(f"  -> Failover thất bại: {fo_err}")

report["failover_test"] = {
    "passed": failover_passed,
    "elapsed_seconds": round(fo_elapsed, 2)
}

# -------------------------------------------------------------
# PHẦN 4: DỌN DẸP THƯ MỤC TEST
# -------------------------------------------------------------
try:
    os.rmdir(TEST_OUTPUT_DIR)
except Exception:
    pass

report["summary"] = {
    "total_valid_tests": total_download_tests,
    "successful_downloads": success_count,
    "success_rate_percent": round((success_count / total_download_tests) * 100, 1),
    "tier1_healthy": v_ok,
    "tier2_healthy": s_ok,
    "failover_verified": failover_passed
}

# Lưu report JSON
report_file = r"C:\tool v1\workspace\douyin_download_verification_report.json"
with open(report_file, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)

print("\n" + "=" * 80)
print(f"KẾT QUẢ KIỂM TRA: {success_count}/{total_download_tests} LINK THÀNH CÔNG ({report['summary']['success_rate_percent']}%)")
print(f"Tầng 1 (Montague/Viesnap): {'KHỎE' if v_ok else 'LỖI'}")
print(f"Tầng 2 (SO9 Downloader):   {'KHỎE' if s_ok else 'LỖI'}")
print(f"Cơ chế Chuyển tầng Dự phòng: {'HOÀN HẢO' if failover_passed else 'LỖI'}")
print("=" * 80)
