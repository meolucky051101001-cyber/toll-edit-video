#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phân tích khảo sát hành vi và đánh giá thương hiệu F&B (Phê La - Katinat - Highlands).

Script đọc file Excel trả lời khảo sát 7 trang, làm sạch dữ liệu, tính các bảng
thống kê mô tả, xuất Excel tổng hợp và biểu đồ PNG.

Cách dùng:
    python tools/phan_tich_khao_sat.py
    python tools/phan_tich_khao_sat.py --input "D:\\duong\\dan\\file.xlsx"

Lưu ý: không xuất email/ thông tin định danh của người trả lời ra bất kỳ file nào.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------
DEFAULT_INPUT = Path(r"D:\Khao sat NCM - Ban chinh thuc (7 trang) (Câu trả lời).xlsx")
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "khao_sat_ncm"
CHART_DIR = ROOT / "docs" / "assets" / "khao_sat_ncm"
REPORT_PATH = ROOT / "docs" / "PHAN_TICH_KHAO_SAT_NCM.md"
EXCEL_PATH = OUT_DIR / "PHAN_TICH_KHAO_SAT_NCM.xlsx"

BRANDS = ["Phê La", "Katinat", "Highlands Coffee"]
BRAND_COLORS = {
    "Phê La": "#2E7D5B",
    "Katinat": "#2F5D9E",
    "Highlands Coffee": "#C8102E",
}
DIM_ORDER = [
    "Chất lượng đồ uống",
    "Giá cả",
    "Khuyến mãi",
    "Không gian & vệ sinh",
    "Nhân viên & hỗ trợ",
    "Thương hiệu & hình ảnh",
    "Vị trí & vận hành",
    "Kênh trực tuyến",
    "Đặt hàng & giao nhận",
]

# Khối cột (0-based) của 7 trang đánh giá
BLOCKS = {
    1: (35, 109),
    2: (110, 178),
    3: (179, 250),
    4: (251, 394),
    5: (395, 541),
    6: (542, 682),
    7: (683, 898),
}
# Mỗi trang gồm những bảng nào, theo đúng thứ tự cột
LAYOUT = {
    1: [("Chung", i) for i in range(25)],
    2: [("Online", i) for i in range(23)],
    3: [("Offline", i) for i in range(24)],
    4: [("Chung", i) for i in range(25)] + [("Online", i) for i in range(23)],
    5: [("Chung", i) for i in range(25)] + [("Offline", i) for i in range(24)],
    6: [("Online", i) for i in range(23)] + [("Offline", i) for i in range(24)],
    7: [("Chung", i) for i in range(25)]
    + [("Online", i) for i in range(23)]
    + [("Offline", i) for i in range(24)],
}
PREFIX = {"Chung": "G", "Online": "O", "Offline": "F"}

# Nhóm tiêu chí do người phân tích gán (không phải thang đo chính thức của khảo sát)
DIM = {
    "G01": "Chất lượng đồ uống", "G02": "Chất lượng đồ uống",
    "G03": "Chất lượng đồ uống", "G04": "Chất lượng đồ uống",
    "G05": "Đặt hàng & giao nhận", "G06": "Đặt hàng & giao nhận",
    "G07": "Đặt hàng & giao nhận", "G08": "Đặt hàng & giao nhận",
    "G09": "Thương hiệu & hình ảnh", "G10": "Không gian & vệ sinh",
    "G11": "Thương hiệu & hình ảnh",
    "G12": "Giá cả", "G13": "Giá cả", "G14": "Giá cả",
    "G15": "Giá cả", "G16": "Giá cả",
    "G17": "Thương hiệu & hình ảnh", "G18": "Thương hiệu & hình ảnh",
    "G19": "Thương hiệu & hình ảnh", "G20": "Kênh trực tuyến",
    "G21": "Thương hiệu & hình ảnh", "G22": "Thương hiệu & hình ảnh",
    "G23": "Thương hiệu & hình ảnh", "G24": "Đặt hàng & giao nhận",
    "G25": "Đặt hàng & giao nhận",
    "O01": "Đặt hàng & giao nhận", "O02": "Đặt hàng & giao nhận",
    "O03": "Đặt hàng & giao nhận", "O04": "Đặt hàng & giao nhận",
    "O05": "Đặt hàng & giao nhận", "O06": "Đặt hàng & giao nhận",
    "O07": "Nhân viên & hỗ trợ", "O08": "Kênh trực tuyến",
    "O09": "Kênh trực tuyến", "O10": "Kênh trực tuyến",
    "O11": "Giá cả", "O12": "Giá cả", "O13": "Giá cả", "O14": "Giá cả",
    "O15": "Thương hiệu & hình ảnh", "O16": "Khuyến mãi",
    "O17": "Thương hiệu & hình ảnh", "O18": "Kênh trực tuyến",
    "O19": "Thương hiệu & hình ảnh", "O20": "Kênh trực tuyến",
    "O21": "Khuyến mãi", "O22": "Đặt hàng & giao nhận",
    "O23": "Đặt hàng & giao nhận",
    "F01": "Chất lượng đồ uống", "F02": "Chất lượng đồ uống",
    "F03": "Chất lượng đồ uống",
    "F04": "Nhân viên & hỗ trợ", "F05": "Nhân viên & hỗ trợ",
    "F06": "Nhân viên & hỗ trợ", "F07": "Nhân viên & hỗ trợ",
    "F08": "Không gian & vệ sinh", "F09": "Không gian & vệ sinh",
    "F10": "Không gian & vệ sinh",
    "F11": "Giá cả", "F12": "Giá cả", "F13": "Giá cả", "F14": "Giá cả",
    "F15": "Thương hiệu & hình ảnh", "F16": "Thương hiệu & hình ảnh",
    "F17": "Thương hiệu & hình ảnh", "F18": "Thương hiệu & hình ảnh",
    "F19": "Thương hiệu & hình ảnh", "F20": "Thương hiệu & hình ảnh",
    "F21": "Vị trí & vận hành", "F22": "Vị trí & vận hành",
    "F23": "Vị trí & vận hành", "F24": "Vị trí & vận hành",
}

