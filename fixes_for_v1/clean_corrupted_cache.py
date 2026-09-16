import os
import glob
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Quét tất cả cache dịch thuật trong workspace
cache_dirs = [
    r"C:\tool v1\workspace\translation_cache",
    r"C:\tool v2\workspace\translation_cache"
]

total_found = 0
total_deleted = 0

mojibake_indicators = ["Ã", "Ä", "á»", "Æ°", "Â", "â€™", "â€œ", "â€"]

for cdir in cache_dirs:
    if not os.path.isdir(cdir):
        continue
    files = glob.glob(os.path.join(cdir, "*.json"))
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            texts = data.get("texts", [])
            raw = json.dumps(texts, ensure_ascii=False)
            
            # Kiểm tra nếu chứa dấu hiệu mojibake
            is_corrupted = any(ind in raw for ind in ["Ä‘Ã£", "má»™t", "tháº­t", "khá»•ng", "luÃ´n", "Ä‘Æ°á»£c", "tháº¿ nÃ"])
            if is_corrupted:
                total_found += 1
                print(f"[PHÁT HIỆN CACHE HỎNG] {f}")
                print(f"  Model: {data.get('model')}, Số câu: {len(texts)}")
                print(f"  Ví dụ câu lỗi: {texts[0] if texts else ''}")
                os.remove(f)
                total_deleted += 1
                print(f"  -> ĐÃ XÓA THÀNH CÔNG!")
        except Exception as e:
            print(f"Lỗi đọc {f}: {e}")

print(f"\nTổng kết: Đã phát hiện {total_found} cache lỗi, đã dọn dẹp an toàn {total_deleted} file.")
