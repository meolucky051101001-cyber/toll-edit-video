from pydantic import BaseModel, Field


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
    source: str = Field(default="gemini", description="Nguồn phân tích (gemini hoặc fallback)")
    cached: bool = Field(default=False)


class ScriptAnalysisRequest(BaseModel):
    force_refresh: bool = False
