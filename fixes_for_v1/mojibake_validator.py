import re

def is_mojibake_corrupted(text: str) -> bool:
    """Kiểm tra văn bản tiếng Việt có bị dính lỗi mã hóa kép UTF-8 / CP1252 (Mojibake) không."""
    if not text or not isinstance(text, str):
        return False
        
    corrupted_patterns = [
        r"Ä‘", r"Ä", r"Ã£", r"Ã¡", r"Ã ", r"Ã©", r"Ã¨", r"Ã­", r"Ã³", r"Ã²", r"Ãº", r"Ã¹",
        r"á»™", r"á»", r"áº­", r"áº", r"áº£", r"áº¡", r"Æ°", r"Æ¡", r"Â", r"â€"
    ]
    for pattern in corrupted_patterns:
        if re.search(pattern, text):
            return True
    return False

def validate_translation_texts(texts: list) -> bool:
    """Xác thực toàn bộ mảng câu dịch, bác bỏ nếu có bất kỳ câu nào dính Mojibake."""
    if not isinstance(texts, list) or not texts:
        return False
    for t in texts:
        if is_mojibake_corrupted(t):
            return False
    return True
