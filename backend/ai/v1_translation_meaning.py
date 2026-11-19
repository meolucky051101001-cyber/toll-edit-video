"""Check explicit, comparable Chinese/Vietnamese subtitle facts before dubbing."""

import re
import unicodedata
from decimal import Decimal


_ZH_DIGITS = dict(zip("零〇一二两兩三四五六七八九", (0, 0, 1, 2, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_VI_DIGITS = dict(zip("không một hai ba bốn năm sáu bảy tám chín".split(), range(10)))
_VI_DIGITS.update(mốt=1, tư=4, lăm=5)
_ZH_SCALES = {"十": 10, "百": 100, "千": 1000, "万": 10000, "萬": 10000}
_VI_SCALES = {"mười": 10, "mươi": 10, "trăm": 100, "nghìn": 1000, "ngàn": 1000, "vạn": 10000}
_ARABIC = r"\d+(?:[.,]\d+)*"
_ZH_NUMBER = rf"(?:{_ARABIC}[千萬万亿億]?|[{''.join(_ZH_DIGITS)}十百千萬万点點]+)"
_VI_WORDS = "|".join((*_VI_DIGITS, *_VI_SCALES, "linh", "lẻ"))
_ARABIC_SCALES = {"千": 1_000, "万": 10_000, "萬": 10_000, "亿": 100_000_000, "億": 100_000_000,
                  "trăm": 100, "nghìn": 1_000, "ngàn": 1_000, "triệu": 1_000_000, "tỷ": 1_000_000_000}
_VI_NUMBER = rf"(?:{_ARABIC}(?:\s+(?:trăm|nghìn|ngàn|triệu|tỷ))?|(?:{_VI_WORDS})(?:\s+(?:{_VI_WORDS}))*?(?:\s+phẩy\s+(?:{_VI_WORDS})(?:\s+(?:{_VI_WORDS}))*)?)"

# Compare measurements in the same base unit. Currency is never converted.
_UNITS = (
    ("length", "1", r"毫米|公厘|mm", r"mi li mét|mi-li-mét|milimét|milimet|mm"),
    ("length", "10", r"厘米|公分|cm", r"xăng ti mét|xăng-ti-mét|centimét|centimet|cm"),
    ("length", "1000", r"米|m", r"mét|m"),
    ("length", "0.01", r"丝|絲", r"sợi"),
    ("mass", "1", r"克|g", r"gam|gram|g"),
    ("mass", "1000", r"公斤|千克|kg", r"ki lô gam|ki-lô-gam|kilôgam|kilogram|kg|ký|kí"),
    ("volume", "1", r"毫升|ml", r"mi li lít|mi-li-lít|mililít|ml"),
    ("volume", "1000", r"升|l", r"lít|lit|l"),
    ("percent", "1", r"%", r"phần trăm|%"),
    ("CNY", "1", r"元|块钱|塊錢|人民币|人民幣|cny|rmb", r"nhân dân tệ|tệ|yuan|cny|rmb"),
    ("VND", "1", r"越南盾|vnd|₫", r"đồng|vnd|₫"),
    ("USD", "1", r"美元|美金|usd", r"đô la mỹ|đô la|usd"),
    ("count", "1", r"个|個|名|位|件|格|层|層|次|款|张|張|只|隻|套|袋|包|份",
     r"cái|chiếc|túi|gói|ngăn|ô|tầng|lớp|bộ|lần|loại|mẫu|tấm|người|tên|con|phần|hộp|món|viên|miếng|cuộn|thùng|bước|bé|em|đứa"),
)


def _unit_pattern(language):
    aliases = {alias: (kind, Decimal(scale)) for kind, scale, zh, vi in _UNITS
               for alias in (zh if language == "zh" else vi).split("|")}
    alternatives = "|".join(map(re.escape, sorted(aliases, key=len, reverse=True)))
    number = _ZH_NUMBER if language == "zh" else _VI_NUMBER
    boundary = r"(?![a-z0-9])" if language == "zh" else r"(?!\w)"
    start = r"(?<![a-z0-9.,])" if language == "zh" else r"(?<![\w.,])"
    return re.compile(rf"{start}(?P<number>{number})\s*(?P<unit>{alternatives}){boundary}"), aliases


_ZH_MEASURE, _ZH_UNITS = _unit_pattern("zh")
_VI_MEASURE, _VI_UNITS = _unit_pattern("vi")
_ZH_PERCENT = re.compile(rf"百分之({_ZH_NUMBER})")
_ZH_IS_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_BARE_ARABIC = re.compile(rf"(?<![a-z0-9.,]){_ARABIC}(?![a-z0-9.,])")
_SOCIAL_REPLY = re.compile(r"(?:评论|評論|回复|回覆|留言|扣)\s*\d+")
_MODEL_OR_SIZE = re.compile(r"尺寸|规格|規格|尺码|尺碼|型号|型號|大号|大號|小号|小號")


def _value(number, language):
    """Normalize written and spoken numerals without using binary floats."""
    if number[0].isdigit():
        match = re.fullmatch(rf"({_ARABIC})(?:\s*({'|'.join(_ARABIC_SCALES)}))?", number)
        if not match:
            return None
        number, scale = match.groups()
        thousands, decimal = (",", ".") if language == "zh" else (".", ",")
        if thousands in number and decimal not in number and not re.fullmatch(
            rf"[1-9]\d{{0,2}}(?:{re.escape(thousands)}\d{{3}})+", number
        ):
            thousands, decimal = decimal, thousands
        normalized = number.replace(thousands, "").replace(decimal, ".")
        return Decimal(normalized) * _ARABIC_SCALES.get(scale, 1) if re.fullmatch(r"\d+(?:\.\d+)?", normalized) else None

    if language == "zh":
        digits, scales = _ZH_DIGITS, _ZH_SCALES
        pieces = re.split(r"[点點]", number, maxsplit=1)
        whole = list(pieces[0])
        fraction = list(pieces[1]) if len(pieces) == 2 else []
    else:
        digits, scales = _VI_DIGITS, _VI_SCALES
        whole, sep, tail = number.partition(" phẩy ")
        whole = whole.split()
        fraction = tail.split() if sep else []
    if not any(token in scales for token in whole):
        integer = int("".join(str(digits[token]) for token in whole if token in digits) or 0)
    else:
        integer = section = 0
        digit = None
        for token in whole:
            if token in digits:
                digit = digits[token]
            elif token in scales:
                scale = scales[token]
                if scale >= 1000:
                    integer += (section + (digit or (0 if section else 1))) * scale
                    section = 0
                else:
                    section += (digit if digit is not None else 1) * scale
                digit = None
        integer += section + (digit or 0)
    if fraction and all(token in digits for token in fraction):
        return Decimal(f"{integer}." + "".join(str(digits[token]) for token in fraction))
    return Decimal(integer) if not fraction else None


def _facts(text, language):
    pattern, aliases = (_ZH_MEASURE, _ZH_UNITS) if language == "zh" else (_VI_MEASURE, _VI_UNITS)
    found = set()
    for match in pattern.finditer(text):
        unit = match["unit"]
        if language == "zh" and match.start() and text[match.start() - 1] == "第":
            continue  # An ordinal such as 第一个 does not count a quantity.
        # Exclude nouns such as “đồng hồ” from the currency detector.
        if unit == "đồng" and re.match(r"\s+(?:hồ|nghiệp|đội|chí|bào)\b", text[match.end():]):
            continue
        kind, scale = aliases[unit]
        number = _value(match["number"], language)
        if language == "zh" and kind == "count" and unit in ("个", "個") and number == 1:
            before, after = text[:match.start()], text[match.end():]
            quantified = re.search(r"(?:只|仅|僅|每|拿|放|用|买|買|选|選|需要|剩|加|装|裝)$", before)
            if not quantified and not after.startswith(("半", "不", "都", "也")):
                continue  # 一个 + noun often introduces an item without asserting an exact count.
        if number is not None:
            found.add((kind, number * scale))
    if language == "zh":
        for raw in _ZH_PERCENT.findall(text):
            number = _value(raw, language)
            if number is not None:
                found.add(("percent", number))
    return found


def _bare_numbers(text):
    excluded = [match.span() for pattern in (_ZH_MEASURE, _ZH_PERCENT, _SOCIAL_REPLY)
                for match in pattern.finditer(text)]
    return {_value(match.group(), "zh") for match in _BARE_ARABIC.finditer(text)
            if not any(start <= match.start() < end for start, end in excluded)} - {None}


_SHAPES = (
    (r"正方形|方块形", r"\bvuông\b"),
    (r"长方形|長方形|矩形", r"\bchữ nhật\b"),
    (r"(?<![椭橢])[圆圓]形", r"\btròn\b"),
    (r"椭圆形|橢圓形", r"\b(?:bầu dục|ô van|oval|elip)\b"),
    (r"单面|單面|单侧|單側", r"\b(?:một|1)\s+(?:mặt|bên|phía)\b"),
    (r"双面|雙面|两面|兩面|两侧|兩側", r"\b(?:cả\s+)?(?:hai|2)\s+(?:mặt|bên|phía)\b"),
)
_ZH_NEGATION = re.compile(r"不要|不得|不能|不可|无需|無需|无法|無法|沒有|没有|"
                          # Keep these additions scoped to demonstrated polarity-bearing phrases.
                          r"(?:尚未|未)(?:打开盖子?|打開蓋子?|开盖|開蓋)|无(?:糖|蔗糖|添加糖)|"
                          r"不(?:打开盖子?|打開蓋子?|盖|蓋|用|需要|装|裝|放|开|開|沾|粘|建议|建議|含|会|會|加|必|接触|接觸|涂|留)|"
                          r"没(?:盖|装|放|用)|沒(?:蓋|裝|放|用)|别(?:盖|用|放|装|开|碰)|別(?:蓋|用|放|裝|開|碰)")
_VI_NEGATION = re.compile(r"\b(?:không(?!\s+phẩy\b)|chưa|chẳng|đừng|khỏi|tránh|cấm)\b")
# 要是 can mean “if”, but inside 主要是 it is part of “mainly”.
_ZH_CONDITION = re.compile(
    r"(?:如果|假如|若是|(?<![主次重])要是|只要)([^，。；！？,;!?]+?)(?:的[话話]|[，。；！？,;!?]|就|$)"
)
_ZH_IMPLICIT_CONDITION = re.compile(r"(?:^|[，,])([^，。；！？,;!?]+?)的[话話]")
_ZH_CONDITIONAL_RESULT = re.compile(r"^[\s，,]*(?:就|则|則|才|请|請|务必|務必|切勿|不要|别|別)")
_VI_CONDITION = re.compile(r"\b(?:nếu|miễn là|chỉ cần|trường hợp)\s+([^,.;!?]+?)\s+thì\b|"
                           r"\b(?:nếu|miễn là|chỉ cần|trường hợp)\s+([^,.;!?]+)|(?:^|[,;])([^,.;!?]+?)\s+thì\b")


def _clause(pattern, text):
    match = pattern.search(text)
    return next((part.strip() for part in match.groups() if part), "") if match else ""


_CONDITION_ACTIONS = (
    (r"盖(?:上|好|住|盖子)|蓋(?:上|好|住|蓋子)", r"\b(?:đậy|đóng|che|phủ)\b"),
    (r"开盖|開蓋|打开盖|打開蓋", r"\bmở\b"),
)

def _chinese_condition(text):
    explicit = _clause(_ZH_CONDITION, text)
    if explicit:
        return explicit
    for match in _ZH_IMPLICIT_CONDITION.finditer(text):
        clause = match[1].strip()
        # 的话 also introduces a topic. An unpunctuated ASR cue can include an
        # unrelated negative statement before that topic.
        if _ZH_CONDITIONAL_RESULT.match(text[match.end():]) or any(
            re.search(action, clause) for action, _ in _CONDITION_ACTIONS
        ):
            return clause
    return ""


_ZH_AFFIRMATIVE_ACTIONS = (
    (r"打开盖子?|打開蓋子?|开盖|開蓋", r"\b(?:không|chẳng|đừng)\s+mở\b"),
    (r"盖上盖子?|蓋上蓋子?|盖好盖子?|蓋好蓋子?", r"\b(?:không|chẳng|đừng)\s+(?:đậy|đóng|che|phủ)\b"),
)


def translation_meaning_issues(source, target):
    """Report concrete mismatches; send uncertain language to contextual review."""
    source = unicodedata.normalize("NFKC", source).casefold()
    target = unicodedata.normalize("NFKC", target).casefold()
    source = re.sub(r"[\u2010\u2011]", "-", source)
    target = re.sub(r"[\u2010\u2011]", "-", target)
    chinese = bool(_ZH_IS_TEXT.search(source))
    expected = _facts(source, "zh" if chinese else "vi")
    actual = _facts(target, "vi")
    issues = []
    expected_kinds = {kind for kind, _ in expected}
    quantities = {item for item in actual if item[0] in expected_kinds}
    if different := expected ^ quantities:
        kinds = {kind for kind, _ in different}
        issues.append("sai số lượng, số đo hoặc đơn vị" if kinds - {"CNY", "VND", "USD"} else "sai số tiền hoặc đơn vị tiền")
    if chinese and _MODEL_OR_SIZE.search(source) and (numbers := _bare_numbers(source)):
        target_numbers = {_value(match.group(), "vi") for match in _BARE_ARABIC.finditer(target)}
        if numbers - target_numbers - {value for _, value in actual}:
            issues.append("đổi hoặc bỏ số hiệu/số cỡ")

    for zh, vi in _SHAPES:
        if re.search(zh if chinese else vi, source) and not re.search(vi, target):
            issues.append("đổi hoặc bỏ hình dạng/số mặt")
            break

    source_negation = _ZH_NEGATION if chinese else _VI_NEGATION
    condition = _chinese_condition(source) if chinese else _clause(_VI_CONDITION, source)
    if condition:
        translated_condition = _clause(_VI_CONDITION, target)
        changed_action = any(re.search(zh if chinese else vi, condition) and not re.search(vi, translated_condition)
                             for zh, vi in _CONDITION_ACTIONS)
        if (not translated_condition or changed_action
                or bool(source_negation.search(condition)) != bool(_VI_NEGATION.search(translated_condition))):
            issues.append("đổi hoặc bỏ điều kiện")
    source_has_negation = bool(source_negation.search(source))
    target_has_negation = bool(_VI_NEGATION.search(target))
    if source_has_negation and not target_has_negation:
        issues.append("bỏ phủ định hoặc lời cấm")
    elif chinese and target_has_negation and any(
        re.search(zh_action, source) and re.search(vi_negated_action, target)
        for zh_action, vi_negated_action in _ZH_AFFIRMATIVE_ACTIONS
    ):
        issues.append("đảo ngược phủ định hoặc lời cấm")
    return issues


_SPEECH_INTEGRITY_ISSUES = frozenset({
    "sai số lượng, số đo hoặc đơn vị",
    "sai số tiền hoặc đơn vị tiền",
    "đổi hoặc bỏ số hiệu/số cỡ",
    "bỏ phủ định hoặc lời cấm",
    "đảo ngược phủ định hoặc lời cấm",
})


def translation_speech_integrity_failures(segments):
    """Return hard number/negation mismatches for translated cues with source text."""
    failures = []
    for position, segment in enumerate(segments, start=1):
        source = getattr(segment, "orig_content", None)
        target = str(getattr(segment, "content", "") or "").strip()
        if not isinstance(source, str) or not source.strip() or not target or _ZH_IS_TEXT.search(target):
            continue
        cue_id = getattr(segment, "index", position)
        for issue in translation_meaning_issues(source, target):
            if issue in _SPEECH_INTEGRITY_ISSUES:
                failures.append(f"cue {cue_id}: {issue}")
    return failures


def ensure_translation_speech_integrity(segments):
    failures = translation_speech_integrity_failures(segments)
    if failures:
        raise RuntimeError("Translation fact mismatch; refusing TTS: " + "; ".join(failures))
