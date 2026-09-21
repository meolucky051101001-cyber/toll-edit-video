import re

from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.translator import (
    is_cjk,
    plan_query,
)

BENCHMARK_DATASET = [
    # Category 1: Làm đẹp & Chăm sóc da (12 queries)
    {"cat": "beauty", "query": "trị mụn ẩn cho da dầu", "must": ["去闭口", "油皮"], "forbid": ["键盘", "开箱"]},
    {"cat": "beauty", "query": "kem chống nắng nâng tone kiềm dầu", "must": ["防晒"], "forbid": ["做饭"]},
    {"cat": "beauty", "query": "sữa rửa mặt cho da nhạy cảm", "must": ["洗面奶"], "forbid": ["手机"]},
    {"cat": "beauty", "query": "mẹo kẻ mắt cho người mới bắt đầu", "must": ["眼线"], "forbid": ["修车"]},
    {"cat": "beauty", "query": "son môi màu đỏ đất tôn da", "must": ["口红"], "forbid": ["电脑"]},
    {"cat": "beauty", "query": "các bước skincare buổi sáng", "must": ["护肤"], "forbid": ["钓鱼"]},
    {"cat": "beauty", "query": "phấn phủ kiềm dầu chống nước", "must": ["散粉", "粉饼", "控油"], "forbid": ["家具"]},
    {"cat": "beauty", "query": "tẩy trang cho da dầu mụn", "must": ["卸妆"], "forbid": ["汽车"]},
    {"cat": "beauty", "query": "dưỡng ẩm phục hồi da treatment", "must": ["修护", "面霜", "保湿", "护肤"], "forbid": ["游戏"]},
    {"cat": "beauty", "query": "chăm sóc tóc khô xơ chẻ ngọn", "must": ["护发", "头发"], "forbid": ["数码"]},
    {"cat": "beauty", "query": "cọ trang điểm đánh nền tự nhiên", "must": ["化妆刷", "粉底刷", "底妆刷"], "forbid": ["做菜"]},
    {"cat": "beauty", "query": "mút tán kem nền hồ lô", "must": ["美妆蛋", "粉扑"], "forbid": ["服装"]},

    # Category 2: Thời trang & Phối đồ (12 queries)
    {"cat": "fashion", "query": "phối đồ mùa đông phong cách vintage", "must": ["冬", "穿搭", "复古"], "forbid": ["美甲"]},
    {"cat": "fashion", "query": "outfit đi học cho sinh viên nữ", "must": ["穿搭", "学生"], "forbid": ["做饭"]},
    {"cat": "fashion", "query": "phối đồ đi làm công sở thanh lịch", "must": ["穿搭", "通勤"], "forbid": ["游戏"]},
    {"cat": "fashion", "query": "phối đồ mùa hè nữ cá tính", "must": ["夏", "穿搭"], "forbid": ["数码"]},
    {"cat": "fashion", "query": "phối đồ phong cách Y2K học sinh", "must": ["Y2K", "穿搭"], "forbid": ["做菜"]},
    {"cat": "fashion", "query": "cách phối áo len cổ lọ dáng rộng", "must": ["毛衣", "穿搭"], "forbid": ["钓鱼"]},
    {"cat": "fashion", "query": "outfit mùa thu nhẹ nhàng tiểu thư", "must": ["秋", "穿搭"], "forbid": ["键盘"]},
    {"cat": "fashion", "query": "phối đồ cho người lùn chân ngắn", "must": ["穿搭", "显高", "小个子"], "forbid": ["手机"]},
    {"cat": "fashion", "query": "áo blazer nữ phối cùng quần jean", "must": ["西装", "穿搭"], "forbid": ["修车"]},
    {"cat": "fashion", "query": "váy hoa nhí mùa hè vintage", "must": ["碎花", "连衣裙", "夏", "复古"], "forbid": ["美发"]},
    {"cat": "fashion", "query": "phối đồ phong cách retro cổ điển", "must": ["复古", "穿搭"], "forbid": ["护肤"]},
    {"cat": "fashion", "query": "set đồ thể thao năng động mùa đông", "must": ["冬", "运动", "穿搭"], "forbid": ["美甲"]},

    # Category 3: Thủ công & Văn phòng phẩm (12 queries)
    {"cat": "stationery", "query": "nhíp gắp sticker chuyên dụng", "must": ["镊子", "贴纸"], "forbid": ["美睫", "做饭"]},
    {"cat": "stationery", "query": "dụng cụ bóc sticker cute", "must": ["贴纸", "手账"], "forbid": ["衣服"]},
    {"cat": "stationery", "query": "hướng dẫn móc len hoa tulip", "must": ["钩织", "郁金香"], "forbid": ["手机"]},
    {"cat": "stationery", "query": "trang trí sổ tay bullet journal", "must": ["手账"], "forbid": ["化妆品"]},
    {"cat": "stationery", "query": "khui hộp mù blind box pop mart", "must": ["盲盒"], "forbid": ["修车"]},
    {"cat": "stationery", "query": "bút vẽ móng nghệ thuật nail", "must": ["美甲", "笔"], "forbid": ["做菜"]},
    {"cat": "stationery", "query": "giấy gói hoa tươi cao cấp", "must": ["包装纸", "花"], "forbid": ["数码"]},
    {"cat": "stationery", "query": "decor bàn học góc làm việc chill", "must": ["书桌", "布置"], "forbid": ["美妆"]},
    {"cat": "stationery", "query": "dụng cụ làm gốm thủ công tại nhà", "must": ["陶艺", "手作", "手工"], "forbid": ["穿搭"]},
    {"cat": "stationery", "query": "hướng dẫn gấp hoa giấy trang trí", "must": ["折纸", "纸花", "手工"], "forbid": ["健身"]},
    {"cat": "stationery", "query": "móc len thú bông cho người mới bắt đầu", "must": ["钩织", "玩偶", "毛线"], "forbid": ["电脑"]},
    {"cat": "stationery", "query": "băng keo washi tape trang trí sổ", "must": ["胶带", "手账"], "forbid": ["汽车"]},

    # Category 4: Nấu ăn & Pha chế & Làm bánh (12 queries)
    {"cat": "cooking", "query": "cách làm bánh bao kim sa tại nhà", "must": ["包子"], "forbid": ["穿搭"]},
    {"cat": "cooking", "query": "cách làm bánh mì hoa cúc mềm xốp", "must": ["面包"], "forbid": ["美甲"]},
    {"cat": "cooking", "query": "cách pha cà phê muối béo ngậy", "must": ["海盐咖啡", "咖啡"], "forbid": ["文具"]},
    {"cat": "cooking", "query": "cách làm sữa chua dẻo úp ngược", "must": ["酸奶"], "forbid": ["键盘"]},
    {"cat": "cooking", "query": "cách làm sữa hạt dinh dưỡng bằng máy", "must": ["豆浆", "破壁机", "植物奶"], "forbid": ["衣服"]},
    {"cat": "cooking", "query": "công thức làm trà sữa trân châu đường đen", "must": ["奶茶"], "forbid": ["修车"]},
    {"cat": "cooking", "query": "hướng dẫn làm bánh cuốn chảo chống dính", "must": ["肠粉"], "forbid": ["美妆"]},
    {"cat": "cooking", "query": "cách làm bánh xèo miền tây giòn lâu", "must": ["煎饼"], "forbid": ["数码"]},
    {"cat": "cooking", "query": "khuôn làm bánh nướng hình hoa", "must": ["模具", "烘焙", "蛋糕"], "forbid": ["服装"]},
    {"cat": "cooking", "query": "món ngon giảm cân ít calo dễ làm", "must": ["减脂餐", "家常菜", "美食"], "forbid": ["美甲"]},
    {"cat": "cooking", "query": "cách nấu phở bò chuẩn vị truyền thống", "must": ["牛肉粉", "美食", "家常菜"], "forbid": ["电脑"]},
    {"cat": "cooking", "query": "cách ngâm măng ớt giòn ngon không nổi váng", "must": ["腌制", "美食", "家常菜"], "forbid": ["穿搭"]},

    # Category 5: Công nghệ & Thiết bị & Đời sống (12 queries)
    {"cat": "tech_life", "query": "bàn phím cơ giá rẻ cho học sinh", "must": ["机械键盘", "平价", "学生"], "forbid": ["美甲"]},
    {"cat": "tech_life", "query": "chuột không dây bluetooth chống ồn", "must": ["无线鼠标", "鼠标"], "forbid": ["做菜"]},
    {"cat": "tech_life", "query": "tai nghe bluetooth chống ồn chủ động", "must": ["蓝牙耳机", "降噪"], "forbid": ["做饭"]},
    {"cat": "tech_life", "query": "dây sạc nhanh type c đa năng", "must": ["快充", "数据线"], "forbid": ["穿搭"]},
    {"cat": "tech_life", "query": "máy uốn tóc tự xoay ion âm", "must": ["卷发棒"], "forbid": ["数码"]},
    {"cat": "tech_life", "query": "máy sấy tóc tốc độ cao mini", "must": ["吹风机"], "forbid": ["做饭"]},
    {"cat": "tech_life", "query": "hộp đựng mỹ phẩm để bàn trong suốt", "must": ["化妆品", "收纳"], "forbid": ["修车"]},
    {"cat": "tech_life", "query": "bình giữ nhiệt inox hiển thị nhiệt độ", "must": ["保温杯"], "forbid": ["穿搭"]},
    {"cat": "tech_life", "query": "bài tập gym tăng cơ lưng cho nam", "must": ["背肌", "练背", "健身"], "forbid": ["美甲"]},
    {"cat": "tech_life", "query": "bài tập giảm mỡ bụng tại nhà 15 phút", "must": ["腹肌", "减肚子", "核心"], "forbid": ["键盘"]},
    {"cat": "tech_life", "query": "máy quay vlog du lịch nhỏ gọn", "must": ["相机", "vlog"], "forbid": ["做菜"]},
    {"cat": "tech_life", "query": "vlog một ngày đi làm của dân công sở", "must": ["vlog", "上班族", "日常"], "forbid": ["修车"]},
]


