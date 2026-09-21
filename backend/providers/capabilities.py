from backend.schemas.contracts import PlatformCapabilities

PLATFORM_CAPABILITIES: dict[str, PlatformCapabilities] = {
    "xiaohongshu": PlatformCapabilities(
        id="xiaohongshu",
        name="Xiaohongshu",
        display_name="Tiểu Hồng Thư (RedNote)",
        supports_headless=True,
        supports_video_download=True,
        supports_metrics=True,
        supports_hashtags=True,
        requires_login_for_search=False,
        auth_domains=["xiaohongshu.com", "rednote.com", "xhslink.com"],
        icon_name="book-open",
        is_enabled=True,
        description="Mạng xã hội chia sẻ phong cách sống, làm đẹp và review hàng đầu Trung Quốc.",
    ),
    "douyin": PlatformCapabilities(
        id="douyin",
        name="Douyin",
        display_name="Douyin (抖音)",
        supports_headless=True,
        supports_video_download=True,
        supports_metrics=True,
        supports_hashtags=True,
        requires_login_for_search=False,
        auth_domains=["douyin.com", "iesdouyin.com"],
        icon_name="music",
        is_enabled=True,
        description="Nền tảng video ngắn số 1 Trung Quốc, cái nôi của các xu hướng video viral.",
    ),
    "tiktok": PlatformCapabilities(
        id="tiktok",
        name="TikTok",
        display_name="TikTok Global",
        supports_headless=False,
        supports_video_download=False,
        supports_metrics=False,
        supports_hashtags=True,
        requires_login_for_search=False,
        auth_domains=["tiktok.com", "vm.tiktok.com"],
        icon_name="globe",
        is_enabled=False,
        description="Nền tảng video ngắn toàn cầu (Đang phát triển - Hiện chỉ hỗ trợ trong chế độ Mock).",
    ),
    "youtube_shorts": PlatformCapabilities(
        id="youtube_shorts",
        name="YouTube Shorts",
        display_name="YouTube Shorts",
        supports_headless=False,
        supports_video_download=False,
        supports_metrics=False,
        supports_hashtags=True,
        requires_login_for_search=False,
        auth_domains=["youtube.com", "youtu.be"],
        icon_name="youtube",
        is_enabled=False,
        description="Video ngắn từ YouTube (Đang phát triển - Hiện chỉ hỗ trợ trong chế độ Mock).",
    ),
    "mock": PlatformCapabilities(
        id="mock",
        name="Mock",
        display_name="Môi trường giả lập (Local Test)",
        supports_headless=True,
        supports_video_download=True,
        supports_metrics=True,
        supports_hashtags=True,
        requires_login_for_search=False,
        auth_domains=["example.invalid"],
        icon_name="cpu",
        is_enabled=True,
        description="Nhà cung cấp dữ liệu giả lập ngoại tuyến phục vụ kiểm thử đơn vị & CI/CD.",
    ),
}


def get_platform_capabilities(platform: str) -> PlatformCapabilities | None:
    return PLATFORM_CAPABILITIES.get(platform)


def list_all_capabilities() -> list[PlatformCapabilities]:
    return list(PLATFORM_CAPABILITIES.values())
