"""Narrow, deterministic checks for invented currency and monetary scale.

These checks supplement translation review; they do not certify semantics.
They never rewrite a translation or convert a price to another currency.
"""
from __future__ import annotations

import re
import unicodedata


class TranslationMeaningError(ValueError):
    pass


_QTY_VN = (
    r"(?:\d+(?:[.,]\d+)?|nghìn|ngàn|vạn|triệu|tỷ|vài|mấy|chục|trăm|nửa|rưỡi|"
    r"một|hai|ba|bốn|tư|năm|lăm|sáu|bảy|tám|chín|mười|mươi|mốt)"
)

_CNY_SOURCE = (
    r"人民币|人民幣|元|圆|圓|块钱|塊錢|毛钱|毛錢|分钱|分錢|RMB|CNY|[￥¥]|"
    r"(?:\d|[一二两兩三四五六七八九十百千万萬几幾多半点點来來])[块塊毛角]|"
    r"[块塊][一二三四五六七八九半多毛]|"
    r"(?:\d+(?:\.\d+)?|[一二两兩三四五六七八九十百千几幾多来來])[米rR](?![a-zA-Z])"
)

_CURRENCIES = (
    (
        _CNY_SOURCE,
        rf"\b(?:nhân dân tệ|ndt|yuan|rmb|cny)\b|\b{_QTY_VN}\s+tệ\b",
    ),
    (
        r"越南盾|VND|₫",
        rf"\b(?:vnd|việt nam đồng)\b|₫|\b{_QTY_VN}\s+đồng\b"
        r"(?!\s*(?:hồ|nghiệp|đội|phục|màu|bộ|cảm|ý|loại|hương|cỏ|ruộng|bằng|tâm|thời|đều|nhất|xu\s+hướng))",
    ),
    (
        r"美元|美金|美刀|USD|US\$|\$",
        rf"\b(?:usd|đô la mỹ|đô la)\b|\$|\b{_QTY_VN}\s+đô\b(?!\s*(?:thị|con|hộ))",
    ),
    (
        r"欧元|歐元|EUR|€",
        r"\b(?:euro|eur)\b|€",
    ),
    (
        r"日元|日圆|日圓|日币|日幣|JPY",
        rf"\b(?:yên nhật|jpy)\b|\b{_QTY_VN}\s+yên\b"
        r"(?!\s*(?:tâm|tĩnh|ổn|bình|lặng|nghỉ|giấc|vị|ắng|xe|ngựa))",
    ),
)


def validate_monetary_meaning(source: str, translation: str) -> None:
    source = unicodedata.normalize("NFKC", source)
    target = unicodedata.normalize("NFKC", translation).casefold()
    has_currency = False
    for source_pattern, target_pattern in _CURRENCIES:
        if re.search(target_pattern, target, re.IGNORECASE):
            has_currency = True
            if not re.search(source_pattern, source, re.IGNORECASE):
                raise TranslationMeaningError("Bản dịch tự thêm hoặc đổi đơn vị tiền không có trong nguồn.")
    if not (has_currency or re.search(r"\b(?:giá|tiền|chi phí)\b", target)):
        return
    numeric_values = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", source)]
    for pattern, minimum, ideographs in (
        (r"\b(?:nghìn|ngàn)\b", 1000, "千万萬亿億"),
        (r"\btriệu\b", 1000000, "亿億"),
        (r"\btỷ\b", 1000000000, "亿億"),
    ):
        explicit = any(char in source for char in ideographs)
        if minimum == 1000:
            explicit = explicit or bool(re.search(r"\d+(?:\.\d+)?[kK]\b", source))
        # Chinese uses hundred ten-thousands to express millions.
        if minimum == 1000000:
            explicit = explicit or bool(re.search(r"(?:百|\d{3,})[万萬]", source))
        if re.search(pattern, target) and not explicit and not any(n >= minimum for n in numeric_values):
            raise TranslationMeaningError("Bản dịch tự thêm bậc giá tiền không có trong nguồn.")
