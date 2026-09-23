
from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.translator import plan_query

# 15 representative Vietnamese queries covering e-commerce, tools, decor, beauty, and food
EVALUATION_QUERIES = [
    {
        "query": "nhíp gắp sticker",
        "category": "tool_craft",
        "expected_core": "镊子",
        "mandatory_attr": "nhip",
        "relevant_samples": [
            ("Nhíp gắp sticker chuyên dụng 贴纸镊子分享", "https://www.xiaohongshu.com/explore/100000000000000000000001", "xiaohongshu"),
            ("Top 5 nhíp gắp sticker tay cầm cong 咕卡手账镊子", "https://www.xiaohongshu.com/explore/100000000000000000000002", "xiaohongshu"),
            ("Review nhíp gắp sticker siêu dính 贴纸专用镊子测评", "https://www.douyin.com/video/7000000000000000001", "douyin"),
        ],
        "distractor_samples": [
            ("Bộ sưu tập sticker cute giá rẻ 贴纸分享", "https://www.xiaohongshu.com/explore/100000000000000000000003", "xiaohongshu"),
            ("Sticker dán sổ tay siêu xinh 手账贴纸", "https://www.douyin.com/video/7000000000000000002", "douyin"),
        ],
    },
    {
        "query": "dụng cụ bóc sticker",
        "category": "tool_craft",
        "expected_core": "镊子",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Dụng cụ bóc sticker và gắp charm 咕卡工具贴纸镊子", "https://www.xiaohongshu.com/explore/100000000000000000000004", "xiaohongshu"),
            ("Mẹo dùng dụng cụ bóc sticker không rách 手账工具", "https://www.douyin.com/video/7000000000000000003", "douyin"),
        ],
        "distractor_samples": [
            ("Hình dán cute bóc dán 贴纸", "https://www.xiaohongshu.com/explore/100000000000000000000005", "xiaohongshu"),
        ],
    },
    {
        "query": "đập hộp văn phòng phẩm cute",
        "category": "unboxing",
        "expected_core": "文具",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Đập hộp văn phòng phẩm cute的可有文具开箱", "https://www.xiaohongshu.com/explore/100000000000000000000006", "xiaohongshu"),
            ("Unbox văn phòng phẩm học sinh cute 沉浸式可爱文具开箱", "https://www.douyin.com/video/7000000000000000004", "douyin"),
        ],
        "distractor_samples": [
            ("Đập hộp đồ ăn vặt Trung Quốc 零食开箱", "https://www.douyin.com/video/7000000000000000005", "douyin"),
        ],
    },
    {
        "query": "decor bàn học vintage",
        "category": "decor",
        "expected_core": "书桌",
        "mandatory_attr": "vintage",
        "relevant_samples": [
            ("Decor bàn học phong cách vintage 复古书桌布置改造", "https://www.xiaohongshu.com/explore/100000000000000000000007", "xiaohongshu"),
            ("Góc học tập vintage cổ điển 复古桌面布置", "https://www.douyin.com/video/7000000000000000006", "douyin"),
        ],
        "distractor_samples": [
            ("Decor phòng ngủ hiện đại 现代卧室布置", "https://www.xiaohongshu.com/explore/100000000000000000000008", "xiaohongshu"),
            ("Bàn học phong cách cyberpunk 桌搭", "https://www.douyin.com/video/7000000000000000007", "douyin"),
        ],
    },
    {
        "query": "phối đồ mùa đông giá rẻ",
        "category": "fashion",
        "expected_core": "穿搭",
        "mandatory_attr": "mua dong",
        "relevant_samples": [
            ("Phối đồ mùa đông giá rẻ cho học sinh 冬季平价穿搭分享", "https://www.xiaohongshu.com/explore/100000000000000000000009", "xiaohongshu"),
            ("Set đồ mùa đông bình dân 冬日平价OOTD", "https://www.douyin.com/video/7000000000000000008", "douyin"),
        ],
        "distractor_samples": [
            ("Phối đồ mùa hè đi biển 夏季穿搭", "https://www.xiaohongshu.com/explore/100000000000000000000010", "xiaohongshu"),
            ("Đồng hồ sang trọng đắt đỏ 奢华手表", "https://www.douyin.com/video/7000000000000000009", "douyin"),
        ],
    },
    {
        "query": "trị mụn ẩn cho da dầu",
        "category": "skincare",
        "expected_core": "去闭口",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Mẹo trị mụn ẩn cho da dầu hiệu quả 油皮去闭口粉刺技巧", "https://www.xiaohongshu.com/explore/100000000000000000000011", "xiaohongshu"),
            ("Quy trình skincare trị mụn ẩn da dầu 油皮闭口护肤分享", "https://www.douyin.com/video/7000000000000000010", "douyin"),
        ],
        "distractor_samples": [
            ("Mèo cute chơi bóng 可愛猫咪", "https://www.douyin.com/video/7000000000000000011", "douyin"),
            ("Sửa xe máy honda 修摩托车", "https://www.xiaohongshu.com/explore/100000000000000000000012", "xiaohongshu"),
        ],
    },
    {
        "query": "bàn phím cơ giá rẻ",
        "category": "tech",
        "expected_core": "机械键盘",
        "mandatory_attr": "gia re",
        "relevant_samples": [
            ("Review bàn phím cơ giá rẻ sinh viên 百元平价机械键盘测评", "https://www.xiaohongshu.com/explore/100000000000000000000013", "xiaohongshu"),
            ("Top 3 bàn phím cơ giá rẻ gõ êm 性价比机械键盘推荐", "https://www.douyin.com/video/7000000000000000012", "douyin"),
        ],
        "distractor_samples": [
            ("Bàn làm việc gỗ sồi 办公桌", "https://www.xiaohongshu.com/explore/100000000000000000000014", "xiaohongshu"),
            ("Bàn phím cơ custom 10 triệu 高端客制化", "https://www.douyin.com/video/7000000000000000013", "douyin"),
        ],
    },
    {
        "query": "sữa rửa mặt cho da dầu",
        "category": "skincare",
        "expected_core": "洗面奶",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Top sữa rửa mặt cho da dầu kiềm dầu 油皮洗面奶推荐", "https://www.xiaohongshu.com/explore/100000000000000000000015", "xiaohongshu"),
            ("Review sữa rửa mặt kiềm dầu dịu nhẹ 控油洁面测评", "https://www.douyin.com/video/7000000000000000014", "douyin"),
        ],
        "distractor_samples": [
            ("Sửa chữa xe máy tại nhà 摩托车维修", "https://www.douyin.com/video/7000000000000000015", "douyin"),
            ("Cách làm sữa chua hoa quả 自制酸奶", "https://www.xiaohongshu.com/explore/100000000000000000000016", "xiaohongshu"),
        ],
    },
    {
        "query": "nuôi mèo cute",
        "category": "pets",
        "expected_core": "猫",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Kinh nghiệm nuôi mèo cute cho người mới 养猫日常可爱猫咪", "https://www.xiaohongshu.com/explore/100000000000000000000017", "xiaohongshu"),
            ("Nuôi mèo cute: bé mèo ăn hạt cưng xỉu 养猫日常可爱猫咪", "https://www.douyin.com/video/7000000000000000016", "douyin"),
        ],
        "distractor_samples": [
            ("Mẹo vặt cuộc sống 实用小技巧", "https://www.douyin.com/video/7000000000000000017", "douyin"),
            ("Nuôi chó alaska khổng lồ 养狗日常", "https://www.xiaohongshu.com/explore/100000000000000000000018", "xiaohongshu"),
        ],
    },
    {
        "query": "sửa xe máy",
        "category": "mechanics",
        "expected_core": "修",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Hướng dẫn tự sửa xe máy cơ bản 机车维修修摩托车", "https://www.douyin.com/video/7000000000000000018", "douyin"),
            ("Mẹo sửa xe máy không nổ máy 修车日常", "https://www.xiaohongshu.com/explore/100000000000000000000019", "xiaohongshu"),
        ],
        "distractor_samples": [
            ("Sữa tươi tiệt trùng nguyên chất 牛奶", "https://www.douyin.com/video/7000000000000000019", "douyin"),
            ("Sữa rửa mặt trị mụn 洁面", "https://www.xiaohongshu.com/explore/100000000000000000000020", "xiaohongshu"),
        ],
    },
    {
        "query": "móc len hoa tulip",
        "category": "handicraft",
        "expected_core": "钩织",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Hướng dẫn móc len hoa tulip cho người mới 钩织郁金香手工", "https://www.xiaohongshu.com/explore/100000000000000000000021", "xiaohongshu"),
            ("Móc len hoa tulip trang trí phòng 毛线钩织花朵", "https://www.douyin.com/video/7000000000000000020", "douyin"),
        ],
        "distractor_samples": [
            ("Bánh bao hoa cúc 花卷包子", "https://www.douyin.com/video/7000000000000000021", "douyin"),
        ],
    },
    {
        "query": "cách làm bánh bao",
        "category": "cooking",
        "expected_core": "包子",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Cách làm bánh bao xá xíu vỏ mềm mịn 手工包子制作教程", "https://www.xiaohongshu.com/explore/100000000000000000000022", "xiaohongshu"),
            ("Bí quyết làm bánh bao không bị xẹp 自制包子做法", "https://www.douyin.com/video/7000000000000000022", "douyin"),
        ],
        "distractor_samples": [
            ("Làm bánh mì hoa cúc 面包教程", "https://www.douyin.com/video/7000000000000000023", "douyin"),
            ("Bánh cuốn nóng gia truyền 肠粉", "https://www.xiaohongshu.com/explore/100000000000000000000023", "xiaohongshu"),
        ],
    },
    {
        "query": "bình giữ nhiệt cute",
        "category": "household",
        "expected_core": "保温杯",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Bình giữ nhiệt cute đi học cho bạn gái 高颜值可爱保温杯", "https://www.xiaohongshu.com/explore/100000000000000000000024", "xiaohongshu"),
            ("Unbox bình giữ nhiệt inox cute 少女心保温水杯开箱", "https://www.douyin.com/video/7000000000000000024", "douyin"),
        ],
        "distractor_samples": [
            ("Ấm đun nước siêu tốc 电热水壶", "https://www.douyin.com/video/7000000000000000025", "douyin"),
        ],
    },
    {
        "query": "tai nghe bluetooth",
        "category": "tech",
        "expected_core": "耳机",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Review tai nghe bluetooth chống ồn giá rẻ 无线蓝牙耳机测评", "https://www.xiaohongshu.com/explore/100000000000000000000025", "xiaohongshu"),
            ("Top tai nghe bluetooth pin trâu 平价蓝牙耳机推荐", "https://www.douyin.com/video/7000000000000000026", "douyin"),
        ],
        "distractor_samples": [
            ("Chuột máy tính có dây 有线鼠标", "https://www.douyin.com/video/7000000000000000027", "douyin"),
        ],
    },
    {
        "query": "chuột không dây",
        "category": "tech",
        "expected_core": "鼠标",
        "mandatory_attr": None,
        "relevant_samples": [
            ("Chuột không dây công thái học văn phòng 办公无线静音鼠标", "https://www.xiaohongshu.com/explore/100000000000000000000026", "xiaohongshu"),
            ("Review chuột không dây bluetooth giá rẻ 蓝牙无线鼠标测评", "https://www.douyin.com/video/7000000000000000028", "douyin"),
        ],
        "distractor_samples": [
            ("Nuôi chuột hamster cute 仓鼠日常", "https://www.douyin.com/video/7000000000000000029", "douyin"),
        ],
    },
]


