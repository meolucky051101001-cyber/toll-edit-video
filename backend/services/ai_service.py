import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable

from pydantic import ValidationError

from backend.core.config import Settings
from backend.providers.ai.base import AIError, AIProvider
from backend.providers.ai.registry import get_ai_provider
from backend.schemas.expansion import ExpansionOutcome, QueryExpansion
from backend.schemas.script_analysis import RemakeScriptVI, ScriptAnalysisOut

logger = logging.getLogger("research")


class AIService:
    def __init__(
        self, config: Settings, factory: Callable[[Settings], AIProvider] = get_ai_provider
    ):
        self.config = config
        self.factory = factory
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.pending = 0
        self.cache: OrderedDict[str, tuple[float, ExpansionOutcome]] = OrderedDict()

    def clear_cache(self) -> None:
        self.cache.clear()

    async def expand(self, query: str) -> ExpansionOutcome:
        if self.pending >= 3:
            return ExpansionOutcome(
                original_query=query,
                source="fallback",
                queries=[query],
                warning="AI đang bận. Hãy đợi lượt hiện tại hoàn tất.",
                error_code="busy",
            )
        self.pending += 1
        try:
            async with asyncio.timeout(85):
                return await self._expand(query)
        except TimeoutError:
            return ExpansionOutcome(
                original_query=query,
                source="fallback",
                queries=[query],
                warning="AI quá thời gian. Đang dùng query gốc.",
                error_code="timeout",
            )
        finally:
            self.pending -= 1

    async def _expand(self, query: str) -> ExpansionOutcome:
        query = " ".join(query.split())
        async with self.lock:
            entry = self.cache.get(query)
            if entry and time.monotonic() - entry[0] < 600:
                self.cache.move_to_end(query)
                return entry[1].model_copy(deep=True, update={"cached": True})
            error = AIError("disabled", "AI chưa được bật. Đang dùng query gốc.")
            for attempt in range(3):
                try:
                    if time.monotonic() < self.cooldown_until:
                        raise AIError(
                            "quota",
                            "Gemini đang tạm nghỉ sau khi chạm hạn mức. Hãy thử lại sau một phút.",
                        )
                    provider = self.factory(self.config)
                    remaining = self.config.ai_min_interval - (time.monotonic() - self.last_request)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    self.last_request = time.monotonic()
                    raw = await provider.expand(query)
                    payload = QueryExpansion.model_validate_json(raw)
                    if payload.original_query != query:
                        raise ValueError("Original query mismatch")
                    outcome = ExpansionOutcome(
                        original_query=query,
                        source="gemini",
                        expansion=payload,
                        queries=payload.queries(self.config.max_queries),
                    )
                    self.cache[query] = (time.monotonic(), outcome)
                    while len(self.cache) > 64:
                        self.cache.popitem(last=False)
                    logger.info(
                        "ai provider=%s model=%s attempt=%d status=success",
                        provider.name,
                        self.config.ai_model,
                        attempt + 1,
                    )
                    return outcome.model_copy(deep=True)
                except (ValidationError, ValueError):
                    error = AIError(
                        "invalid_json",
                        "AI trả JSON không đúng yêu cầu. Đang dùng query gốc.",
                        retryable=True,
                    )
                except AIError as caught:
                    error = caught
                except Exception:
                    error = AIError(
                        "unexpected", "Không xử lý được phản hồi AI. Đang dùng query gốc."
                    )
                logger.warning("ai attempt=%d code=%s", attempt + 1, error.code)
                if error.code == "quota":
                    self.cooldown_until = time.monotonic() + 60
                if not error.retryable:
                    break
            return ExpansionOutcome(
                original_query=query,
                source="fallback",
                queries=[query],
                warning=str(error),
                error_code=error.code,
            )

    async def analyze_script(self, video_id: str, video_data: dict) -> ScriptAnalysisOut:
        fallback = generate_fallback_script_analysis(video_id, video_data)
        if not self.config.ai_api_key.get_secret_value() or not self.config.ai_free_tier_confirmed:
            return fallback

        try:
            provider = self.factory(self.config)
            video_payload = dict(video_data)
            video_payload["video_id"] = video_id
            raw = await provider.analyze_script(video_payload)
            data = json.loads(raw)
            data["video_id"] = video_id
            data["source"] = "gemini"
            data["cached"] = False
            return ScriptAnalysisOut.model_validate(data)
        except Exception as exc:
            logger.warning("ai script analysis failed: %s; using fallback", exc)
            return fallback


def generate_fallback_script_analysis(video_id: str, video_data: dict) -> ScriptAnalysisOut:
    title = (video_data.get("title") or "").strip()
    caption = (video_data.get("caption") or "").strip()
    hashtags = video_data.get("hashtags") or []

    topic = title or (caption[:60] if caption else "") or (hashtags[0] if hashtags else "chủ đề này")

    hook_3s = (
        f"Video thu hút người xem trong 3 giây đầu nhờ cách mở đầu trực diện vào vấn đề: '{topic}'. "
        f"Gợi mở sự tò mò cao khiến người xem muốn dừng lại xem tiếp phần diễn biến."
    )

    core_points = [
        f"Đặt vấn đề trọng tâm và điểm độc đáo của {topic}",
        "Trực quan hóa quá trình thực hiện / điểm nổi bật từng bước rõ ràng",
        "Đúc kết kinh nghiệm, lưu ý quan trọng và kết quả đạt được",
    ]

    retention_tactics = [
        "Nhịp điệu dồn dập, chuyển cảnh nhanh không có đoạn chết",
        "Tạo tò mò từ 3s đầu và hé lộ kết quả ở những giây cuối",
        "Hình ảnh cận cảnh góc quay sinh động",
    ]

    intro_vi = f"Nếu bạn đang quan tâm đến {topic}, đừng bỏ qua cách làm cực chuẩn này!"
    body_vi = [
        f"Phần 1: Chuẩn bị và nắm rõ nguyên tắc cốt lõi của {topic}.",
        "Phần 2: Bắt tay vào làm theo từng bước trực quan, chú ý những chi tiết nhỏ nhưng quan trọng.",
        "Phần 3: Mẹo tối ưu giúp bạn làm nhanh hơn gấp đôi mà kết quả vẫn đẹp chuẩn.",
    ]
    cta_vi = "Bấm lưu lại ngay để áp dụng và follow kênh để cập nhật thêm nhiều bí quyết hay nhé!"

    return ScriptAnalysisOut(
        video_id=video_id,
        hook_3s=hook_3s,
        core_points=core_points,
        retention_tactics=retention_tactics,
        remake_script_vi=RemakeScriptVI(
            intro=intro_vi,
            body=body_vi,
            cta=cta_vi,
        ),
        source="fallback",
        cached=False,
    )
