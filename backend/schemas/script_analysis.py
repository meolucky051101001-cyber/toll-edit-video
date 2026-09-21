from datetime import datetime, timezone

from pydantic import BaseModel, Field


class StoryboardScene(BaseModel):
    scene_number: int = Field(description="Thứ tự phân cảnh (1, 2, 3...)")
    timestamp_range: str = Field(description="Khung thời gian ước tính (ví dụ: '0:00 - 0:03')")
    visual_action: str = Field(description="Mô tả hành động, góc máy, chuyển động hình ảnh")
    spoken_narration: str = Field(description="Lời thoại hoặc giọng đọc thuyết minh tiếng Việt")
    on_screen_text: str = Field(default="", description="Chữ hiển thị trên màn hình, tiêu đề, sticker")
    sound_music: str = Field(default="", description="Gợi ý hiệu ứng âm thanh SFX, mood nhạc nền")


class RemakeScriptVI(BaseModel):
    intro: str = Field(description="3-5s punchy Vietnamese opening hook")
    body: list[str] = Field(description="Step-by-step Vietnamese body script")
    cta: str = Field(description="Engaging Vietnamese call-to-action")


class ScriptAnalysisOut(BaseModel):
    video_id: str
    hook_3s: str = Field(description="Phân tích câu mở đầu & lý do hook thành công")
    core_points: list[str] = Field(description="Các luận điểm cốt lõi của video")
    retention_tactics: list[str] = Field(description="Kỹ thuật giữ chân người xem")
    remake_script_vi: RemakeScriptVI = Field(description="Kịch bản chuyển thể tiếng Việt")
    storyboard: list[StoryboardScene] = Field(default_factory=list, description="Phân cảnh quay chi tiết dạng storyboard")
    analysis_basis: str = Field(default="metadata", description="Căn cứ phân tích (multimodal_metadata, transcript, metadata)")
    analysis_version: str = Field(default="v2.0", description="Phiên bản thuật toán phân tích kịch bản")
    model_used: str = Field(default="gemini", description="Model hoặc nguồn sinh kịch bản")
    source: str = Field(default="gemini", description="Nguồn phân tích (gemini hoặc fallback)")
    cached: bool = Field(default=False)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ScriptAnalysisRequest(BaseModel):
    force_refresh: bool = False