# Nhãn ngắn cho các tiêu chí xuất hiện trong phần điểm mạnh / điểm yếu
SHORT_CRIT = {
    "G01": "Hương vị phù hợp",
    "G02": "Chất lượng tổng thể",
    "G03": "Đáp ứng kỳ vọng",
    "G04": "Chất lượng ổn định",
    "G05": "Thực hiện đúng món, đủ số lượng",
    "G06": "Đáp ứng yêu cầu tùy chỉnh",
    "G07": "Thông báo tình trạng món kịp thời",
    "G08": "Quy trình bàn giao chỉn chu",
    "G09": "Thiết kế nhận diện",
    "G10": "Không gian sạch sẽ",
    "G11": "Làm mới hình ảnh",
    "G12": "Giá trong khả năng chi trả",
    "G13": "Giá hợp lý so với thị trường",
    "G14": "Giá minh bạch",
    "G15": "Giá tương xứng chất lượng",
    "G16": "Giá trên sàn TMĐT nhất quán",
    "G17": "Cập nhật sản phẩm mới",
    "G18": "Hình ảnh hiện đại",
    "G19": "Truyền thông sáng tạo",
    "G20": "Giao diện online đẹp",
    "G21": "Góc check-in theo trend",
    "G22": "Bắt kịp xu hướng",
    "G23": "Lựa chọn hàng đầu",
    "G24": "Gọi món/thanh toán suôn sẻ",
    "G25": "Đa dạng phương thức thanh toán",
    "O01": "Giữ nhiệt độ khi giao",
    "O02": "Hương vị khi giao",
    "O03": "Đóng gói chắc chắn",
    "O04": "Xác nhận đơn rõ ràng",
    "O05": "Cung cấp dụng cụ đầy đủ",
    "O06": "Niêm phong cẩn thận",
    "O07": "Hỗ trợ khách online",
    "O08": "Giao diện app dễ dùng",
    "O09": "Thông tin món rõ ràng",
    "O10": "Menu cập nhật",
    "O11": "Giá trong khả năng chi trả (online)",
    "O12": "Giá hợp lý (online)",
    "O13": "Giá tương xứng chất lượng (online)",
    "O14": "Giá minh bạch (online)",
    "O15": "Cập nhật sản phẩm mới (online)",
    "O16": "Khuyến mãi hấp dẫn",
    "O17": "Hình ảnh hiện đại (online)",
    "O18": "Giao diện app đẹp",
    "O19": "Bắt kịp xu hướng (online)",
    "O20": "Trải nghiệm đặt hàng mượt",
    "O21": "Dễ tìm khuyến mãi",
    "O22": "Thời gian giao nhanh",
    "O23": "Đóng gói khi giao",
    "F01": "Đồ uống tươi mới tại bàn",
    "F02": "Đúng nhiệt độ tại quán",
    "F03": "Trang trí ly/cốc đẹp",
    "F04": "Nhân viên chuyên nghiệp",
    "F05": "Nhân viên thân thiện",
    "F06": "Nhân viên nắm thực đơn",
    "F07": "Hỗ trợ khách tại quán",
    "F08": "Thiết kế cửa hàng",
    "F09": "Không gian rộng rãi",
    "F10": "Ánh sáng phù hợp",
    "F11": "Giá trong khả năng chi trả (offline)",
    "F12": "Giá hợp lý (offline)",
    "F13": "Giá minh bạch (offline)",
    "F14": "Giá tương xứng chất lượng (offline)",
    "F15": "Cập nhật sản phẩm mới (offline)",
    "F16": "Hình ảnh hiện đại (offline)",
    "F17": "Truyền thông sáng tạo (offline)",
    "F18": "Góc check-in theo trend (offline)",
    "F19": "Bắt kịp xu hướng (offline)",
    "F20": "Lựa chọn cho dịp quan trọng",
    "F21": "Vị trí thuận tiện",
    "F22": "Bãi đỗ xe thuận tiện",
    "F23": "Hàng chờ hợp lý",
    "F24": "Thời gian chờ ngắn",
}

# Danh sách lựa chọn của các câu hỏi nhiều lựa chọn (đã kiểm tra từ dữ liệu gốc)
MULTI_OPTIONS = {
    "purpose": [
        ("Học tập, làm việc cá nhân, chạy deadline", "Học tập / làm việc / deadline"),
        ("Gặp gỡ, trò chuyện, kết nối bạn bè/người thân", "Gặp gỡ, kết nối"),
        ("Thưởng thức hương vị món nước đặc trưng yêu thích", "Thưởng thức hương vị"),
        ("Trải nghiệm không gian đẹp, chụp ảnh check-in sống ảo", "Không gian đẹp / check-in"),
        ("Nhanh chóng, tiện lợi phục vụ nhu cầu giải khát/nạp năng lượng tức thì", "Nhanh chóng / tiện lợi"),
    ],
    "drink": [
        ("Trà đậm vị / Trà sữa đặc trưng (Trà Ô long, Ô long nhài sữa, trà sữa nướng,...)", "Trà đậm vị / trà sữa"),
        ("Đồ uống đá xay / Freeze", "Đá xay / Freeze"),
        ("Cà phê pha máy hiện đại (Espresso, Americano, Latte, Cappuccino)", "Cà phê pha máy"),
        ("Cà phê truyền thống Việt Nam (Cà phê phin, đen đá, nâu đá, bạc xỉu)", "Cà phê truyền thống"),
        ("Trà hoa quả nhiệt đới, nước ép thanh mát", "Trà hoa quả / nước ép"),
    ],
    "discovery": [
        ("Video review, xu hướng trên TikTok / Facebook Reels (KOCs, Food Reviewers)", "Video review / TikTok"),
        ("Lời giới thiệu từ bạn bè, người quen", "Bạn bè / người quen giới thiệu"),
        ("Vị trí cửa hàng nổi bật, bắt mắt khi đi trên đường", "Vị trí cửa hàng nổi bật"),
        ("Các chương trình ưu đãi, mã giảm giá trên ứng dụng đặt đồ ăn", "Ưu đãi / mã giảm giá"),
        ("Bài đăng từ Fanpage/Instagram chính thức của thương hiệu", "Fanpage / Instagram"),
    ],
    "decision": [
        ("Hương vị đồ uống thơm ngon, đậm đà đặc trưng", "Hương vị thơm ngon"),
        ("Mức giá hợp lý, tương xứng với chất lượng và nhiều ưu đãi", "Giá hợp lý / nhiều ưu đãi"),
        ("Không gian thiết kế đẹp, có tính thẩm mỹ và hợp xu hướng chụp ảnh", "Không gian đẹp / chụp ảnh"),
        ("Tiện nghi cửa hàng tốt (bàn ghế thoải mái, nhiều ổ điện, wifi ổn định phục vụ ngồi lâu)", "Tiện nghi ngồi lâu"),
        ("Tác phong phục vụ nhanh chóng, thái độ nhân viên thân thiện, niềm nở", "Phục vụ nhanh / thân thiện"),
        ("Đóng gói cẩn thận, đảm bảo nguyên vẹn khi mang về hoặc giao hàng", "Đóng gói cẩn thận"),
        ("Thương hiệu hợp thời, bắt kịp xu hướng giới trẻ (bao bì đẹp, thức uống viral)", "Hợp thời / bắt trend"),
        ("Vị trí cửa hàng thuận tiện, dễ tìm kiếm, chỗ gửi xe thuận lợi hoặc dễ đặt trên ứng dụng", "Vị trí / gửi xe thuận tiện"),
    ],
}


