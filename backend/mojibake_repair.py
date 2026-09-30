# encoding: utf-8
import ast
import re

cp1252_map = {
    '\u20ac': 0x80, '\u201a': 0x82, '\u0192': 0x83, '\u201e': 0x84,
    '\u2026': 0x85, '\u2020': 0x86, '\u2021': 0x87, '\u02c6': 0x88,
    '\u2030': 0x89, '\u0160': 0x8a, '\u2039': 0x8b, '\u0152': 0x8c,
    '\u017d': 0x8e, '\u2018': 0x91, '\u2019': 0x92, '\u201c': 0x93,
    '\u201d': 0x94, '\u2022': 0x95, '\u2013': 0x96, '\u2014': 0x97,
    '\u02dc': 0x98, '\u2122': 0x99, '\u0161': 0x9a, '\u203a': 0x9b,
    '\u0153': 0x9c, '\u017e': 0x9e, '\u0178': 0x9f
}

pattern = re.compile(
    r'[\xc2-\xdf][\x80-\xbf\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178]|'
    r'[\xe0-\xef][\x80-\xbf\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178]{2}'
)

def decode_chunk(match):
    chunk = match.group(0)
    byte_list = []
    for ch in chunk:
        o = ord(ch)
        if o < 256:
            byte_list.append(o)
        elif ch in cp1252_map:
            byte_list.append(cp1252_map[ch])
        else:
            return chunk
    try:
        return bytes(byte_list).decode('utf-8')
    except UnicodeDecodeError:
        return chunk

def repair_vietnamese_mojibake(text: str) -> str:
    if not text:
        return text
    
    # Specific known multi-character corruptions
    text = text.replace("Ä ang", "Đang")
    text = text.replace("Ä Ã£", "Đã")
    text = text.replace("Ä Ã", "Đã")
    text = text.replace("Ä‘", "đ")
    text = text.replace("Ä'", "đ")
    text = text.replace("Gá» C", "GỐC")
    text = text.replace("Gá»\xa0C", "GỐC")
    text = text.replace("Gá»?C", "GỐC")
    text = text.replace("Sáº CH", "SẠCH")
    text = text.replace("Sáº\xa0CH", "SẠCH")
    text = text.replace("Sáº?CH", "SẠCH")
    text = text.replace("KHÃ”NG", "KHÔNG")
    text = text.replace("Ä á» I", "ĐỜI")
    text = text.replace("Ä á»", "Độ")
    text = text.replace("Ä iá» u", "Điều")
    text = text.replace("Ä áº·c", "Đặc")
    text = text.replace("Ä á»‹nh", "Định")
    text = text.replace("Ä á»ƒ", "Để")
    text = text.replace("Ä áº·t", "Đặt")
    text = text.replace("Ä á» c", "Đọc")
    text = text.replace("Ä áº§u", "Đầu")
    text = text.replace("Ä áº¡i", "Đại")
    text = text.replace("Ä áº£m", "Đảm")
    text = text.replace("Ä áº¿n", "Đến")
    text = text.replace("Ä á»“ng", "Đồng")
    text = text.replace("Ä Ã³", "Đó")
    text = text.replace("Ä Ã¢y", "Đây")
    text = text.replace("Ä Ã£", "Đã")
    text = text.replace("Ä Æ°á»£c", "Được")
    text = text.replace("Ä Æ°á» ng", "Đường")
    text = text.replace("Ä iá»ƒm", "Điểm")
    
    text = pattern.sub(decode_chunk, text)
    
    # Chinese phrase restore if damaged:
    # "ä½ è®¿é—®çš„é¡µé ¢ä¸ è§ äº†" -> "你访问的页面不见了"
    # "é¡µé ¢ä¸ å­˜åœ¨" -> "页面不存在"
    # "è¯¥ç¬”è®°å·²è¢«åˆ é™¤" -> "该笔记已被删除"
    # "ç¬”è®°ä¸ å­˜åœ¨" -> "笔记不存在"
    # "æ‡’æ˜¥ç§‹" -> "懒春秋"
    # "ä¸‰å ˆé™¢" -> "三合院"
    # "å  åœ°" -> "占地"
    # "å¤§æ°”" -> "大气"
    # "å›žå¤ 888" -> "回复888"
    # "å…³æ³¨æˆ‘" -> "关注我"
    # "ç‚¹èµž" -> "点赞"
    zh_map = {
        "ä½ è®¿é—®çš„é¡µé ¢ä¸ è§ äº†": "你访问的页面不见了",
        "é¡µé ¢ä¸ å­˜åœ¨": "页面不存在",
        "è¯¥ç¬”è®°å·²è¢«åˆ é™¤": "该笔记已被删除",
        "ç¬”è®°ä¸ å­˜åœ¨": "笔记不存在",
        "æ‡’æ˜¥ç§‹": "懒春秋",
        "ä¸‰å ˆé™¢": "三合院",
        "å  åœ°": "占地",
        "å¤§æ°”": "大气",
        "å›žå¤ 888": "回复888",
        "å…³æ³¨æˆ‘": "关注我",
        "ç‚¹èµž": "点赞"
    }
    for k, v in zh_map.items():
        text = text.replace(k, v)
    
    text = text.replace("Ä ", "Đ")
    text = text.replace("Ä", "Đ")
    return text

if __name__ == "__main__":
    import os
    files_to_repair = [
        r"C:\tool v1\backend\social_downloader.py",
        r"C:\tool v1\backend\ai\translation.py"
    ]

    for fpath in files_to_repair:
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            repaired = repair_vietnamese_mojibake(content)
            # Validate syntax before saving
            ast.parse(repaired)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(repaired)
            print(f"Repaired and saved: {fpath}")
