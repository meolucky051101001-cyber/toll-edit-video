"""Narrow, deterministic checks for invented currency and monetary scale.

These checks supplement translation review; they do not certify semantics.
They never rewrite a translation or convert a price to another currency.
"""
from __future__ import annotations

import re
import unicodedata


class TranslationMeaningError(ValueError):
    pass


_CURRENCIES = (
    (r"人民币|人民幣|元|圆|圓|块钱|塊錢|RMB|CNY|￥", r"\b(?:nhân dân tệ|tệ|yuan|rmb|cny)\b"),
    (r"越南盾|VND|₫", r"\b(?:vnd|việt nam đồng)\b|₫|\b(?:\d+|nghìn|ngàn|triệu|tỷ|vài|mấy|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)\s+đồng\b"),
    (r"美元|美金|USD|US\$", r"\b(?:usd|đô la mỹ|đô la|đô)\b|\$"),
    (r"欧元|歐元|EUR|€", r"\b(?:euro|eur)\b|€"),
    (r"日元|日圆|日圓|JPY", r"\b(?:yên nhật|yên|jpy)\b"),
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
        # Chinese uses hundred ten-thousands to express millions.
        if minimum == 1000000:
            explicit = explicit or bool(re.search(r"(?:百|\d{3,})[万萬]", source))
        if re.search(pattern, target) and not explicit and not any(n >= minimum for n in numeric_values):
            raise TranslationMeaningError("Bản dịch tự thêm bậc giá tiền không có trong nguồn.")