# ---------------------------------------------------------------------------
# Đọc và làm sạch dữ liệu
# ---------------------------------------------------------------------------
def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def load_survey(path: Path):
    """Đọc sheet trả lời, xác định 32 phiếu hợp lệ và trang đánh giá của từng phiếu."""
    raw = pd.read_excel(path, sheet_name=0, header=0)
    counts = np.array(
        [
            [raw.iloc[i, a:b + 1].notna().sum() for (a, b) in BLOCKS.values()]
            for i in range(len(raw))
        ]
    )
    has_rating = counts.max(axis=1) > 0
    page = counts.argmax(axis=1) + 1
    df = raw[has_rating].reset_index(drop=True).copy()
    df["_page"] = page[has_rating]
    return raw, df


def parse_header(h: str):
    s = str(h).strip()
    m = re.search(r"\s*\[(Phê La|Katinat|Highlands Coffee)\]\s*\d*\s*$", s)
    if not m:
        return None, None
    return re.sub(r"\s*\[(Phê La|Katinat|Highlands Coffee)\]\s*\d*\s*$", "", s).strip(), m.group(1)


def norm_brand(b: str | None) -> str | None:
    if b is None:
        return None
    if b.startswith("Phê La"):
        return "Phê La"
    if b.startswith("Katinat"):
        return "Katinat"
    if b.startswith("Highlands"):
        return "Highlands Coffee"
    return None