def test_dataset_size_and_distribution():
    assert len(BENCHMARK_DATASET) == 60
    categories = set(item["cat"] for item in BENCHMARK_DATASET)
    assert len(categories) == 5
    for cat in categories:
        items = [i for i in BENCHMARK_DATASET if i["cat"] == cat]
        assert len(items) == 12


def test_translation_intent_coverage_and_zero_failure():
    failures = []
    cjk_missing = []

    for item in BENCHMARK_DATASET:
        query = item["query"]
        plan = plan_query(query)

        # 1. Zero crash / zero empty
        if not plan.tiered_queries:
            failures.append((query, "Empty tiered queries"))
            continue

        full_chinese = " ".join(plan.tiered_queries)
        # 2. Chinese CJK presence
        if not is_cjk(full_chinese) and not any(re.search(r"[\u4e00-\u9fff]", q) for q in plan.tiered_queries):
            cjk_missing.append(query)

    assert len(failures) == 0, f"Translation plan failures: {failures}"
    # High translation coverage across dictionary & compound rules
    assert len(cjk_missing) <= 5, f"Queries without CJK: {cjk_missing}"


def test_precision_benchmark_across_categories():
    total_evaluated = 0
    precision_5_hits = 0
    precision_10_hits = 0

    for item in BENCHMARK_DATASET:
        query = item["query"]
        must_tokens = item["must"]
        forbid_tokens = item["forbid"]

        plan = plan_query(query)
        alt_queries = plan.tiered_queries

        # Synthesize realistic candidate pool (10 relevant hits, 5 distractors)
        candidates = []

        # 10 Relevant hits
        must_str = " ".join(must_tokens)
        for i in range(10):
            prefix = alt_queries[i % len(alt_queries)]
            hit_title = f"{prefix} {must_str} 超好用推荐 #{i+1}"
            candidates.append(VideoResult(
                platform="douyin",
                platform_video_id=f"hit-{i}",
                url=f"https://www.douyin.com/video/hit{i}",
                title=hit_title,
                caption=f"Chia sẻ về {query} chất lượng cao",
                like_count=5000 + i * 100,
            ))

        # 5 Distractors
        for j in range(5):
            distractor_title = f"{forbid_tokens[0]} 开箱日常 热门分享 #{j+1}"
            candidates.append(VideoResult(
                platform="douyin",
                platform_video_id=f"dist-{j}",
                url=f"https://www.douyin.com/video/dist{j}",
                title=distractor_title,
                caption=f"Video không liên quan {forbid_tokens[0]}",
                like_count=10000,
            ))

        # Rank candidates
        ranked = []
        for cand in candidates:
            breakdown = rank_detailed(cand, query, alternative_queries=alt_queries)
            ranked.append((cand, breakdown))

        ranked.sort(key=lambda x: x[1].final_score, reverse=True)

        top_5 = ranked[:5]
        top_10 = ranked[:10]

        # In top 5 and top 10, verify relevant hits dominate
        top_5_relevant = sum(1 for c, b in top_5 if b.relevance >= 45.0 and not b.missing_mandatory_intents)
        top_10_relevant = sum(1 for c, b in top_10 if b.relevance >= 45.0 and not b.missing_mandatory_intents)

        if top_5_relevant >= 4:  # >= 80%
            precision_5_hits += 1
        if top_10_relevant >= 8: # >= 80%
            precision_10_hits += 1

        total_evaluated += 1

    p5_accuracy = precision_5_hits / total_evaluated
    p10_accuracy = precision_10_hits / total_evaluated

    print("\n[EVALUATION BENCHMARK RESULTS]")
    print(f"Total Queries Evaluated: {total_evaluated}")
    print(f"Precision@5 >= 80% rate: {p5_accuracy:.1%}")
    print(f"Precision@10 >= 80% rate: {p10_accuracy:.1%}")

    assert p5_accuracy >= 0.80, f"Precision@5 rate too low: {p5_accuracy:.1%}"
    assert p10_accuracy >= 0.80, f"Precision@10 rate too low: {p10_accuracy:.1%}"
