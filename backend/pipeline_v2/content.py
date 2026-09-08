"""Pure content helpers shared by the Pipeline V2 runner and adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .segments import RuntimeSegment, segment_from_dict, segment_to_dict
from .stage_validation import is_real_rvc_model


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, secs, millis)


def compose_srt(segments: Iterable[Any]) -> str:
    blocks = []
    for position, segment in enumerate(segments, 1):
        blocks.append(
            "{}\n{} --> {}\n{}".format(
                int(getattr(segment, "index", position)),
                format_srt_timestamp(segment.start.total_seconds()),
                format_srt_timestamp(segment.end.total_seconds()),
                str(segment.content).strip(),
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def validate_translated_batch(
    source: Sequence[RuntimeSegment], translated: Sequence[RuntimeSegment]
) -> None:
    """Reject structurally incomplete or unchanged-CJK translation checkpoints."""

    if len(source) != len(translated):
        raise RuntimeError("Translation batch changed the segment count")
    for original, result in zip(source, translated):
        if int(original.index) != int(result.index):
            raise RuntimeError("Translation batch changed segment identity")
        source_text = str(original.content).strip()
        translated_text = str(result.content).strip()
        if not translated_text:
            raise RuntimeError(
                "Translation returned empty text for segment {}".format(original.index)
            )
        contains_cjk = any("\u4e00" <= character <= "\u9fff" for character in source_text)
        if contains_cjk and translated_text == source_text:
            raise RuntimeError(
                "Translation left CJK source unchanged for segment {}".format(
                    original.index
                )
            )


def merge_ocr_geometry(
    translated: Iterable[Any], ocr_segments: Iterable[Any]
) -> List[RuntimeSegment]:
    geometry = {
        int(segment.index): segment_from_dict(segment_to_dict(segment))
        for segment in ocr_segments
    }
    merged = []
    for translated_segment in translated:
        segment = segment_from_dict(segment_to_dict(translated_segment))
        source = geometry.get(int(segment.source_segment_id or segment.index)) or geometry.get(
            int(segment.index)
        )
        if source is not None:
            segment.y_pct = source.y_pct
            segment.max_y_pct = source.max_y_pct
            segment.best_block = source.best_block
            segment.tracking_blocks = source.tracking_blocks
        merged.append(segment)
    return merged


def discover_rvc_model(workspace: Path) -> Optional[Path]:
    workspace_path = Path(workspace).resolve()
    search_dirs = [
        workspace_path.parent / "MyVoiceModel_v2",
        workspace_path / "MyVoiceModel_v2",
        workspace_path.parent / "models" / "rvc",
        workspace_path / "models" / "rvc",
    ]
    for model_directory in search_dirs:
        if not model_directory.is_dir():
            continue
        for candidate in sorted(model_directory.glob("*.pth")):
            if is_real_rvc_model(candidate):
                return candidate
    return None