CIRCLED = {"⓪": 0, "①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5}


def recode_score(v) -> float:
    if pd.isna(v):
        return np.nan
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return np.nan
    if s[0] in CIRCLED:
        return float(CIRCLED[s[0]])
    if re.fullmatch(r"[0-5](\.0+)?", s):
        return float(s)
    return np.nan


def build_tidy(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """Dựng bảng dài: mỗi dòng = 1 phiếu × 1 tiêu chí × 1 thương hiệu."""
    canon = {"Chung": [], "Online": [], "Offline": []}
    a7, _ = BLOCKS[7]
    for g in range(72):
        ctx = "Chung" if g < 25 else ("Online" if g < 48 else "Offline")
        canon[ctx].append(parse_header(cols[a7 + 3 * g])[0])
    keys = {
        ctx: [f"{PREFIX[ctx]}{i + 1:02d}" for i in range(len(canon[ctx]))]
        for ctx in canon
    }

    records = []
    for page, layout in LAYOUT.items():
        sub = df[df["_page"] == page]
        a, b = BLOCKS[page]
        n_groups = (b - a + 1) // 3
        assert n_groups == len(layout), (page, n_groups, len(layout))
        for g, (ctx, idx) in enumerate(layout):
            trio = cols[a + 3 * g:a + 3 * g + 3]
            parsed = [parse_header(h) for h in trio]
            brands = [norm_brand(x[1]) for x in parsed]
            if any(x is None for x in brands) or len(set(brands)) != 3:
                raise ValueError(f"Không nhận diện được thương hiệu: page={page}, group={g}, {trio}")
            for k, brand in enumerate(brands):
                for ridx, row in sub.iterrows():
                    records.append(
                        {
                            "respondent": ridx,
                            "page": page,
                            "ctx": ctx,
                            "key": keys[ctx][idx],
                            "criterion": canon[ctx][idx],
                            "brand": brand,
                            "score": recode_score(row.iloc[a + 3 * g + k]),
                        }
                    )
    tidy = pd.DataFrame(records)
    tidy["brand"] = tidy["brand"].replace({"Highlands": "Highlands Coffee"})
    tidy["dimension"] = tidy["key"].map(DIM)
    assert tidy["dimension"].notna().all()
    tidy["valid"] = tidy["score"].where(tidy["score"] > 0)
    return tidy


def quality_flags(tidy: pd.DataFrame) -> tuple[list[int], list[int]]:
    stats = tidy.groupby("respondent")["valid"].agg(["count", "std"]).fillna(0)
    flags = stats[(stats["count"] >= 5) & (stats["std"] == 0)].index.tolist()
    all_zero = stats[stats["count"] == 0].index.tolist()
    return flags, all_zero


# ---------------------------------------------------------------------------
# Các bảng thống kê
# ---------------------------------------------------------------------------
def count_table(series: pd.Series, order: list[str] | None = None) -> pd.DataFrame:
    s = series.astype(str).str.strip()
    vc = s.value_counts()
    if order:
        idx = [o for o in order if o in vc.index]
        extra = [i for i in vc.index if i not in idx]
        vc = vc.reindex(idx + extra).fillna(0).astype(int)
    out = vc.rename_axis("label").reset_index(name="count")
    out["pct"] = out["count"] / len(s) * 100
    return out


def multi_table(series: pd.Series, options: list[tuple[str, str]]) -> pd.DataFrame:
    s = series.astype(str)
    rows = []
    for full, short in options:
        n = int(s.str.contains(full, regex=False).sum())
        rows.append({"label": short, "count": n, "pct": n / len(s) * 100})
    return pd.DataFrame(rows)


def build_profile(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    n = len(df)
    age = count_table(df.iloc[:, 2], ["Từ 18 đến 21 tuổi", "Từ 22 đến 25 tuổi"])
    gender = count_table(df.iloc[:, 8], ["Nữ", "Nam", "Không muốn tiết lộ"])
    occupation = count_table(
        df.iloc[:, 9],
        ["Sinh viên", "Khác (Thực tập sinh, Freelancer, vừa học vừa làm,...)"],
    )
    income = count_table(
        df.iloc[:, 12],
        [
            "Dưới 3.000.000 VNĐ",
            "Từ 3.000.000 - dưới 5.000.000 VNĐ",
            "Từ 5.000.000 - dưới 8.000.000 VNĐ",
            "Từ 8.000.000 VNĐ trở lên",
        ],
    )
    # Chuẩn hóa nhẹ tên phường/quận để đếm
    loc = df.iloc[:, 3].astype(str).str.strip().str.lower()

    def norm_loc(s: str) -> str:
        if "hai bà trưng" in s or "hbt" in s:
            return "Hai Bà Trưng"
        if "thanh xuân" in s:
            return "Thanh Xuân"
        if "đống đa" in s:
            return "Đống Đa"
        if "bạch mai" in s:
            return "Bạch Mai"
        if "hoàn kiếm" in s:
            return "Hoàn Kiếm"
        if "ngọc hà" in s or "ba đình" in s:
            return "Ba Đình"
        if "cầu giấy" in s or "yên hòa" in s:
            return "Cầu Giấy"
        if "đại mỗ" in s or "nam từ liêm" in s:
            return "Nam Từ Liêm"
        if "gia lâm" in s:
            return "Gia Lâm"
        if "các phường/xã" in s:
            return "Không xác định"
        return s.title()

    location = count_table(loc.map(norm_loc))
    return {
        "n": n,
        "age": age,
        "gender": gender,
        "occupation": occupation,
        "income": income,
        "location": location,
    }


def build_behavior(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    freq = count_table(
        df.iloc[:, 13],
        [
            "Hiếm khi (1 – 3 lần/tháng)",
            "Thỉnh thoảng (4 – 11 lần/tháng)",
            "Thường xuyên (12 – 20 lần/tháng)",
            "Hàng ngày (Trên 20 lần/tháng)",
        ],
    )
    form = count_table(
        df.iloc[:, 14],
        [
            "Thuần Offline: Hầu như chỉ ngồi uống trực tiếp tại quán",
            "Kết hợp linh hoạt: Vừa hay ngồi quán khi có thời gian, vừa thường xuyên đặt qua app khi bận",
            "Thuần Online: Hầu như chỉ đặt giao tận nơi qua ứng dụng (ShopeeFood, Grab,...)",
            "Mua mang đi (Take-away) trực tiếp tại quầy",
        ],
    )
    form["label"] = form["label"].replace(
        {
            "Thuần Offline: Hầu như chỉ ngồi uống trực tiếp tại quán": "Thuần Offline",
            "Kết hợp linh hoạt: Vừa hay ngồi quán khi có thời gian, vừa thường xuyên đặt qua app khi bận": "Kết hợp linh hoạt (cả hai kênh)",
            "Thuần Online: Hầu như chỉ đặt giao tận nơi qua ứng dụng (ShopeeFood, Grab,...)": "Thuần Online",
            "Mua mang đi (Take-away) trực tiếp tại quầy": "Mua mang đi tại quầy",
        }
    )
    spend = count_table(
        df.iloc[:, 18],
        [
            "Dưới 40.000 VNĐ",
            "Từ 40.000 - 60.000 VNĐ",
            "Từ 61.000 - 80.000 VNĐ",
            "Trên 80.000 VNĐ",
        ],
    )
    companion = count_table(
        df.iloc[:, 19],
        ["Một mình", "Bạn bè / Bạn học", "Người yêu", "Gia đình", "Đồng nghiệp"],
    )
    # Thời gian là nhiều lựa chọn
    time_opts = [
        ("Buổi sáng", "Buổi sáng (07:00–11:00)"),
        ("Buổi trưa", "Buổi trưa (12:00–14:00)"),
        ("Buổi chiều", "Buổi chiều (15:00–18:00)"),
        ("Buổi tối", "Buổi tối (19:00–22:00)"),
        ("Đêm muộn", "Đêm muộn (sau 22:00)"),
    ]
    t = df.iloc[:, 20].astype(str)
    time_df = pd.DataFrame(
        [
            {"label": short, "count": int(t.str.contains(full, regex=False).sum())}
            for full, short in time_opts
        ]
    )
    time_df["pct"] = time_df["count"] / len(df) * 100

    purpose = multi_table(df.iloc[:, 21], MULTI_OPTIONS["purpose"])
    drink = multi_table(df.iloc[:, 22], MULTI_OPTIONS["drink"])
    discovery = multi_table(df.iloc[:, 23], MULTI_OPTIONS["discovery"])
    decision = multi_table(df.iloc[:, 24], MULTI_OPTIONS["decision"])
    return {
        "freq": freq,
        "form": form,
        "spend": spend,
        "companion": companion,
        "time": time_df,
        "purpose": purpose,
        "drink": drink,
        "discovery": discovery,
        "decision": decision,
    }


def usage_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for brand, col in zip(BRANDS, [15, 16, 17]):
        s = df.iloc[:, col].astype(str).str.strip()

        def level(x: str) -> int:
            if "Chưa sử dụng" in x:
                return 0
            if "Hiếm khi" in x:
                return 1
            if "Thỉnh thoảng" in x:
                return 2
            if "Thường xuyên" in x:
                return 3
            return -1

        counts = Counter(level(x) for x in s)
        rows.append(
            {
                "Brand": brand,
                "Chưa dùng": counts.get(0, 0),
                "Hiếm khi": counts.get(1, 0),
                "2–3/tháng": counts.get(2, 0),
                "≥4/tháng": counts.get(3, 0),
                "Đã dùng": sum(counts.get(i, 0) for i in (1, 2, 3)),
                "Thường xuyên": counts.get(2, 0) + counts.get(3, 0),
            }
        )
    return pd.DataFrame(rows)


def channel_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for brand, col in zip(BRANDS, [5, 6, 7]):
        s = df.iloc[:, col].astype(str).str.strip()
        tried = s[~s.str.contains("Chưa từng trải nghiệm", regex=False)]
        n = len(tried)
        off = int(tried.str.contains("Thuần Offline", regex=False).sum())
        on = int(tried.str.contains("Thuần Online", regex=False).sum())
        both = int(tried.str.contains("Cả hai kênh", regex=False).sum())
        rows.append(
            {"Brand": brand, "Đã trải nghiệm": n, "Offline": off, "Online": on, "Cả hai": both}
        )
    return pd.DataFrame(rows)


def image_table(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    img = pd.DataFrame(
        {
            "Phê La": pd.to_numeric(df.iloc[:, 26], errors="coerce"),
            "Katinat": pd.to_numeric(df.iloc[:, 29], errors="coerce"),
            "Highlands Coffee": pd.to_numeric(df.iloc[:, 32], errors="coerce"),
        }
    )
    rows = []
    for b in BRANDS:
        s = img[b].dropna()
        dist = " / ".join(str(int((s == i).sum())) for i in range(1, 6))
        rows.append(
            {
                "Brand": b,
                "n": len(s),
                "Mean": s.mean(),
                "Median": s.median(),
                "Top2": (s >= 4).mean() * 100,
                "Phân bố 1/2/3/4/5": dist,
            }
        )
    table = pd.DataFrame(rows)
    paired = img.dropna()
    paired_mean = pd.DataFrame(
        {"Brand": BRANDS, "Mean (cùng người trả lời)": [paired[b].mean() for b in BRANDS]}
    )
    return table, paired_mean


def rating_tables(tidy: pd.DataFrame, flags: list[int]):
    t = tidy[~tidy["respondent"].isin(flags)].dropna(subset=["valid"]).copy()
    rows = []
    for b in BRANDS:
        s = t[t["brand"] == b]["valid"]
        rows.append(
            {
                "Brand": b,
                "n": len(s),
                "Mean": s.mean(),
                "Top2": (s >= 4).mean() * 100,
                "Pct5": (s == 5).mean() * 100,
                "PctLE2": (s <= 2).mean() * 100,
            }
        )
    summary = pd.DataFrame(rows)

    dim_mean = (
        t.groupby(["dimension", "brand"])["valid"].mean().unstack("brand").reindex(DIM_ORDER)[BRANDS]
    )
    dim_n = (
        t.groupby(["dimension", "brand"])["valid"].size().unstack("brand").reindex(DIM_ORDER)[BRANDS]
    )
    dim_long = (
        t.groupby(["dimension", "brand"])["valid"]
        .agg(n="size", Mean="mean")
        .reset_index()
        .rename(columns={"dimension": "Nhóm tiêu chí", "brand": "Brand"})
    )
    dim_long["Nhóm tiêu chí"] = pd.Categorical(
        dim_long["Nhóm tiêu chí"], categories=DIM_ORDER, ordered=True
    )
    dim_long = dim_long.sort_values(["Nhóm tiêu chí", "Brand"])

    t["top2"] = (t["valid"] >= 4).astype(int)
    crit = (
        t.groupby(["key", "ctx", "dimension", "criterion", "brand"])
        .agg(n=("valid", "size"), Mean=("valid", "mean"), Top2=("top2", "mean"))
        .reset_index()
        .rename(columns={"brand": "Brand"})
    )
    crit["Top2"] *= 100
    # Tỷ lệ ⓪ trên toàn bộ lượt (kể cả 0), cùng nhóm 26 phiếu
    t_all = tidy[~tidy["respondent"].isin(flags)].copy()
    zero = (
        t_all.groupby(["key", "brand"])["score"]
        .apply(lambda s: (s == 0).mean() * 100)
        .rename("Pct0")
        .reset_index()
        .rename(columns={"brand": "Brand"})
    )
    crit = crit.merge(zero, on=["key", "Brand"], how="left")
    crit["Tiêu chí ngắn"] = crit["key"].map(SHORT_CRIT).fillna(
        crit["criterion"].str.slice(0, 60)
    )

    top_bottom = {}
    for b in BRANDS:
        s = crit[(crit["Brand"] == b) & (crit["n"] >= 8)].sort_values("Mean", ascending=False)
        top_bottom[b] = {
            "top": s.head(5)[["key", "Tiêu chí ngắn", "Mean", "n"]].to_dict("records"),
            "bottom": s.tail(5).sort_values("Mean")[["key", "Tiêu chí ngắn", "Mean", "n"]].to_dict(
                "records"
            ),
        }
    return t, summary, dim_mean, dim_n, dim_long, crit, top_bottom


def pair_table(t: pd.DataFrame) -> pd.DataFrame:
    p = t.pivot_table(
        index=["respondent", "key"], columns="brand", values="valid", aggfunc="first"
    )
    rows = []
    for a, b in [("Phê La", "Katinat"), ("Phê La", "Highlands Coffee"), ("Katinat", "Highlands Coffee")]:
        d = p[[a, b]].dropna()
        wins = int((d[a] > d[b]).sum())
        losses = int((d[a] < d[b]).sum())
        ties = int((d[a] == d[b]).sum())
        rows.append(
            {
                "Cặp": f"{a} vs {b}",
                "A": a,
                "B": b,
                "Thắng": wins,
                "Hòa": ties,
                "Thua": losses,
                "n": len(d),
                "Tỷ lệ thắng": wins / (wins + losses) * 100 if wins + losses else np.nan,
                "Chênh lệch TB": (d[a] - d[b]).mean(),
            }
        )
    return pd.DataFrame(rows)


def selected_table(t: pd.DataFrame) -> pd.DataFrame:
    selected = [
        ("G01", "Hương vị phù hợp"),
        ("G12", "Giá trong khả năng chi trả"),
        ("G13", "Giá hợp lý so với thị trường"),
        ("G09", "Thiết kế nhận diện"),
        ("F18", "Góc check-in theo trend"),
        ("F03", "Trang trí ly/cốc"),
        ("F05", "Nhân viên thân thiện"),
        ("F21", "Vị trí thuận tiện"),
        ("G24", "Gọi món/thanh toán"),
    ]
    rows = []
    for key, label in selected:
        s = t[t["key"] == key]
        row = {"key": key, "label": label}
        for b in BRANDS:
            vals = s[s["brand"] == b]["valid"]
            row[b] = vals.mean() if len(vals) else np.nan
            row[f"n_{b}"] = len(vals)
        rows.append(row)
    return pd.DataFrame(rows)


def qualitative_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        (27, "Phê La", "Mục đích sử dụng"),
        (28, "Phê La", "Ba tính từ"),
        (30, "Katinat", "Mục đích sử dụng"),
        (31, "Katinat", "Ba tính từ"),
        (33, "Highlands Coffee", "Mục đích sử dụng"),
        (34, "Highlands Coffee", "Ba tính từ"),
    ]
    rows = []
    for c, brand, question in cols:
        for v in df.iloc[:, c].dropna():
            s = str(v).strip()
            if not s or s.lower() in {"nan", "ko bt", "không biết", "khong biet"}:
                continue
            rows.append({"Brand": brand, "Câu hỏi": question, "Trả lời": s})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Định dạng bảng Markdown
# ---------------------------------------------------------------------------
def md_table(headers: list[str], rows: list[list[str]]) -> str:
    def clean(x):
        return str(x).replace("|", "/").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(clean(x) for x in r) + " |")
    return "\n".join(out)


def fmt_pct(n: int | float, d: int | float) -> str:
    return f"{n / d * 100:.1f}%"


def fmt_mean(x: float) -> str:
    return f"{x:.2f}"


# ---------------------------------------------------------------------------
# Xuất Excel
# ---------------------------------------------------------------------------
def profile_long(profile: dict) -> pd.DataFrame:
    rows = []
    for group, key in [
        ("Độ tuổi", "age"),
        ("Giới tính", "gender"),
        ("Nghề nghiệp", "occupation"),
        ("Thu nhập hàng tháng", "income"),
        ("Khu vực sinh sống/học tập", "location"),
    ]:
        for _, r in profile[key].iterrows():
            rows.append([group, r["label"], int(r["count"]), round(float(r["pct"]), 1)])
    return pd.DataFrame(rows, columns=["Nhóm", "Lựa chọn", "Số lượng", "Tỷ lệ (%)"])


def behavior_long(behavior: dict) -> pd.DataFrame:
    rows = []
    for group, key in [
        ("Tần suất dùng đồ uống chuỗi", "freq"),
        ("Hình thức tiêu dùng", "form"),
        ("Chi tiêu mỗi lần", "spend"),
        ("Đi cùng ai", "companion"),
        ("Thời gian trong ngày", "time"),
    ]:
        for _, r in behavior[key].iterrows():
            rows.append([group, r["label"], int(r["count"]), round(float(r["pct"]), 1)])
    for group, key in [
        ("Mục đích sử dụng (nhiều lựa chọn)", "purpose"),
        ("Nhóm đồ uống thường chọn (nhiều lựa chọn)", "drink"),
        ("Kênh thông tin ảnh hưởng (nhiều lựa chọn)", "discovery"),
        ("Yếu tố quyết định (nhiều lựa chọn)", "decision"),
    ]:
        for _, r in behavior[key].iterrows():
            rows.append([group, r["label"], int(r["count"]), round(float(r["pct"]), 1)])
    return pd.DataFrame(rows, columns=["Nhóm", "Lựa chọn", "Số lượng", "Tỷ lệ (%)"])


def write_excel(
    path: Path,
    profile: dict,
    behavior: dict,
    usage: pd.DataFrame,
    channel: pd.DataFrame,
    summary: pd.DataFrame,
    image: pd.DataFrame,
    image_paired: pd.DataFrame,
    dim_long: pd.DataFrame,
    crit: pd.DataFrame,
    pairs: pd.DataFrame,
    qual: pd.DataFrame,
    tidy: pd.DataFrame,
    flags: list[int],
    all_zero: list[int],
    n_rating: int,
) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = [
        ["Nguồn dữ liệu", "File Excel trả lời khảo sát 7 trang (Google Forms export)."],
        ["Thời gian khảo sát", "20/10/2026 18:47 – 21/10/2026 14:22."],
        ["Số phản hồi", f"{tidy['respondent'].nunique()} phiếu hợp lệ (file có 33 dòng, 1 dòng không có đánh giá)."],
        ["Phiếu dùng cho phân tích điểm", f"{n_rating} phiếu có ít nhất một điểm 1–5 và có biến thiên."],
        ["Ngoại lệ đã loại", f"{len(flags)} phiếu đánh giá cùng một mức điểm (3 phiếu toàn 5, 1 phiếu toàn 3); {len(all_zero)} phiếu toàn ⓪."],
        ["Email trùng", "31 email duy nhất cho 32 phiếu; 1 email xuất hiện 2 lần với câu trả lời khác nhau, giữ cả hai."],
        ["Thang điểm", "①–⑤ = 1–5; ⓪ = Chưa trải nghiệm / Không áp dụng, loại khỏi trung bình."],
        ["Cấu trúc 7 trang", "Mỗi phiếu chỉ trả lời 1 trang: 25 tiêu chí chung (G), 23 tiêu chí online (O), 24 tiêu chí offline (F), hoặc tổ hợp. Trang 2 không có phiếu nào."],
        ["Nhóm tiêu chí", "9 nhóm do người phân tích gán từ 72 tiêu chí, không phải thang đo chính thức của khảo sát."],
        ["So sánh cặp", "Chỉ tính các lượt cả 2 thương hiệu đều có điểm 1–5; tỷ lệ thắng loại trường hợp hòa."],
        ["Quyền riêng tư", "Không xuất email, họ tên hay thông tin định danh. Mã phiếu chỉ là chỉ số dòng ẩn danh."],
        ["Cảnh báo", "Mẫu nhỏ (N=32), chọn mẫu thuận tiện; kết quả chỉ mang tính mô tả, không suy rộng cho toàn thị trường."],
    ]
    sheets = {
        "Ho so mau": profile_long(profile),
        "Hanh vi": behavior_long(behavior),
        "Tham nhap": usage,
        "Kenh": channel,
        "Diem tong": summary,
        "Hinh anh": image,
        "Diem theo nhom": dim_long,
        "Chi tiet tieu chi": crit[
            ["key", "ctx", "dimension", "criterion", "Tiêu chí ngắn", "Brand", "n", "Mean", "Top2", "Pct0"]
        ].rename(
            columns={
                "key": "Mã",
                "ctx": "Bảng",
                "dimension": "Nhóm",
                "criterion": "Tiêu chí",
                "Brand": "Thương hiệu",
                "Mean": "Điểm TB",
                "Top2": "% 4–5",
                "Pct0": "% ⓪",
            }
        ),
        "So sanh cap": pairs,
        "Dinh tinh": qual,
        "Du lieu sach": tidy[
            ["respondent", "page", "ctx", "key", "criterion", "brand", "score", "valid", "dimension"]
        ].rename(
            columns={
                "respondent": "Mã phiếu",
                "page": "Trang",
                "ctx": "Bảng",
                "key": "Mã tiêu chí",
                "criterion": "Tiêu chí",
                "brand": "Thương hiệu",
                "score": "Điểm gốc",
                "valid": "Điểm hợp lệ",
                "dimension": "Nhóm",
            }
        ),
        "Hinh anh (cap)": image_paired,
    }

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(doc, columns=["Mục", "Nội dung"]).to_excel(
            writer, sheet_name="Doc", index=False
        )
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
        wb = writer.book
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        thin = Side(style="thin", color="D9E2F3")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.sheet_view.showGridLines = False
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = border
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                    cell.border = border
            for col_idx, cell in enumerate(ws[1], start=1):
                h = str(cell.value)
                fmt = None
                if any(k in h for k in ["%", "Tỷ lệ", "Top2", "Pct"]):
                    fmt = "0.0"
                elif any(k in h for k in ["Mean", "TB", "Chênh lệch", "Trung vị"]):
                    fmt = "0.00"
                elif h in {
                    "n", "Số lượng", "Thắng", "Hòa", "Thua", "Đã dùng",
                    "Thường xuyên", "Chưa dùng", "Hiếm khi", "2–3/tháng",
                    "≥4/tháng", "Đã trải nghiệm", "Offline", "Online", "Cả hai",
                }:
                    fmt = "0"
                if fmt:
                    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                        for c in row:
                            if isinstance(c.value, (int, float)):
                                c.number_format = fmt
            for col in ws.columns:
                values = [str(c.value) for c in col if c.value is not None]
                width = min(max(10, max((len(v) for v in values), default=10) + 2), 55)
                if ws.title == "Doc" and col[0].column == 2:
                    width = 110
                ws.column_dimensions[get_column_letter(col[0].column)].width = width
            if ws.title != "Doc":
                ws.auto_filter.ref = ws.dimensions
    print(f"Đã ghi Excel: {path}")


# ---------------------------------------------------------------------------
# Biểu đồ
# ---------------------------------------------------------------------------
def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    names = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ["Segoe UI", "Arial", "Tahoma", "DejaVu Sans"]:
        if candidate in names:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "white"
    return plt


def _save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170, bbox_inches="tight", facecolor="white")
    import matplotlib.pyplot as plt

    plt.close(fig)


def chart_profile(profile: dict, freq: pd.DataFrame, path: Path) -> None:
    plt = _setup_matplotlib()
    panels = [
        ("Độ tuổi", profile["age"], ["18–21", "22–25"], "#2F5D9E"),
        ("Giới tính", profile["gender"], ["Nữ", "Nam", "Không tiết lộ"], "#2E7D5B"),
        ("Thu nhập hàng tháng", profile["income"], ["<3 triệu", "3–5 triệu", "5–8 triệu"], "#C8102E"),
        (
            "Tần suất dùng đồ uống chuỗi",
            freq,
            ["1–3 lần/tháng", "4–11 lần/tháng", "12–20 lần/tháng", ">20 lần/tháng"],
            "#8A5A2B",
        ),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(11.5, 7.2))
    for ax, (title, frame, labels, color) in zip(axes.ravel(), panels):
        frame = frame.copy().reset_index(drop=True)
        frame["short"] = labels[: len(frame)]
        y = np.arange(len(frame))[::-1]
        ax.barh(y, frame["count"], color=color, alpha=0.88, height=0.6)
        ax.set_yticks(y, frame["short"], fontsize=9)
        ax.set_title(title, fontsize=11, fontweight="bold", loc="left")
        ax.set_xlim(0, max(frame["count"]) * 1.35 + 1)
        for yi, c, p in zip(y, frame["count"], frame["pct"]):
            ax.text(c + 0.4, yi, f"{c} ({p:.1f}%)", va="center", fontsize=9, color="#333333")
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.grid(axis="x", alpha=0.18)
        ax.set_xlabel("Số phiếu", fontsize=8, color="#666666")
    fig.suptitle("Hồ sơ mẫu (N=32)", fontsize=15, fontweight="bold", x=0.02, ha="left", y=0.985)
    fig.text(0.02, 0.925, "Khảo sát tháng 10/2026; mẫu sinh viên tại Hà Nội", fontsize=9, color="#666666")
    fig.tight_layout(rect=[0, 0, 1, 0.90])
    _save(fig, path)


def chart_usage(usage: pd.DataFrame, path: Path) -> None:
    plt = _setup_matplotlib()
    order = ["Highlands Coffee", "Phê La", "Katinat"]
    u = usage.set_index("Brand").loc[order]
    labels = ["Chưa dùng 6 tháng", "Hiếm khi", "2–3 lần/tháng", "≥4 lần/tháng"]
    cols = ["Chưa dùng", "Hiếm khi", "2–3/tháng", "≥4/tháng"]
    colors = ["#D9D9D9", "#A8C5B5", "#5E9C7E", "#1F5C43"]

    total_n = int(u[cols].sum(axis=1).iloc[0])
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    y = np.arange(len(u))
    left = np.zeros(len(u))
    for j, (col, label, color) in enumerate(zip(cols, labels, colors)):
        vals = u[col].values.astype(float)
        ax.barh(y, vals, left=left, color=color, label=label, height=0.55, edgecolor="white", linewidth=1)
        for i, v in enumerate(vals):
            if v >= 3:
                ax.text(
                    left[i] + v / 2, y[i], f"{int(v)}", ha="center", va="center",
                    fontsize=9, color="white" if j >= 2 else "#222222", fontweight="bold",
                )
        left += vals
    for i, (_, row) in enumerate(u.iterrows()):
        pct = row["Đã dùng"] / total_n * 100
        ax.text(33.0, y[i], f"Đã dùng: {int(row['Đã dùng'])} ({pct:.1f}%)",
                va="center", fontsize=9.5, color=BRAND_COLORS[order[i]], fontweight="bold")
    ax.set_yticks(y, order, fontsize=11)
    ax.invert_yaxis()
    ax.set_xlim(0, 42)
    ax.set_xlabel(f"Số phiếu (N={total_n})", fontsize=9, color="#555555")
    ax.set_title("Thâm nhập và tần suất sử dụng trong 6 tháng", fontsize=14, fontweight="bold", loc="left")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", alpha=0.15)
    ax.legend(loc="lower center", bbox_to_anchor=(0.45, -0.32), ncol=4, frameon=False, fontsize=9)
    fig.text(0.01, -0.02, "Nguồn: khảo sát 32 phiếu; tần suất tự báo cáo trong 6 tháng gần nhất.", fontsize=8, color="#777777")
    fig.tight_layout()
    _save(fig, path)


def chart_dimensions(dim_mean: pd.DataFrame, dim_n: pd.DataFrame, path: Path) -> None:
    plt = _setup_matplotlib()
    data = dim_mean.loc[DIM_ORDER, BRANDS].values.astype(float)
    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    im = ax.imshow(data, cmap="YlGnBu", vmin=3.2, vmax=4.2, aspect="auto")
    ax.set_xticks(range(len(BRANDS)), BRANDS, fontsize=10)
    ax.set_yticks(range(len(DIM_ORDER)), DIM_ORDER, fontsize=9.5)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            val = data[i, j]
            color = "white" if val >= 3.82 else "#1A1A1A"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=10.5,
                    fontweight="bold", color=color)
        row_max = np.nanmax(data[i])
        for j in range(data.shape[1]):
            if np.isclose(data[i, j], row_max):
                ax.add_patch(
                    plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False,
                                  edgecolor="#C8102E", lw=2.4, zorder=5)
                )
    cbar = fig.colorbar(im, ax=ax, fraction=0.032, pad=0.02)
    cbar.set_label("Điểm trung bình (1–5)", fontsize=8)
    cbar.ax.tick_params(labelsize=8)
    ax.set_title("Điểm trung bình theo nhóm tiêu chí (N=26)", fontsize=14, fontweight="bold", loc="left", pad=14)
    ax.tick_params(length=0)
    fig.text(0.01, -0.02,
             "Ô viền đỏ = điểm cao nhất mỗi hàng. Thang 1–5; 0 (chưa trải nghiệm) không tính vào trung bình. "
             "Số lượt đánh giá mỗi ô: 9–231.",
             fontsize=8, color="#666666")
    fig.tight_layout()
    fig.subplots_adjust(left=0.29)
    _save(fig, path)


