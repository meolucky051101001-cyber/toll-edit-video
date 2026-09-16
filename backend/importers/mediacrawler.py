"""Independent reader for MediaCrawler content exports; see docs/MEDIACRAWLER.md."""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, ValidationError

from backend.schemas.contracts import VideoResult

MAX_BYTES = 1_048_576
MAX_ROWS = 200


class ImportProblem(ValueError):
    pass


class ImportRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_BYTES)
    format: Literal["json", "jsonl"] = "json"


class ImportItem(BaseModel):
    row: int
    query: str
    video: VideoResult


class ImportIssue(BaseModel):
    row: int
    reason: str


class ParsedImport(BaseModel):
    digest: str
    total: int
    items: list[ImportItem]
    issues: list[ImportIssue]


def text(value: object, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def metric(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿kw]?)\+?", raw)
    if not match:
        return None
    number = (
        float(match[1]) * {"": 1, "k": 1000, "w": 10000, "万": 10000, "亿": 100000000}[match[2]]
    )
    return int(number) if math.isfinite(number) and number <= 10**12 else None


def timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value))
        if number > 10**11:
            number /= 1000
        result = datetime.fromtimestamp(number, tz=timezone.utc)
        return result if 1970 < result.year <= 2100 else None
    except (ValueError, OverflowError, OSError):
        return None


def thumbnail(value: object) -> str | None:
    raw = text(value, 2048)
    if not raw:
        return None
    raw = raw.split(",")[0]
    try:
        url = HttpUrl(raw)
    except ValidationError:
        return None
    domains = ("xhscdn.com", "rednotecdn.com", "byteimg.com", "douyinpic.com")
    if url.scheme != "https" or url.username or url.password or url.port != 443:
        return None
    return (
        str(url)
        if any(url.host == d or (url.host or "").endswith("." + d) for d in domains)
        else None
    )


def normalize(row: dict) -> tuple[VideoResult, str]:
    if "comment_id" in row or "content" in row:
        raise ImportProblem("Bỏ qua bình luận; cần file content của video.")
    if "note_id" in row and "aweme_id" in row:
        raise ImportProblem("Bản ghi chứa hai loại ID nền tảng.")
    if "note_id" in row:
        video_id = str(row["note_id"]).lower()
        if str(row.get("type", "")).lower() != "video":
            raise ImportProblem("Bỏ qua bài ảnh hoặc bài chưa xác định là video Xiaohongshu.")
        if not re.fullmatch(r"[0-9a-fA-F]{24}", video_id):
            raise ImportProblem("ID Xiaohongshu không hợp lệ.")
        platform = "xiaohongshu"
        url = f"https://www.xiaohongshu.com/explore/{video_id}"
        published = timestamp(row.get("time"))
        cover = thumbnail(row.get("image_list"))
    elif "aweme_id" in row:
        video_id = str(row["aweme_id"])
        if str(row.get("aweme_type", "")) != "0":
            raise ImportProblem(
                "Chỉ nhận video Douyin aweme_type=0; bỏ qua loại khác hoặc chưa rõ."
            )
        if not re.fullmatch(r"[0-9]{15,25}", video_id):
            raise ImportProblem("ID Douyin không hợp lệ; ID phải là chuỗi để giữ đủ chữ số.")
        if isinstance(row["aweme_id"], float):
            raise ImportProblem("ID Douyin dạng số thực có thể đã mất độ chính xác.")
        platform = "douyin"
        url = f"https://www.douyin.com/video/{video_id}"
        published = timestamp(row.get("create_time"))
        cover = thumbnail(row.get("cover_url"))
    else:
        raise ImportProblem("Không nhận ra bản ghi content Xiaohongshu/Douyin.")
    tags = row.get("tag_list", "")
    hashtags = (
        [v.strip().lstrip("#")[:100] for v in tags.split(",") if v.strip()]
        if isinstance(tags, str)
        else []
    )
    caption = text(row.get("desc"), 20000)
    title = text(row.get("title"), 500) or (caption[:255] if caption else None)
    if not title and not caption:
        raise ImportProblem("Thiếu cả tiêu đề lẫn mô tả video.")
    # Preserve the exported display name as supplied; never reconstruct anonymized identities.
    video = VideoResult(
        platform=platform,
        platform_video_id=video_id,
        url=url,
        title=title,
        caption=caption,
        author_name=text(row.get("nickname"), 100),
        thumbnail_url=cover,
        hashtags=list(dict.fromkeys(hashtags))[:30],
        published_at=published,
        like_count=metric(row.get("liked_count")),
        favorite_count=metric(row.get("collected_count")),
        comment_count=metric(row.get("comment_count")),
        share_count=metric(row.get("share_count")),
        is_mock=False,
    )
    return video, text(row.get("source_keyword")) or title or "Nhập MediaCrawler"


def parse_export(payload: ImportRequest) -> ParsedImport:
    raw = payload.content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ImportProblem("File vượt quá 1 MB. Hãy chia thành file nhỏ hơn.")
    content = payload.content.lstrip("\ufeff")
    try:
        if payload.format == "jsonl":
            lines = [line for line in content.splitlines() if line.strip()]
            if len(lines) > MAX_ROWS:
                raise ImportProblem("Mỗi lần tối đa 200 bản ghi. Hãy chia nhỏ file.")
            rows = [json.loads(line) for line in lines]
        else:
            rows = json.loads(content)
    except (ValueError, RecursionError):
        raise ImportProblem(
            "File JSON/JSONL không hợp lệ; không có dữ liệu nào được nhập."
        ) from None
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise ImportProblem("Cần danh sách JSON từ 1 đến 200 bản ghi content.")
    result = ParsedImport(
        digest=hashlib.sha256(raw).hexdigest(), total=len(rows), items=[], issues=[]
    )
    seen: set[tuple[str, str | None]] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            result.issues.append(ImportIssue(row=index, reason="Bản ghi không phải object JSON."))
            continue
        try:
            video, query = normalize(row)
            identity = (video.platform, video.platform_video_id)
            if identity in seen:
                raise ImportProblem("Bản ghi trùng ID trong cùng file.")
            seen.add(identity)
            result.items.append(ImportItem(row=index, query=query[:300], video=video))
        except ImportProblem as error:
            result.issues.append(ImportIssue(row=index, reason=str(error)))
        except (ValidationError, ValueError, TypeError, OverflowError):
            result.issues.append(ImportIssue(row=index, reason="Metadata không đúng định dạng."))
    return result