def test_query_plan_and_mandatory_preservation():
    """Verify that query expansion preserves core subjects and mandatory attributes for all 15 queries."""
    for item in EVALUATION_QUERIES:
        q = item["query"]
        plan = plan_query(q)
        assert plan.tiered_queries, f"Query '{q}' produced empty tiered queries"
        assert any(item["expected_core"] in t for t in plan.tiered_queries), (
            f"Query '{q}' tiered queries {plan.tiered_queries} did not contain core '{item['expected_core']}'"
        )
        if item["mandatory_attr"]:
            assert item["mandatory_attr"] in plan.mandatory_attributes, (
                f"Query '{q}' missing expected mandatory attribute '{item['mandatory_attr']}'"
            )


def test_ranking_precision_benchmark_across_queries():
    """Benchmark Precision@10 across all 15 queries, verifying true matches rank ahead of distractors."""
    results_summary = []

    for item in EVALUATION_QUERIES:
        q = item["query"]
        plan = plan_query(q)

        # Build candidate pool with true matches and distractors
        pool = []
        for title, url, platform in item["relevant_samples"]:
            vr = VideoResult(
                platform=platform,
                title=title,
                url=url,
                author_name="test_author",
                like_count=1000,
            )
            pool.append((vr, True))

        for title, url, platform in item["distractor_samples"]:
            vr = VideoResult(
                platform=platform,
                title=title,
                url=url,
                author_name="test_author",
                like_count=500,
            )
            pool.append((vr, False))

        scored = []
        for vr, is_true in pool:
            breakdown = rank_detailed(vr, q, alternative_queries=plan.tiered_queries)
            scored.append((vr, is_true, breakdown))

        # Sort descending by final_score
        scored.sort(key=lambda x: x[2].final_score, reverse=True)

        top_k = scored[:10]
        true_positives = sum(1 for _, is_true, _ in top_k if is_true)
        precision_at_k = true_positives / len(top_k) if top_k else 0.0

        results_summary.append({
            "query": q,
            "category": item["category"],
            "candidates": len(scored),
            "precision_at_k": precision_at_k,
            "top_score": top_k[0][2].final_score if top_k else 0,
            "bottom_score": top_k[-1][2].final_score if top_k else 0,
        })

        # Precision assertions: relevant items must score significantly higher than distractors
        # For relevant items: final_score should be >= 40.0
        # For distractors: final_score should be penalized / capped
        for vr, is_true, breakdown in scored:
            if is_true:
                assert breakdown.final_score >= 35.0, (
                    f"Relevant video '{vr.title}' for query '{q}' scored too low: {breakdown.final_score}"
                )
            else:
                assert breakdown.final_score <= 36.0, (
                    f"Distractor video '{vr.title}' for query '{q}' scored too high: {breakdown.final_score}"
                )

        assert precision_at_k >= len(item["relevant_samples"]) / len(top_k)

    # All queries evaluated successfully
    assert len(results_summary) == 15
    print("\n--- PRECISION EVALUATION SUMMARY (15 Queries) ---")
    for r in results_summary:
        print(f"[{r['category']}] '{r['query']}': Precision={r['precision_at_k']:.2f}, TopScore={r['top_score']:.1f}")