def chart_selected(selected: pd.DataFrame, path: Path) -> None:
    plt = _setup_matplotlib()
    df = selected.set_index("label")
    labels = df.index.tolist()[::-1]
    fig, ax = plt.subplots(figsize=(10.5, 6.0))
    for i, label in enumerate(labels):
        vals = df.loc[label, BRANDS].values.astype(float)
        ax.plot([vals.min(), vals.max()], [i, i], color="#D0D0D0", lw=3.5, zorder=1, solid_capstyle="round")
        for b in BRANDS:
            v = float(df.loc[label, b])
            ax.scatter(v, i, s=120, color=BRAND_COLORS[b], edgecolor="white", linewidth=1.3,
                       zorder=3, label=b if i == len(labels) - 1 else None)
    ax.set_yticks(np.arange(len(labels)), labels, fontsize=9.5)
    ax.set_xlim(2.7, 4.85)
    ax.axvline(3.5, color="#999999", ls="--", lw=1)
    ax.set_xlabel("Điểm trung bình (1–5)", fontsize=9, color="#555555")
    ax.set_title("Các tiêu chí thể hiện rõ khác biệt giữa 3 thương hiệu", fontsize=14,
                 fontweight="bold", loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.2)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=3, frameon=False, fontsize=9)
    fig.tight_layout()
    _save(fig, path)


def chart_pairs(pairs: pd.DataFrame, path: Path) -> None:
    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=(10.5, 3.9))
    y = np.arange(len(pairs))
    for i, row in pairs.iterrows():
        total = row["n"]
        w1 = row["Thắng"] / total * 100
        wt = row["Hòa"] / total * 100
        w2 = row["Thua"] / total * 100
        ax.barh(i, w1, color="#2E7D5B", height=0.55, edgecolor="white")
        ax.barh(i, wt, left=w1, color="#DDDDDD", height=0.55, edgecolor="white")
        ax.barh(i, w2, left=w1 + wt, color="#C8102E", height=0.55, edgecolor="white")
        if w1 >= 7:
            ax.text(w1 / 2, i, f"{row['Thắng']}", ha="center", va="center", color="white",
                    fontsize=9, fontweight="bold")
        if wt >= 7:
            ax.text(w1 + wt / 2, i, f"{row['Hòa']} hòa", ha="center", va="center",
                    color="#333333", fontsize=9, fontweight="bold")
        if w2 >= 7:
            ax.text(w1 + wt + w2 / 2, i, f"{row['Thua']}", ha="center", va="center",
                    color="white", fontsize=9, fontweight="bold")
        ax.text(101, i, f"n={int(total)}", va="center", fontsize=8.5, color="#555555")
    ax.set_yticks(y, pairs["Cặp"].tolist(), fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xlabel("% số lượt so sánh", fontsize=9, color="#555555")
    ax.set_title("So sánh cặp: thắng – hòa – thua", fontsize=14, fontweight="bold", loc="left")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", alpha=0.15)
    from matplotlib.patches import Patch

    handles = [
        Patch(facecolor="#2E7D5B", label="Thương hiệu đứng trước thắng"),
        Patch(facecolor="#DDDDDD", label="Hòa"),
        Patch(facecolor="#C8102E", label="Thương hiệu đứng sau thắng"),
    ]
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.45, -0.42),
              ncol=3, frameon=False, fontsize=8.5)
    fig.text(0.01, -0.03,
             "Chỉ tính các lượt cả 2 thương hiệu đều có điểm 1–5. Tỷ lệ thắng loại hòa: "
             + " | ".join(f"{r['Cặp']}: {r['Tỷ lệ thắng']:.1f}%" for _, r in pairs.iterrows()),
             fontsize=7.8, color="#666666")
    fig.tight_layout()
    _save(fig, path)


def make_charts(
    profile: dict,
    freq: pd.DataFrame,
    usage: pd.DataFrame,
    dim_mean: pd.DataFrame,
    dim_n: pd.DataFrame,
    selected: pd.DataFrame,
    pairs: pd.DataFrame,
    outdir: Path,
) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = [
        outdir / "01_ho_so_mau.png",
        outdir / "02_tham_nhap_tan_suat.png",
        outdir / "03_diem_theo_nhom.png",
        outdir / "04_diem_manh_yeu.png",
        outdir / "05_so_sanh_cap.png",
    ]
    chart_profile(profile, freq, paths[0])
    chart_usage(usage, paths[1])
    chart_dimensions(dim_mean, dim_n, paths[2])
    chart_selected(selected, paths[3])
    chart_pairs(pairs, paths[4])
    print("Đã ghi biểu đồ:", ", ".join(p.name for p in paths))
    return paths


# ---------------------------------------------------------------------------
# Main tạm thời: in bảng để kiểm tra
# ---------------------------------------------------------------------------
def main() -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="Phân tích khảo sát F&B 7 trang")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if not args.input.exists():
        print(f"Không tìm thấy file: {args.input}")
        return 2

    raw, df = load_survey(args.input)
    cols = list(raw.columns)
    tidy = build_tidy(df, cols)
    flags, all_zero = quality_flags(tidy)
    t, summary, dim_mean, dim_n, dim_long, crit, top_bottom = rating_tables(tidy, flags)
    profile = build_profile(df)
    behavior = build_behavior(df)
    usage = usage_table(df)
    channel = channel_table(df)
    image, image_paired = image_table(df)
    pairs = pair_table(t)
    selected = selected_table(t)
    qual = qualitative_table(df)

    print(f"File: {args.input.name}")
    print(f"Tổng dòng dữ liệu: {len(raw)} | phiếu hợp lệ: {len(df)} | email duy nhất: {df.iloc[:, 1].nunique()}")
    print(f"Trang: {df['_page'].value_counts().sort_index().to_dict()}")
    print(f"Straight-lining: {flags} | toàn 0: {all_zero}")
    print(f"Phiếu dùng cho phân tích điểm: {t['respondent'].nunique()}")
    print("\nĐiểm tổng:")
    print(summary.round(2).to_string(index=False))
    print("\nĐiểm theo nhóm:")
    print(dim_mean.round(2).to_string())
    print("\nSo sánh cặp:")
    print(pairs.round(2).to_string(index=False))
    print("\nThâm nhập:")
    print(usage.to_string(index=False))
    print("\nHình ảnh:")
    print(image.round(2).to_string(index=False))
    print("\nĐiểm mạnh/yếu Phê La:")
    print(pd.DataFrame(top_bottom["Phê La"]["top"]).round(2).to_string(index=False))
    print(pd.DataFrame(top_bottom["Phê La"]["bottom"]).round(2).to_string(index=False))
    print("\nĐã tính xong các bảng.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_excel(
        EXCEL_PATH,
        profile,
        behavior,
        usage,
        channel,
        summary,
        image,
        image_paired,
        dim_long,
        crit,
        pairs,
        qual,
        tidy,
        flags,
        all_zero,
        t["respondent"].nunique(),
    )

    make_charts(
        profile,
        behavior["freq"],
        usage,
        dim_mean,
        dim_n,
        selected,
        pairs,
        CHART_DIR,
    )

    if args.debug:
        tidy.to_csv(OUT_DIR / "tidy_ratings.csv", index=False, encoding="utf-8-sig")
        crit.to_csv(OUT_DIR / "criteria_detail.csv", index=False, encoding="utf-8-sig")
        print(f"Đã lưu CSV tạm vào {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
