"""
v1_orchestrator.py - Bộ điều phối thực thi hợp nhất toàn diện cho Tool V1 (Giai đoạn A-G).
Hợp nhất Telegram Bot, Web API (main.py) và Batch Processor vào MỘT luồng điều phối duy nhất.
Tích hợp:
- Video Length Router (SHORT, MEDIUM, LONG, AUTO, CUSTOM) & đóng băng cấu hình bất biến
- Flexible Model Registry & Mode Policy (GPU CUDA enforcement, no silent CPU fallback)
- Atomic Job Manifest & Stage Checkpoint / Smart Resume từ stage dở dang
- Smart Skip OCR an toàn & Adaptive Vocal Separation
- Hierarchical Audio Mixer (>100 câu / >300s) & Safe ASR Chunking
- Quality Gate tự động kiểm tra 8 nhóm trước khi xuất bản (publish)
"""

from __future__ import annotations

import os
import time
import math
import logging
import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from v1_stage_runtime import save_payload, load_payload, await_stage
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# V1 callers select this orchestrator explicitly; do not mutate process mode.

try:
    from v1_checkpoint import ManifestManager, JobManifest, STAGES_ORDER, fingerprint
    from v1_video_router import create_router_snapshot
    from v1_model_registry import (
        ModelStage, resolve_effective_model
    )
    from v1_feature_flags import get_feature_flags, migrate_legacy_asr_model
    from v1_job_policy import (
        looks_like_bgm_hallucination as _looks_like_bgm_hallucination,
        merged_speech_coverage as _merged_speech_coverage,
        visual_ocr_transcript_usable as _visual_ocr_transcript_usable,
        visual_transcript_candidates, TRANSCRIPT_SOURCE_POLICY_VERSION,
    )
except ImportError:
    from .v1_checkpoint import ManifestManager, JobManifest, STAGES_ORDER, fingerprint
    from .v1_video_router import create_router_snapshot
    from .v1_model_registry import (
        ModelStage, resolve_effective_model
    )
    from .v1_feature_flags import get_feature_flags, migrate_legacy_asr_model
    from .v1_job_policy import (
        looks_like_bgm_hallucination as _looks_like_bgm_hallucination,
        merged_speech_coverage as _merged_speech_coverage,
        visual_ocr_transcript_usable as _visual_ocr_transcript_usable,
        visual_transcript_candidates, TRANSCRIPT_SOURCE_POLICY_VERSION,
    )


QC_POLICIES = ("REPORT_ONLY", "WARN", "BLOCK")


def resolve_qc_policy(config=None, overrides=None, workspace_path=None) -> str:
    """Explicit job override wins; otherwise read the single live setting."""
    if isinstance(overrides, dict):
        requested = str(overrides.get("qc_policy") or "").upper()
        if requested in QC_POLICIES:
            return requested
    if isinstance(config, dict) and config.get("qc_policy_source") == "job":
        requested = str(config.get("qc_policy") or "").upper()
        if requested in QC_POLICIES:
            return requested
    try:
        live = str(get_feature_flags(workspace_path).get("V1_QC_POLICY", "BLOCK")).upper()
    except Exception:
        return "BLOCK"
    return live if live in QC_POLICIES else "BLOCK"



@dataclass
class _ExecutionContext:
    """Job-owned settings/checkpoints; no state is shared between execute calls."""
    v_path: Path
    job_id: str
    out_dir: Path
    manifest: JobManifest
    effective_cfg: dict
    planned: dict
    resolved_mode: str
    overrides: dict
    delivery_path: Optional[str | Path]
    t_job_start: float
    base_name: str
    visual_sub_to_speech: bool
    video_dur_s: float = 0.0
    can_resume: bool = False
    verified_stages: list = field(default_factory=list)
    artifacts_acc: dict = field(default_factory=dict)
    notify: Any = None
    check_stop: Any = None
    pause_checkpoint: Any = None
    run_stage: Any = None
    ocr_reader_releaser: Any = None


class V1Orchestrator:
    """Bộ điều phối duy nhất thực thi pipeline lồng tiếng video cho Tool V1."""

    def __init__(self, workspace_path: Optional[str | Path] = None):
        if workspace_path:
            self.workspace = Path(workspace_path)
        else:
            base_dir = Path(__file__).resolve().parent
            self.workspace = Path(os.getenv("AUTODUB_WORKSPACE", str(base_dir.parent / "workspace")))
        self.manifest_manager = ManifestManager(self.workspace)
        # Đồng bộ cờ tính năng từ file cấu hình bền vững
        # Per-job flags are frozen; never change global process settings here.

    @staticmethod
    def _plan_info_from_manifest(manifest: JobManifest) -> Dict[str, Any]:
        """Return the frozen plan stored with a job, without consulting current settings."""
        config = manifest.effective_config or {}
        routing = manifest.routing_snapshot or {}
        planned = dict(config.get("planned_pipeline") or routing.get("planned_pipeline") or {})
        planned_models = dict(config.get("planned_models") or {})

        asr_model = planned.get("asr_model")
        if not asr_model:
            asr_spec = planned_models.get("asr") or {}
            model_id = asr_spec.get("model_id") if isinstance(asr_spec, dict) else None
            if model_id == "whisper_large_v3_turbo":
                asr_model = "whisper_turbo"
            elif model_id == "whisper_sensevoice_hybrid":
                asr_model = "whisper_sensevoice"
            elif model_id == "sensevoice_small":
                asr_model = "sensevoice_small"
            else:
                asr_model = migrate_legacy_asr_model(model_id, "job manifest")
        asr_model = migrate_legacy_asr_model(
            asr_model or config.get("asr_model") or "whisper_turbo", "job manifest"
        )
        planned["asr_model"] = asr_model

        return {
            "job_id": manifest.job_id,
            "manifest": manifest.to_dict(),
            "routing": routing,
            "planned_pipeline": planned,
            "planned_models": planned_models,
            "effective_config": config,
        }

    def plan_job(
        self,
        video_path: str | Path,
        job_id: str,
        user_mode: str = "auto",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Lập kế hoạch thực thi bất biến cho job trước khi chạy:
        1. Phân tích video metadata và độ phức tạp.
        2. Quyết định VideoMode (SHORT, MEDIUM, LONG, CUSTOM).
        3. Phân bổ các model hiệu lực từ Model Registry.
        4. Đóng băng cấu hình vào job_frozen_configs.json.
        5. Tạo và lưu trữ JobManifest nguyên tử.
        """
        path = Path(video_path)
        overrides = dict(overrides or {})

        existing = self.manifest_manager.load_manifest(job_id)
        if existing and path.is_file() and existing.video_fingerprint == fingerprint(path):
            return self._plan_info_from_manifest(existing)

        from v1_job_policy import apply_job_policy
        from job_config_service import freeze_job_config
        from ai.v1_auto_voice import get_auto_voice_config
        import voice_selection
        flags = dict(overrides.get("feature_flags") or get_feature_flags(self.workspace))
        mode = user_mode or flags.get("V1_VIDEO_MODE", "AUTO")
        routing = apply_job_policy(create_router_snapshot(
            file_path=path, requested_mode=mode, job_id=job_id,
            user_overrides=overrides, workspace_path=self.workspace,
        ), flags, overrides)
        duration = float(routing.get("metadata", {}).get("duration_s", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Không đọc được thời lượng video; dừng trước khi chọn model.")
        limit = routing.get("thresholds", {}).get("max_video_seconds", 3600)
        if duration > limit:
            raise ValueError(f"Video dài hơn giới hạn đã cấu hình ({limit} giây).")
        planned = routing["planned_pipeline"]
        resolved_mode = routing["resolved_mode"]
        planned_models = {
            stage.value: resolve_effective_model(stage, resolved_mode,
                planned["separation_model"] if stage == ModelStage.SEPARATION
                else planned["asr_model"] if stage == ModelStage.ASR else None).to_dict()
            for stage in ModelStage
        }
        # Credentials are runtime arguments, never written to manifests/snapshots.
        safe_overrides = {k: v for k, v in overrides.items()
                          if not any(s in k.lower() for s in ("key", "token", "secret", "password"))}
        voice_config = dict(overrides.get("voice_config") or get_auto_voice_config(str(self.workspace)))
        explicit_source = str(overrides.get("voice_source") or "").strip()
        explicit_param = str(overrides.get("voice_param") or "").strip()
        if explicit_source or explicit_param:
            if not explicit_source or not explicit_param:
                raise ValueError("Chọn giọng riêng cần cả voice_source và voice_param.")
            selected_voice = next((dict(voice) for voice in voice_selection.catalog()
                                   if voice.get("source") == explicit_source
                                   and str(voice.get("param")) == explicit_param), None)
            voice_config["manual_voice"] = selected_voice or {
                "id": f"api-{explicit_source}-{explicit_param}",
                "source": explicit_source, "param": explicit_param,
                "label": f"{explicit_source} · {explicit_param}",
            }
            # An explicit API voice is a per-job choice, including when global
            # automatic/dual speaker selection is enabled.
            overrides["voice_mode"] = "manual"
            safe_overrides["voice_mode"] = "manual"
        elif "manual_voice" not in voice_config:
            voice_config["manual_voice"] = dict(voice_selection.selected())
        # The global QC policy is read live at execution; only an explicit
        # per-job choice is frozen into the snapshot.
        explicit_qc_policy = overrides.get("qc_policy")
        pinned_qc = (
            {"qc_policy": explicit_qc_policy, "qc_policy_source": "job"}
            if explicit_qc_policy else {}
        )
        from subtitle_blur import freeze_subtitle_settings
        import canvas_settings
        visual_defaults = dict(canvas_settings.get_canvas_settings())
        if os.getenv("V1_SUBTITLE_COVER_STYLE"):
            visual_defaults["sub_cover_style"] = os.environ["V1_SUBTITLE_COVER_STYLE"]
        subtitle_settings = freeze_subtitle_settings(overrides, visual_defaults)
        snapshot = freeze_job_config(
            video_name=path.name, job_id=job_id, routing_snapshot=routing, workspace_path=self.workspace,
            overrides={**safe_overrides, **subtitle_settings,
                "video_mode": resolved_mode, "planned_pipeline": planned,
                "planned_models": planned_models, "policy_version": 2,
                "separation_mode": planned["separation_model"],
                "asr_model": planned["asr_model"],
                "voice_mode": overrides.get("voice_mode") or ("auto" if voice_config.get("enabled") else "manual"),
                "voice_config": voice_config, "feature_flags": flags,
                **pinned_qc,
            })
        effective_cfg = snapshot["effective_config"]
        # Config services may filter unknown preset fields; retain job-owned policy.
        effective_cfg.update({
            **subtitle_settings,
            "policy_version": 2, "planned_pipeline": planned, "planned_models": planned_models,
            "video_mode": resolved_mode, "separation_mode": planned["separation_model"],
            "voice_config": voice_config, "feature_flags": flags,
            "voice_mode": overrides.get("voice_mode") or ("auto" if voice_config.get("enabled") else "manual"),
            **pinned_qc,
            "visual_sub_to_speech": bool(
                overrides.get("visual_sub_to_speech")
                if overrides.get("visual_sub_to_speech") is not None
                else flags.get("V1_VISUAL_SUB_TO_SPEECH", False)
            ),
        })
        manifest = self.manifest_manager.get_or_create_manifest(
            job_id, path, resolved_mode, effective_cfg, routing)
        if manifest.effective_config.get("policy_version") != 2:
            # Old checkpoints lack OCR/TTS sidecars. Keep files but recompute these stages.
            manifest.effective_config = effective_cfg
            manifest.routing_snapshot = routing
            manifest.video_mode = resolved_mode
            for stage in STAGES_ORDER[3:]:
                manifest.stages[stage]["status"] = "pending"
            manifest.status = "created"
            self.manifest_manager.save_manifest(manifest)
        return {"job_id": job_id, "manifest": manifest.to_dict(), "routing": routing,
                "planned_pipeline": planned, "planned_models": planned_models,
                "effective_config": effective_cfg}

    def check_resumability(self, job_id: str) -> Tuple[bool, str, Dict[str, Any]]:
        """Kiểm tra khả năng khôi phục (Resume) an toàn của một job sau crash/mất mạng."""
        return self.manifest_manager.verify_manifest_resumability(job_id)

    def _check_gpu_guard(self, stage_name: str) -> None:
        """
        Fail-Closed GPU Guard: Đảm bảo các tác vụ AI nặng bắt buộc có GPU CUDA,
        không bao giờ âm thầm fallback sang CPU.
        """
        try:
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError(
                    f"GPU Guard từ chối thực thi stage '{stage_name}': GPU CUDA:0 không khả dụng. "
                    f"Chính sách an toàn V1 nghiêm cấm fallback CPU âm thầm cho các tác vụ AI nặng."
                )
        except ImportError:
            raise RuntimeError(f"GPU Guard: Không tìm thấy PyTorch CUDA để chạy stage '{stage_name}'.")

    def _calculate_stage_timeout(self, stage: str, video_duration_s: float, mode: str) -> int:
        """Tính toán timeout an toàn theo thời lượng video và mô hình của stage."""
        dur = max(10.0, float(video_duration_s))
        if stage == "extract_audio":
            return max(30, min(300, int(dur * 0.2) + 30))
        elif stage == "separate_vocals":
            # BS-RoFormer RTF ~1.2x + thời gian nạp trọng số mô hình vào VRAM (~60s)
            return max(450, min(3600, int(dur * 3.5) + 300))
        elif stage == "speech_asr":
            return max(120, min(1800, int(dur * 2.0) + 120))
        elif stage == "visual_ocr":
            # 4K / 60fps cần thêm thời gian OCR frame processing
            return max(300, min(5400, int(dur * 4.5) + 300))
        elif stage == "ai_translation":
            # Gemini dịch và dự trù thời gian backoff retry
            return max(300, min(2400, int(dur * 4.0) + 300))
        elif stage == "ai_tts":
            # CapCut TTS: Cho phép dồi dào thời gian để hoàn thành mọi lượt retry câu lẻ (tránh ngắt ở 500s)
            return max(300, min(3600, int(dur * 3.0) + 300))
        elif stage == "audio_mixing":
            return max(60, min(600, int(dur * 0.5) + 60))
        elif stage == "video_rendering":
            # NVENC render 4K/high FPS cần đủ thời gian an toàn không bị timeout sớm
            if dur <= 420:
                return max(900, min(5400, int(dur * 15.0) + 600))
            # Video dài (VD 52 phút) từng bị ngắt cứng ở 5400s dù FFmpeg vẫn đang chạy.
            # Cho trần co giãn theo thời lượng (~3x realtime), tối đa 4 giờ.
            return max(5400, min(14400, int(dur * 3.0) + 600))
        return 900

    async def execute_job(
        self,
        video_path: str | Path,
        job_id: str,
        output_dir: Optional[str | Path] = None,
        delivery_path: Optional[str | Path] = None,
        user_mode: str = "auto",
        overrides: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, int, int, float, str, Dict[str, Any]], Any]] = None,
        resume_if_possible: bool = True,
        stop_checker: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        THỰC THI TRỌN VẸN PIPELINE LỒNG TIẾNG CHO TOOL V1:
        1. Nhận input, lập kế hoạch & đóng băng cấu hình bất biến.
        2. Kiểm tra Resume an toàn (bỏ qua các stage đã hoàn thành có artifacts hash hợp lệ).
        3. Thực thi lần lượt 8 stages với GPU enforcement và timeout cụ thể.
        4. Kiểm định chất lượng Quality Gate 8 nhóm.
        5. Xuất bản (Publish/Replace) nguyên tử và gửi thông báo.
        """
        c = self._prepare_execution(
            video_path, job_id, output_dir, delivery_path, user_mode, overrides, resume_if_possible)
        if isinstance(c, dict):
            return c
        job_id, resolved_mode = c.job_id, c.resolved_mode
        total_steps = len(STAGES_ORDER)

        async def notify(stage: str, step: int, pct: float, msg: str, details: Optional[Dict[str, Any]] = None):
            if progress_callback:
                d = details or {}
                d["video_mode"] = resolved_mode
                d["job_id"] = job_id
                try:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(stage, step, total_steps, pct, msg, d)
                    else:
                        progress_callback(stage, step, total_steps, pct, msg, d)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Progress notification failed; keep processing job %s", job_id, exc_info=True)

        def check_stop():
            from batch_processor import BatchStopRequested
            import job_tracker
            import stop_service
            if (stop_checker and stop_checker()) or job_tracker.is_stop_requested() or stop_service.is_stop_requested(self.workspace):
                raise BatchStopRequested(f"Job {job_id} đã bị dừng theo yêu cầu.")

        async def pause_checkpoint():
            try:
                import video_pause
                await video_pause.checkpoint()
            except Exception:
                pass

        async def run_stage(awaitable, timeout):
            return await await_stage(awaitable, timeout, check_stop)

        c.notify, c.check_stop = notify, check_stop
        c.pause_checkpoint, c.run_stage = pause_checkpoint, run_stage
        self._restore_execution_checkpoint(c, resume_if_possible)
        original_audio = await self._stage_extract(c)
        vocals_audio, no_vocals_audio = await self._stage_separate(c, original_audio)
        srt_original, srt_segments, asr_was_empty = await self._stage_asr(c, vocals_audio, original_audio)
        srt_segments, floating_segments, vid_w, vid_h, main_y_pct = await self._stage_ocr(
            c, srt_original, srt_segments, asr_was_empty)
        translated_segments, v1_script_mode = await self._stage_translate(
            c, srt_segments, original_audio=original_audio, vocals_audio=vocals_audio)
        translated_segments, dubbing_audio_files, ass_path = await self._stage_tts(
            c, original_audio, vocals_audio, srt_segments, translated_segments,
            floating_segments, vid_w, vid_h, main_y_pct, v1_script_mode)
        mixed_audio = await self._stage_mix(c, original_audio, no_vocals_audio, asr_was_empty, dubbing_audio_files)
        final_video, temp_render_video = await self._stage_render(c, ass_path, mixed_audio, main_y_pct)
        return await self._stage_qc_and_deliver(
            c, final_video, temp_render_video, translated_segments, dubbing_audio_files, mixed_audio, ass_path)

    def _prepare_execution(self, video_path, job_id, output_dir, delivery_path, user_mode, overrides, resume_if_possible):
        v_path = Path(video_path).resolve()
        if not v_path.is_file():
            raise FileNotFoundError(f"Video đầu vào không tồn tại: {v_path}")

        t_job_start = time.time()
        try:
            from v1_gpu_gatekeeper import set_gpu_context_id
            set_gpu_context_id(f"job_{job_id}")
        except Exception:
            pass

        overrides = dict(overrides or {})
        safe_stem = v_path.stem.rstrip(". ") or "video"
        raw_out = output_dir or (self.workspace / safe_stem)
        requested_out_dir = Path(str(raw_out).rstrip(". ")).resolve()
        # The same input may be retried with a fresh queue ID. Reuse only a
        # non-completed manifest with verified source and the same working folder.
        if resume_if_possible and not self.manifest_manager.load_manifest(job_id):
            candidates = sorted(self.manifest_manager.manifest_dir.glob("*.manifest.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
            for candidate in candidates:
                old = self.manifest_manager.load_manifest(candidate.name.removesuffix(".manifest.json"))
                if (old and old.status != "completed" and Path(old.video_path).resolve() == v_path
                        and old.effective_config.get("policy_version") == 2
                        and old.effective_config.get("output_dir") == str(requested_out_dir)
                        and old.video_fingerprint == fingerprint(v_path)):
                    job_id = old.job_id
                    break
        plan_info = self.plan_job(v_path, job_id, user_mode, overrides)
        manifest = self.manifest_manager.load_manifest(job_id)
        if not manifest:
            raise RuntimeError(f"Không thể khởi tạo manifest cho job: {job_id}")

        if manifest.status == "completed" and manifest.output_video_path:
            published = Path(manifest.output_video_path)
            rendered = manifest.stages.get("video_rendering", {}).get("artifacts", {}).get("temp_render", {})
            if published.is_file() and rendered.get("sha256") == fingerprint(published):
                return {"status": "success", "job_id": job_id, "video_mode": manifest.video_mode,
                        "final_video": str(published), "manifest_file": str(self.manifest_manager._manifest_file(job_id)),
                        "qc_status": "PREVIOUSLY_PUBLISHED", "qc_report": {}, "reused_completed": True}

        import tempfile
        from disk_guard import ensure_free_space
        ensure_free_space(
            self.workspace, requested_out_dir, delivery_path,
            os.getenv("AUTODUB_OUTPUT_DIR"), tempfile.gettempdir(),
            purpose=f"xử lý {v_path.name}",
        )

        resolved_mode = manifest.video_mode
        planned = self._plan_info_from_manifest(manifest)["planned_pipeline"]
        planned.setdefault("asr_chunking", resolved_mode != "SHORT")
        planned.setdefault("asr_chunk_size_s", 240.0)
        effective_cfg = manifest.effective_config
        if effective_cfg.get("visual_sub_to_speech") is None:
            effective_cfg["visual_sub_to_speech"] = bool(
                planned.get(
                    "visual_sub_to_speech",
                    (effective_cfg.get("feature_flags") or {}).get("V1_VISUAL_SUB_TO_SPEECH", False),
                )
            )
        visual_sub_to_speech = bool(effective_cfg.get("visual_sub_to_speech"))

        self._record_execution_router(manifest, effective_cfg, planned, resolved_mode)

        # Legacy snapshots did not freeze these fields. Capture missing runtime
        # defaults exactly once without rerouting or changing existing models.
        # We cannot reconstruct settings that were never recorded historically.
        if not effective_cfg.get("feature_flags") or not effective_cfg.get("voice_config"):
            from v1_job_policy import capture_job_settings
            captured = capture_job_settings(self.workspace)
            for field in ("feature_flags", "voice_config", "voice_mode"):
                if not effective_cfg.get(field):
                    effective_cfg[field] = overrides.get(field) or captured[field]
            effective_cfg["legacy_runtime_defaults_captured"] = True
            logger.warning("Legacy job %s lacked runtime snapshot; captured missing settings once", job_id)

        # Legacy snapshots did not freeze cover style. Preserve the historical
        # solid default rather than switching an in-flight job to live settings.
        from subtitle_blur import freeze_subtitle_settings
        effective_cfg.update(freeze_subtitle_settings(effective_cfg))

        # Thư mục làm việc cho job
        base_name = v_path.stem
        out_dir = requested_out_dir
        previous_out_dir = effective_cfg.get("output_dir")
        if previous_out_dir and Path(previous_out_dir).resolve() != out_dir:
            raise ValueError("Job đang resume phải dùng nguyên thư mục làm việc đã đóng băng.")
        effective_cfg["output_dir"] = str(out_dir)
        self.manifest_manager.save_manifest(manifest)
        out_dir.mkdir(parents=True, exist_ok=True)

        return _ExecutionContext(
            v_path=v_path, job_id=job_id, out_dir=out_dir, manifest=manifest,
            effective_cfg=effective_cfg, planned=planned, resolved_mode=resolved_mode,
            overrides=overrides, delivery_path=delivery_path, t_job_start=t_job_start,
            base_name=base_name, visual_sub_to_speech=visual_sub_to_speech,
        )

    def _record_execution_router(self, manifest, effective_cfg, planned, resolved_mode):
        # Ghi nhận thông tin bộ điều phối video (SML Pipeline Router) vào job_tracker
        routing = getattr(manifest, "routing_snapshot", {}) or {}
        req_mode = routing.get("requested_mode") or effective_cfg.get("video_mode") or "AUTO"
        is_esc = routing.get("is_escalated", False)
        esc_reasons = routing.get("escalation_reasons", [])
        dur_s = routing.get("metadata", {}).get("duration_s", 0)

        lbl = f"AUTO · {resolved_mode}" if str(req_mode).upper() == "AUTO" else f"Thủ công · {resolved_mode}"
        if resolved_mode == "SHORT": lbl += " (≤ 7m)"
        elif resolved_mode == "LONG": lbl += " (> 7m)"
        elif resolved_mode == "MEDIUM": lbl += " (Chuyển tiếp)"

        try:
            import job_tracker
            job_tracker.record_router_info({
                "resolved_mode": resolved_mode,
                "requested_mode": req_mode,
                "is_escalated": is_esc,
                "escalation_reasons": esc_reasons,
                "duration_s": dur_s,
                "router_label": lbl,
                "separation_model": planned.get("separation_model", "roformer"),
                "asr_model": planned.get("asr_model", "whisper_turbo"),
            })
        except Exception as rh_err:
            logger.warning("Không thể ghi nhận router_info vào job_tracker: %s", rh_err)

    def _restore_execution_checkpoint(self, c, resume_if_possible):
        # Probe metadata, not full decoded audio, for long videos.
        c.video_dur_s = float(c.manifest.routing_snapshot["metadata"]["duration_s"])
        try:
            import soundfile as sf
            temp_probe = c.out_dir / "original.wav"
            if temp_probe.is_file():
                c.video_dur_s = sf.info(str(temp_probe)).duration
        except Exception:
            pass

        # Kiểm tra Resume
        c.can_resume = False
        resume_stage = STAGES_ORDER[0]
        c.verified_stages = []
        if resume_if_possible:
            c.can_resume, resume_stage, resume_details = self.manifest_manager.verify_manifest_resumability(c.job_id)
            c.verified_stages = resume_details.get("verified_stages", [])
            if c.visual_sub_to_speech and "speech_asr" in c.verified_stages:
                try:
                    source_policy = load_payload(c.out_dir / "asr.json").get("source_policy_version")
                except (OSError, ValueError, KeyError, TypeError):
                    source_policy = None
                if source_policy != TRANSCRIPT_SOURCE_POLICY_VERSION:
                    asr_pos = STAGES_ORDER.index("speech_asr")
                    c.verified_stages = [s for s in c.verified_stages if STAGES_ORDER.index(s) < asr_pos]
                    logger.info("[RESUME] Recompute old transcript selection; retain extracted/separated audio")
            if c.effective_cfg.get("speech_timing_version") != 3:
                tts_pos = STAGES_ORDER.index("ai_tts")
                c.verified_stages = [s for s in c.verified_stages if STAGES_ORDER.index(s) < tts_pos]
            from v1_ocr_geometry import OCR_GEOMETRY_VERSION
            if "visual_ocr" in c.verified_stages:
                try:
                    cached_geometry = load_payload(c.out_dir / "ocr.json").get("geometry_version")
                except (OSError, ValueError, KeyError, TypeError):
                    cached_geometry = None
                if cached_geometry != OCR_GEOMETRY_VERSION:
                    ocr_pos = STAGES_ORDER.index("visual_ocr")
                    c.verified_stages = [s for s in c.verified_stages if STAGES_ORDER.index(s) < ocr_pos]
                    logger.info('[RESUME] Old OCR geometry: recompute OCR and dependent stages; keep audio/ASR')

        c.artifacts_acc: Dict[str, str] = {}

    async def _stage_extract(self, c):
        # ==================== STAGE 1: EXTRACT AUDIO ====================
        stage_id = "extract_audio"
        original_audio = c.out_dir / "original.wav"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        if should_run:
            await c.notify(stage_id, 1, 10.0, "🎧 Bước 1/9: Đang trích xuất âm thanh gốc...")
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                from video_utils import extract_audio_from_video
                c.check_stop()
                ok = await c.run_stage(
                    asyncio.to_thread(extract_audio_from_video, str(c.v_path), str(original_audio)),
                    timeout=t_out
                )
                if not ok or not original_audio.is_file() or original_audio.stat().st_size < 1000:
                    raise RuntimeError("Trích xuất âm thanh thất bại hoặc file rỗng.")
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"original_audio": str(original_audio)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            logger.info("[RESUME] Bỏ qua stage 1 (extract_audio) do artifact đã được xác thực.")
        c.artifacts_acc["original_audio"] = str(original_audio)

        # Cập nhật thời lượng audio chính xác
        try:
            import soundfile as sf
            c.video_dur_s = sf.info(str(original_audio)).duration
        except Exception:
            pass
        return original_audio

    async def _stage_separate(self, c, original_audio):
        # ==================== STAGE 2: SEPARATE VOCALS ====================
        stage_id = "separate_vocals"
        vocals_audio = c.out_dir / "vocals.wav"
        no_vocals_audio = c.out_dir / "no_vocals.wav"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        if should_run:
            sep_mode = (
                c.effective_cfg.get("separation_mode")
                or c.effective_cfg.get("separation_model")
                or c.planned.get("separation_model")
                or c.planned.get("separator_engine")
                or ("demucs" if c.resolved_mode in ("LONG", "MEDIUM") else "roformer")
            )
            await c.notify(stage_id, 2, 25.0, f"🧠 Bước 2/9: Bóc tách giọng nói khỏi nhạc nền ({sep_mode.upper()})...", {"model": sep_mode})
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                if sep_mode != "bypass":
                    self._check_gpu_guard(stage_id)
                from video_utils import separate_vocals_demucs
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                c.check_stop()
                async with async_gpu_gatekeeper("vocal_separation", timeout_seconds=t_out):
                    v_res, nv_res = await c.run_stage(
                        asyncio.to_thread(
                            separate_vocals_demucs,
                            str(original_audio),
                            str(c.out_dir),
                            separation_mode=sep_mode,
                        ),
                        timeout=t_out
                    )
                vocals_audio = Path(v_res)
                no_vocals_audio = Path(nv_res)
                if not vocals_audio.is_file() or not no_vocals_audio.is_file():
                    raise RuntimeError("Tách âm thanh không xuất đủ file vocals hoặc no_vocals.")
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {
                    "vocals_audio": str(vocals_audio),
                    "no_vocals_audio": str(no_vocals_audio),
                })
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            saved = c.manifest.stages[stage_id]["artifacts"]
            vocals_audio = Path(saved["vocals_audio"]["path"])
            no_vocals_audio = Path(saved["no_vocals_audio"]["path"])
            logger.info("[RESUME] Khôi phục đúng đường dẫn file tách âm đã xác thực.")
        c.artifacts_acc["vocals_audio"] = str(vocals_audio)
        c.artifacts_acc["no_vocals_audio"] = str(no_vocals_audio)
        return vocals_audio, no_vocals_audio

    async def _stage_asr(self, c, vocals_audio, original_audio):
        # ==================== STAGE 3: SPEECH ASR ====================
        stage_id = "speech_asr"
        srt_original = c.out_dir / "original.srt"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        srt_segments = []
        asr_was_empty = False
        visual_ocr_cached = False
        if should_run:
            asr_m = migrate_legacy_asr_model(str(c.planned.get("asr_model", "whisper_turbo")), "job plan")
            if asr_m == "whisper_sensevoice":
                model_display = "Hybrid: Faster-Whisper Turbo + SenseVoice-Small (CTC 60ms)"
            elif asr_m == "sensevoice_small":
                model_display = "SenseVoice-Small (ONNX INT8 Siêu Tốc)"
            else:
                model_display = "Faster-Whisper Large-v3 Turbo"
            await c.notify(stage_id, 3, 40.0, f"🤖 Bước 3/9: {model_display} đang nhận dạng giọng nói...")
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                self._check_gpu_guard(stage_id)
                from ai.v1_asr_isolated import extract_subtitles_isolated as extract_subtitles_whisper
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                c.check_stop()
                # extract_subtitles_whisper tự động điều phối Hybrid/SenseVoice hoặc Faster-Whisper + Chunking
                async with async_gpu_gatekeeper("speech_asr", timeout_seconds=t_out):
                    srt_segments = await c.run_stage(
                        asyncio.to_thread(
                            extract_subtitles_whisper,
                            str(vocals_audio),
                            str(srt_original),
                            original_audio_path=str(original_audio),
                            model_id=c.planned["asr_model"], chunking=c.planned["asr_chunking"],
                            chunk_size_s=c.planned["asr_chunk_size_s"], overlap_s=c.planned["asr_overlap_s"], timeout_s=t_out,
                        ),
                        timeout=t_out
                    )
                asr_was_empty = not srt_segments or not srt_original.is_file()
                if c.visual_sub_to_speech and not asr_was_empty and _looks_like_bgm_hallucination(srt_segments, c.video_dur_s):
                    logger.info(
                        "🎬 Visual mode: bỏ %d câu ASR nghi ảo giác nhạc nền (phủ thoại %.1f%%)",
                        len(srt_segments),
                        100.0 * _merged_speech_coverage(srt_segments, c.video_dur_s) / max(c.video_dur_s, 1.0),
                    )
                    srt_segments = []
                    asr_was_empty = True
                if c.visual_sub_to_speech and asr_was_empty:
                    srt_segments, visual_ocr_cached = await self._stage_visual_transcript(
                        c, srt_segments, asr_was_empty, srt_original, async_gpu_gatekeeper)
                elif asr_was_empty:
                    raise RuntimeError(f"ASR ({model_display}) không nhận dạng được câu thoại nào.")
                save_payload(c.out_dir / "asr.json", segments=srt_segments, asr_was_empty=asr_was_empty,
                             source_policy_version=TRANSCRIPT_SOURCE_POLICY_VERSION,
                             transcript_source="visual_caption" if asr_was_empty else "speech_asr")
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"srt_original": str(srt_original), "asr_data": str(c.out_dir / "asr.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            import srt
            logger.info("[RESUME] Tải lại srt_original đã cache.")
            sidecar = c.out_dir / "asr.json"
            asr_payload = load_payload(sidecar) if sidecar.is_file() else {}
            srt_segments = asr_payload.get("segments") or list(srt.parse(srt_original.read_text(encoding="utf-8")))
            asr_was_empty = bool(asr_payload.get("asr_was_empty", False))
            ocr_sidecar = c.out_dir / "ocr.json"
            if c.visual_sub_to_speech and asr_was_empty and ocr_sidecar.is_file():
                cached_ocr = load_payload(ocr_sidecar)
                visual_ocr_cached = bool(cached_ocr.get("visual_asr_reuse"))
                if visual_ocr_cached:
                    asr_was_empty = bool(cached_ocr.get("asr_was_empty", asr_was_empty))
        c.artifacts_acc["srt_original"] = str(srt_original)
        return srt_original, srt_segments, asr_was_empty

    async def _stage_ocr(self, c, srt_original, srt_segments, asr_was_empty):
        # ==================== STAGE 4: VISUAL OCR ====================
        stage_id = "visual_ocr"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        vid_w, vid_h, main_y_pct = 1080, 1920, 0.88
        floating_segments = []
        cached_ocr_payload = None
        if c.visual_sub_to_speech and asr_was_empty:
            try:
                candidate_ocr = load_payload(c.out_dir / "ocr.json")
            except (OSError, ValueError, KeyError, TypeError):
                candidate_ocr = None
            from v1_ocr_geometry import OCR_GEOMETRY_VERSION
            if (candidate_ocr and candidate_ocr.get("visual_asr_reuse")
                    and candidate_ocr.get("source_policy_version") == TRANSCRIPT_SOURCE_POLICY_VERSION
                    and candidate_ocr.get("geometry_version") == OCR_GEOMETRY_VERSION):
                cached_ocr_payload = candidate_ocr
        if cached_ocr_payload is not None:
            srt_segments = cached_ocr_payload.get("segments") or srt_segments
            floating_segments = cached_ocr_payload.get("floating_segments") or []
            vid_w = int(cached_ocr_payload.get("width", vid_w))
            vid_h = int(cached_ocr_payload.get("height", vid_h))
            main_y_pct = float(cached_ocr_payload.get("main_y_pct", main_y_pct))
            if should_run:
                await c.notify(
                    stage_id, 4, 55.0,
                    "👀 Bước 4/9: Tái sử dụng kết quả quét phụ đề từ bước 3 (không quét lại)...",
                )
                self.manifest_manager.record_stage_start(c.job_id, stage_id)
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"ocr_data": str(c.out_dir / "ocr.json")})
        elif should_run:
            flags = dict(c.overrides.get("feature_flags") or get_feature_flags(self.workspace))
            enable_smart = flags.get("V1_SMART_SKIP_OCR", True)
            ocr_strat = c.overrides.get("ocr_strategy") or c.planned.get("ocr_strategy", "auto")
            if c.overrides.get("ocr_strategy"):
                ocr_strat = c.overrides["ocr_strategy"]
            elif enable_smart:
                ocr_strat = "smart_skip"
            elif not ocr_strat or ocr_strat == "auto":
                short_thresh = float(os.getenv("V1_SHORT_MAX_SECONDS", "420.0"))
                if c.video_dur_s > 0 and c.video_dur_s <= short_thresh:
                    ocr_strat = "full"
                else:
                    ocr_strat = "smart_skip"
            await c.notify(stage_id, 4, 55.0, f"👀 Bước 4/9: Quét vị trí phụ đề chữ cứng (OCR {ocr_strat})...", {"ocr_strategy": ocr_strat})
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                from ocr_utils import perform_video_ocr, release_ocr_reader
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                c.check_stop()
                gemini_key = c.overrides.get("api_key") or os.getenv("GEMINI_API_KEY", "")
                ocr_metrics = {}
                # Crash between stages 3/4: re-scan from an empty timeline so the
                # cached visual cues are not appended to themselves.
                ocr_input = [] if (c.visual_sub_to_speech and asr_was_empty) else srt_segments
                async with async_gpu_gatekeeper("visual_ocr", timeout_seconds=t_out):
                    ocr_result, vid_w, vid_h, main_y_pct = await c.run_stage(
                        asyncio.to_thread(
                            perform_video_ocr,
                            str(c.v_path),
                            target_lang=c.effective_cfg.get("target_lang", "vi"),
                            sample_rate=1.0,
                            api_key=gemini_key,
                            srt_segments=ocr_input,
                            ocr_strategy=ocr_strat,
                            adaptive=False,
                            geometry_report_path=str(c.out_dir / 'ocr_geometry_report.json'),
                            strict_geometry=True,
                            visual_sub_to_speech=bool(c.visual_sub_to_speech and asr_was_empty),
                            _source_duration=c.video_dur_s,
                            metrics=ocr_metrics,
                        ),
                        timeout=t_out
                    )
                from v1_ocr_geometry import OCR_GEOMETRY_VERSION

                if c.visual_sub_to_speech and asr_was_empty and ocr_result:
                    ocr_result = visual_transcript_candidates(ocr_result)
                    if not _visual_ocr_transcript_usable(ocr_result, c.video_dur_s):
                        raise RuntimeError("Không tìm thấy phụ đề động hợp lệ để lồng tiếng")
                    srt_segments = ocr_result
                    floating_segments = []
                    from ai.transcription import save_srt
                    save_srt(srt_segments, str(srt_original))
                else:
                    floating_segments = ocr_result
                save_payload(c.out_dir / "ocr.json", geometry_version=OCR_GEOMETRY_VERSION, segments=srt_segments,
                             source_policy_version=TRANSCRIPT_SOURCE_POLICY_VERSION,
                             floating_segments=floating_segments, width=vid_w, height=vid_h, main_y_pct=main_y_pct,
                             visual_asr_reuse=bool(c.visual_sub_to_speech and asr_was_empty),
                             asr_was_empty=asr_was_empty,
                             detected_watermarks=ocr_metrics.get("detected_watermarks", []))
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"ocr_data": str(c.out_dir / "ocr.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
            finally:
                if "release_ocr_reader" in locals():
                    release_ocr_reader()
                elif c.ocr_reader_releaser is not None:
                    c.ocr_reader_releaser()
        else:
            saved = load_payload(c.out_dir / "ocr.json")
            srt_segments, floating_segments = saved["segments"], saved["floating_segments"]
            vid_w, vid_h, main_y_pct = saved["width"], saved["height"], saved["main_y_pct"]

        self._align_source_subtitles(srt_segments)
        return srt_segments, floating_segments, vid_w, vid_h, main_y_pct

    async def _stage_translate(self, c, srt_segments, original_audio=None, vocals_audio=None):
        # ==================== STAGE 5: AI TRANSLATION ====================
        stage_id = "ai_translation"
        srt_translated = c.out_dir / "translated.srt"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        translated_segments = []
        v1_script_mode = c.effective_cfg.get("script_mode") or c.overrides.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
        if should_run:
            configured_gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
            try:
                import job_tracker
                job_tracker.record_active_translation_model(configured_gemini_model)
            except Exception:
                pass
            script_badge = " (Kịch bản: Hài hước)" if str(v1_script_mode).lower() in ("humorous", "hai_huoc", "haihuoc", "comedy") else ""
            await c.notify(stage_id, 5, 65.0, f"🌐 Bước 5/9: Dịch phụ đề chuẩn ngữ cảnh bằng {configured_gemini_model}{script_badge} ({len(srt_segments)} câu)...", {"gemini_model": configured_gemini_model, "script_mode": v1_script_mode})
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                from ai.translation import translate_subtitles
                from ai.transcription import save_srt

                # Early Voice Lock trước bước dịch (chỉ chạy khi bật tự động nhận diện giọng)
                voice_lock_info = None
                v_mode = str(c.effective_cfg.get("voice_mode") or "").strip().lower()
                v_cfg = c.effective_cfg.get("voice_config") or {}
                run_early_voice_lock = bool(
                    srt_segments
                    and (vocals_audio or original_audio)
                    and not c.visual_sub_to_speech
                    and v_mode != "manual"
                    and (v_cfg.get("enabled") or v_mode in ("auto", "dialogue", "smart_dialogue", "dual"))
                )
                if run_early_voice_lock:
                    try:
                        from ai.v1_auto_voice import decide_video_voice
                        voice_lock_info = await asyncio.to_thread(
                            decide_video_voice,
                            out_dir=str(c.out_dir),
                            srt_segments=srt_segments,
                            vocals_path=str(vocals_audio) if vocals_audio else None,
                            original_audio_path=str(original_audio) if original_audio else None,
                            video_path=str(c.v_path),
                            voice_mode=c.effective_cfg.get("voice_mode"),
                            workspace=str(self.workspace),
                            voice_config=c.effective_cfg["voice_config"],
                            job_id=c.job_id,
                            visual_sub_to_speech=bool(c.visual_sub_to_speech and asr_was_empty),
                        )
                    except Exception as vl_exc:
                        logger.warning("[TRANSLATE] Early voice lock fallback sang speaker='unknown': %s", vl_exc)

                # Chuẩn bị siêu dữ liệu từng câu ({id, speaker, max_words, ocr_hint}) khớp 1-1 với texts
                cue_meta = []
                if srt_segments:
                    try:
                        from v1_timing_solver import calculate_timing_windows
                        windows = calculate_timing_windows(srt_segments, total_audio_duration=c.video_dur_s)
                    except Exception:
                        windows = {}
                    seg_voices = (voice_lock_info or {}).get("segment_voices") or {}
                    top_gender = str((voice_lock_info or {}).get("detected_gender") or "unknown").lower()
                    non_empty_segs = [s for s in srt_segments if getattr(s, "content", None)]
                    for pos, seg in enumerate(non_empty_segs):
                        seg_idx = int(getattr(seg, "index", pos + 1))
                        sv = seg_voices.get(str(seg_idx)) or seg_voices.get(seg_idx) or {}
                        g = str(sv.get("gender") or ("unknown" if top_gender in ("dual", "manual", "") else top_gender)).lower()
                        speaker = g if g in ("male", "female") else "unknown"
                        win_s = float(windows.get(seg_idx, 2.0) or 2.0)
                        max_words = max(3, int(round(max(0.6, win_s - 0.05) * 3.6 * 1.15)))
                        bb = getattr(seg, "best_block", None)
                        bb_text = (bb.get("text") if isinstance(bb, dict) else getattr(bb, "text", "")) if bb else ""
                        cue_meta.append({
                            "id": pos + 1,
                            "index": seg_idx,
                            "speaker": speaker,
                            "max_sec": round(win_s, 2),
                            "max_words": max_words,
                            "ocr_hint": str(bb_text or "").strip()[:80],
                        })

                gemini_key = c.overrides.get("api_key") or ""
                c.check_stop()
                translated_segments = await c.run_stage(
                    asyncio.to_thread(
                        translate_subtitles,
                        srt_segments,
                        c.effective_cfg.get("target_lang", "vi"),
                        api_key=gemini_key,
                        video_path=str(c.v_path),
                        strict=True,
                        script_mode=v1_script_mode,
                        cue_meta=cue_meta or None,
                        folder_scope=str(c.v_path.parent),
                    ),
                    timeout=t_out
                )
                save_srt(translated_segments, str(srt_translated))
                try:
                    import job_tracker
                    status_models = job_tracker.get_status().get("translation_models", [])
                    actual_model = status_models[-1] if status_models else configured_gemini_model
                    job_tracker.record_active_translation_model(actual_model)
                except Exception:
                    pass
                save_payload(c.out_dir / "translation.json", segments=translated_segments)
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"srt_translated": str(srt_translated), "translation_data": str(c.out_dir / "translation.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            import srt
            logger.info("[RESUME] Tải lại srt_translated đã cache.")
            translated_segments = load_payload(c.out_dir / "translation.json")["segments"]
        c.artifacts_acc["srt_translated"] = str(srt_translated)
        return translated_segments, v1_script_mode

    async def _stage_tts(self, c, original_audio, vocals_audio, srt_segments, translated_segments, floating_segments, vid_w, vid_h, main_y_pct, v1_script_mode):
        # ==================== STAGE 6: AI TTS & DUBBING ====================
        stage_id = "ai_tts"
        dubbing_dir = c.out_dir / "dubbing"
        ass_path = c.out_dir / "final.ass"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        from subtitle_blur import BLUR_RENDER_VERSION, freeze_subtitle_settings
        ass_signature = {"version": BLUR_RENDER_VERSION,
                         "settings": freeze_subtitle_settings(c.effective_cfg),
                         "font": [c.effective_cfg.get("font_name", "Arial"),
                                  c.effective_cfg.get("font_color", "&H00000000"),
                                  c.effective_cfg.get("font_weight", 2)]}
        dubbing_audio_files = []
        if should_run:
            await c.notify(stage_id, 6, 75.0, "Dang long tieng (TTS)...")
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                from ai.v1_auto_voice import decide_video_voice
                from ai.v1_voice_isolated import generate_dubbing_audio_isolated as generate_dubbing_audio
                import importlib
                import ass_utils
                importlib.reload(ass_utils)
                from ass_utils import sync_and_clamp_subtitles, generate_ass_file

                # Khóa giọng đọc
                voice_lock_info = await asyncio.to_thread(
                    decide_video_voice,
                    out_dir=str(c.out_dir),
                    srt_segments=srt_segments,
                    vocals_path=str(vocals_audio),
                    original_audio_path=str(original_audio),
                    video_path=str(c.v_path),
                    voice_mode=c.effective_cfg.get("voice_mode"),
                    workspace=str(self.workspace),
                    voice_config=c.effective_cfg["voice_config"], job_id=c.job_id,
                    visual_sub_to_speech=c.visual_sub_to_speech,
                )
                v_source = voice_lock_info["voice_source"]
                v_param = voice_lock_info["voice_param"]
                v_label = voice_lock_info["voice_label"]
                seg_voices = voice_lock_info.get("segment_voices")

                await c.notify(stage_id, 6, 75.0, f"🗣️ Bước 6/9: Lồng tiếng AI CapCut ({v_label}, 6 luồng)...", {"voice": v_label})
                c.check_stop()

                gemini_key = c.overrides.get("api_key") or os.getenv("GEMINI_API_KEY", "")
                dubbing_audio_files = await c.run_stage(
                    generate_dubbing_audio(
                        translated_segments,
                        str(dubbing_dir),
                        voice_source=v_source,
                        voice_param=v_param,
                        api_key=gemini_key,
                        video_duration=c.video_dur_s,
                        segment_voices=seg_voices,
                        tts_workers=c.effective_cfg["feature_flags"]["V1_TTS_WORKERS"],
                        max_natural_speed=c.effective_cfg["feature_flags"]["V1_MAX_NATURAL_SPEED"],
                        timeout_s=t_out,
                        script_mode=v1_script_mode,
                    ),
                    timeout=t_out
                )

                if len(dubbing_audio_files) != len(translated_segments) or not dubbing_audio_files:
                    raise RuntimeError("TTS returned incomplete segment audio; checkpoint retained for retry.")
                for dub in dubbing_audio_files:
                    dur = float(dub.get("duration", 0) or dub.get("actual_audio_duration", 0))
                    if "duration" not in dub:
                        dub["duration"] = dur
                    if not Path(dub.get("path", "")).is_file() or dur <= 0:
                        raise RuntimeError("TTS produced missing or zero-duration audio.")

                # Ghi nhận các segment TTS đã hoàn thành vào manifest
                for d in dubbing_audio_files:
                    if d and "index" in d:
                        self.manifest_manager.record_tts_segment_done(c.job_id, int(d["index"]))

                # Đồng bộ timing và chống đè câu
                translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

                # Tạo file phụ đề ASS chuẩn theo kích thước video nguồn để khớp 100% tọa độ OCR
                sub_cover_style = c.effective_cfg.get("sub_cover_style", "solid_box")
                await asyncio.to_thread(
                    generate_ass_file,
                    translated_segments,
                    floating_segments,
                    str(ass_path),
                    play_res_x=vid_w,
                    play_res_y=vid_h,
                    main_y_pct=main_y_pct,
                    font_name=c.effective_cfg.get("font_name", "Arial"),
                    font_color=c.effective_cfg.get("font_color", "&H00000000"),
                    font_weight=c.effective_cfg.get("font_weight", 2),
                    cover_style=sub_cover_style,
                    blur_settings=c.effective_cfg,
                    source_segments=srt_segments,
                )

                save_payload(c.out_dir / "tts.json", segments=translated_segments, dubs=dubbing_audio_files)
                tts_artifacts = {"tts_data": str(c.out_dir / "tts.json"), "ass_path": str(ass_path),
                                 "voice_lock": str(c.out_dir / "voice_lock.json")}
                for i, dub in enumerate(dubbing_audio_files):
                    tts_artifacts[f"audio_{i}"] = dub["path"]
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, tts_artifacts)
                current = self.manifest_manager.load_manifest(c.job_id)
                current.effective_config["speech_timing_version"] = 3
                current.effective_config["subtitle_ass_signature"] = ass_signature
                c.effective_cfg["subtitle_ass_signature"] = ass_signature
                self.manifest_manager.save_manifest(current)
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            saved = load_payload(c.out_dir / "tts.json")
            translated_segments, dubbing_audio_files = saved["segments"], saved["dubs"]
            if c.effective_cfg.get("subtitle_ass_signature") != ass_signature:
                from ass_utils import generate_ass_file
                await asyncio.to_thread(
                    generate_ass_file, translated_segments, floating_segments, str(ass_path),
                    play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct,
                    font_name=c.effective_cfg.get("font_name", "Arial"),
                    font_color=c.effective_cfg.get("font_color", "&H00000000"),
                    font_weight=c.effective_cfg.get("font_weight", 2),
                    cover_style=c.effective_cfg.get("sub_cover_style", "solid_box"),
                    blur_settings=c.effective_cfg, source_segments=srt_segments)
                current = self.manifest_manager.load_manifest(c.job_id)
                current.stages[stage_id]["artifacts"]["ass_path"] = {
                    "path": str(ass_path.resolve()), "size_bytes": ass_path.stat().st_size,
                    "sha256": fingerprint(ass_path)}
                current.effective_config["subtitle_ass_signature"] = ass_signature
                c.effective_cfg["subtitle_ass_signature"] = ass_signature
                self.manifest_manager.save_manifest(current)

        c.artifacts_acc["dubbing_dir"] = str(dubbing_dir)
        c.artifacts_acc["ass_path"] = str(ass_path)
        return translated_segments, dubbing_audio_files, ass_path

    async def _stage_mix(self, c, original_audio, no_vocals_audio, asr_was_empty, dubbing_audio_files):
        # ==================== STAGE 7: AUDIO MIXING ====================
        stage_id = "audio_mixing"
        mixed_audio = c.out_dir / "mixed.wav"
        c.check_stop()
        await c.pause_checkpoint()

        should_run = not (c.can_resume and stage_id in c.verified_stages)
        if should_run:
            await c.notify(stage_id, 7, 85.0, "🎛️ Bước 7/9: Trộn nhạc nền & giọng đọc (Hierarchical Adaptive Mixer)...")
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                from video_utils import mix_audio_pydub
                bgm_vol = c.effective_cfg.get("bgm_volume_db", -2.0)
                dub_vol = c.effective_cfg.get("dubbing_volume_db", 1.0)
                duck_mode = c.effective_cfg.get("ducking_mode", "soft")
                mixer_mode = c.planned.get("mixer_mode", "direct")
                # Silent-source visual mode: the separated "no_vocals" stem would
                # drop the BGM that is the only original audio, so keep original.wav.
                bgm_source = original_audio if (c.visual_sub_to_speech and asr_was_empty) else no_vocals_audio

                c.check_stop()
                if mixer_mode == "hierarchical" or c.resolved_mode in ("LONG", "MEDIUM") or len(dubbing_audio_files) > 100:
                    try:
                        from v1_hierarchical_mixer import mix_hierarchical_audio
                        await c.run_stage(
                            asyncio.to_thread(
                                mix_hierarchical_audio,
                                str(bgm_source),
                                dubbing_audio_files,
                                str(mixed_audio),
                                base_bgm_gain_db=bgm_vol,
                                base_voice_gain_db=dub_vol,
                                ducking_mode=duck_mode,
                            ),
                            timeout=t_out
                        )
                    except Exception:
                        # Do not allocate the entire long video after a bounded mixer failure.
                        raise
                else:
                    await c.run_stage(
                        asyncio.to_thread(
                            mix_audio_pydub,
                            str(bgm_source),
                            dubbing_audio_files,
                            str(mixed_audio),
                            original_volume_db=bgm_vol,
                            dubbing_volume_db=dub_vol,
                            ducking_mode=duck_mode,
                            explicit=True,
                        ),
                        timeout=t_out
                    )
                if not mixed_audio.is_file() or mixed_audio.stat().st_size < 1000:
                    raise RuntimeError("Trộn âm thanh thất bại hoặc file mixed.wav rỗng.")
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"mixed_audio": str(mixed_audio)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
        else:
            logger.info("[RESUME] Bỏ qua stage 7 (audio_mixing).")
        c.artifacts_acc["mixed_audio"] = str(mixed_audio)
        return mixed_audio

    async def _stage_render(self, c, ass_path, mixed_audio, main_y_pct):
        # ==================== STAGE 8: VIDEO RENDERING ====================
        stage_id = "video_rendering"
        final_video = c.out_dir / f"final_{c.base_name}.mp4"
        temp_render_video = c.out_dir / f"final_{c.base_name}.tmp.mp4"
        c.check_stop()
        await c.pause_checkpoint()

        from subtitle_blur import BLUR_RENDER_VERSION, freeze_subtitle_settings
        render_signature = {"version": BLUR_RENDER_VERSION, "ass_sha256": fingerprint(ass_path),
                            "settings": freeze_subtitle_settings(c.effective_cfg)}
        cached_signature = c.effective_cfg.get("subtitle_render_signature")
        should_run = not (c.can_resume and stage_id in c.verified_stages
                          and cached_signature == render_signature)
        if should_run:
            await c.notify(stage_id, 8, 92.0, "🎬 Bước 8/9: Render video thành phẩm bằng NVENC GPU...")
            self.manifest_manager.record_stage_start(c.job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, c.video_dur_s, c.resolved_mode)
            try:
                self._check_gpu_guard(stage_id)
                from video_utils import process_video
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                c.check_stop()
                canvas_cfg = {}
                try:
                    import canvas_settings
                    canvas_cfg = canvas_settings.get_canvas_settings() or {}
                except Exception:
                    canvas_cfg = {}
                sub_cover_style = c.effective_cfg.get("sub_cover_style", "solid_box")
                delogo_mode = str(
                    c.effective_cfg.get("delogo_mode")
                    or c.overrides.get("delogo_mode")
                    or os.getenv("V1_DELOGO_MODE")
                    or canvas_cfg.get("delogo_mode")
                    or "off"
                ).strip().lower()
                delogo_boxes = []
                if delogo_mode == "smart":
                    try:
                        ocr_sidecar = c.out_dir / "ocr.json"
                        if ocr_sidecar.is_file():
                            ocr_saved = load_payload(ocr_sidecar)
                            if isinstance(ocr_saved, dict):
                                delogo_boxes = ocr_saved.get("detected_watermarks") or []
                        if not delogo_boxes:
                            geo_rep = c.out_dir / "ocr_geometry_report.json"
                            if geo_rep.is_file():
                                geo_saved = load_payload(geo_rep)
                                if isinstance(geo_saved, dict):
                                    delogo_boxes = geo_saved.get("watermarks") or (geo_saved.get("sampling") or {}).get("detected_watermarks") or []
                    except Exception:
                        delogo_boxes = []
                async with async_gpu_gatekeeper("video_rendering", timeout_seconds=t_out):
                    rendered = await c.run_stage(
                        asyncio.to_thread(
                            process_video,
                            str(c.v_path),
                            str(ass_path),
                            str(mixed_audio),
                            str(temp_render_video),
                            main_y_pct=main_y_pct,
                            delogo=delogo_mode if delogo_mode != "off" else False,
                            delogo_mode=delogo_mode,
                            delogo_boxes=delogo_boxes,
                            subtitle_cover_style=sub_cover_style,
                            subtitle_blur_settings=c.effective_cfg,
                        ),
                        timeout=t_out
                    )
                if not rendered or not temp_render_video.is_file() or temp_render_video.stat().st_size < 10240:

                    raise RuntimeError("Render video NVENC thất bại hoặc file xuất xưởng bị lỗi.")
                self.manifest_manager.record_stage_complete(c.job_id, stage_id, {"temp_render": str(temp_render_video)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(c.job_id, stage_id, str(exc))
                raise
            current = self.manifest_manager.load_manifest(c.job_id)
            current.effective_config["subtitle_render_signature"] = render_signature
            c.effective_cfg["subtitle_render_signature"] = render_signature
            self.manifest_manager.save_manifest(current)
        else:
            logger.info("[RESUME] Bỏ qua stage 8 (video_rendering).")
            temp_render_video = Path(c.manifest.stages[stage_id]["artifacts"]["temp_render"]["path"])
        return final_video, temp_render_video

    async def _stage_qc_and_deliver(self, c, final_video, temp_render_video, translated_segments, dubbing_audio_files, mixed_audio, ass_path):
        # ==================== STAGE 9: QUALITY GATE & PUBLISH ====================
        c.check_stop()
        await c.pause_checkpoint()
        await c.notify("quality_gate", 9, 96.0, "🔎 Bước 9/9: Kiểm tra chất lượng video sau render...")
        self.manifest_manager.record_stage_start(c.job_id, "quality_gate")
        qc_target = temp_render_video if temp_render_video.is_file() else final_video
        qc_policy = resolve_qc_policy(c.effective_cfg, c.overrides, self.workspace)
        logger.info("[QUALITY_GATE] Đang thực hiện kiểm tra chất lượng tự động 8 nhóm (policy=%s)...", qc_policy)
        loop = asyncio.get_running_loop()
        phase_progress = {"decode": (0, 0), "ocr": (0, 0)}
        last_progress_at = 0.0

        def qc_progress(event):
            nonlocal last_progress_at
            c.check_stop()
            phase = str(event.get("phase", ""))
            completed = max(0, int(event.get("completed", 0)))
            total = max(0, int(event.get("total", 0)))
            if phase not in phase_progress:
                return
            phase_progress[phase] = (completed, total)
            # Bounded OCR batches interleave with decoding. Track both work
            # counters so a subsequent decode event cannot rewind progress.
            fraction = sum(min(1.0, done / count) if count else 0.0
                           for done, count in phase_progress.values()) / 2.0
            now = time.monotonic()
            if now - last_progress_at < 2.0 and not (total and completed >= total):
                return
            last_progress_at = now
            label = "Trích khung hậu kiểm" if phase == "decode" else "OCR hậu kiểm"
            pct = 96.0 + 3.0 * fraction
            details = {"phase": phase, "completed": completed, "total": total,
                       "stage_fraction": fraction, "recognized": phase_progress["ocr"][0]}
            # QC runs in a worker thread. Deliver ordered progress back through
            # the owning event loop rather than invoking async callbacks there.
            future = asyncio.run_coroutine_threadsafe(c.notify(
                "quality_gate", 9, pct,
                f"🔎 Bước 9/9: {label} ({completed}/{total})...", details), loop)
            try:
                future.result()
            except FutureCancelledError as exc:
                raise asyncio.CancelledError() from exc

        try:
            import importlib
            import v1_quality_gate
            importlib.reload(v1_quality_gate)
            from v1_quality_gate import run_quality_gate

            flags_cfg = c.effective_cfg.get("feature_flags") or {}
            if "V1_QC_VISUAL_PIXEL_CHECK" in flags_cfg:
                qc_pixel_enabled = bool(flags_cfg.get("V1_QC_VISUAL_PIXEL_CHECK"))
            else:
                from v1_feature_flags import get_feature_flags
                qc_pixel_enabled = bool(get_feature_flags(self.workspace).get("V1_QC_VISUAL_PIXEL_CHECK", True))

            qc_report = await asyncio.to_thread(run_quality_gate,
                job_id=c.job_id,
                final_video_path=qc_target,
                original_video_path=c.v_path,
                translated_subtitles=translated_segments,
                dubbing_audio_files=dubbing_audio_files,
                mixed_audio_path=mixed_audio,
                ass_subtitle_path=ass_path,
                policy=qc_policy,
                workspace_path=self.workspace,
                enable_visual_pixel_qc=qc_pixel_enabled,
                progress_callback=qc_progress,
                stop_check=c.check_stop,
            )
            c.check_stop()
            report_path = self.workspace / "bot_system" / "qc_reports" / f"{c.job_id}.qc.json"
            artifacts = {"qc_report": str(report_path)} if report_path.is_file() else None
            self.manifest_manager.record_stage_complete(c.job_id, "quality_gate", artifacts)
        except asyncio.CancelledError:
            self.manifest_manager.record_stage_failure(c.job_id, "quality_gate", "Quality Gate cancelled")
            raise
        except Exception as exc:
            self.manifest_manager.record_stage_failure(c.job_id, "quality_gate", str(exc))
            raise

        c.check_stop()
        # Xuất bản nguyên tử (Atomic Replace)
        if temp_render_video.is_file() and temp_render_video != final_video:
            os.replace(str(temp_render_video), str(final_video))
            self.manifest_manager.record_stage_complete(c.job_id, "video_rendering", {"temp_render": str(final_video)})

        # Phân phối tới thư mục đích nếu có (mặc định D:\banve từ AUTODUB_OUTPUT_DIR)
        published_path = str(final_video)
        effective_delivery = c.delivery_path
        if not effective_delivery:
            default_out_dir = os.getenv("AUTODUB_OUTPUT_DIR", "D:\\banve")
            if default_out_dir and os.path.isdir(default_out_dir):
                effective_delivery = Path(default_out_dir) / f"Dubbed_{c.base_name}.mp4"

        if effective_delivery:
            del_p = Path(effective_delivery)
            from pipeline_v2.atomic_io import atomic_copy_file
            if del_p.resolve() != final_video.resolve():
                await asyncio.to_thread(atomic_copy_file, final_video, del_p)
            published_path = str(del_p)
            logger.info("[DELIVERY_GUARD] Da tu dong luu video hoan tat toi: %s", published_path)

        from dashboard_media import media_status
        delivered_check = await asyncio.to_thread(media_status, published_path)
        if delivered_check["status"] != "valid":
            message = "Khong xac minh duoc thanh pham: " + delivered_check["reason"]
            self.manifest_manager.record_stage_failure(c.job_id, "delivery", message)
            raise RuntimeError(message)

        # Ghi nhận thời gian render/edit vào render_history và job_tracker vĩnh viễn
        job_duration_s = round(time.time() - c.t_job_start, 1)
        try:
            from render_history import record_render_duration
            record_render_duration(published_path, job_duration_s)
            record_render_duration(Path(published_path).name, job_duration_s)
            record_render_duration(c.v_path.name, job_duration_s)
            if c.delivery_path:
                record_render_duration(str(c.delivery_path), job_duration_s)
                record_render_duration(Path(c.delivery_path).name, job_duration_s)
        except Exception as rh_err:
            logger.warning("Không thể ghi nhận render_history: %s", rh_err)

        tracker_finalized = False
        try:
            import job_tracker
            job_tracker.finish_video(c.v_path.name, output_path=published_path, duration_seconds=job_duration_s)
            tracker_finalized = True
        except Exception as tracker_err:
            logger.warning("Không thể ghi nhận hoàn tất job: %s", tracker_err)

        c.manifest = self.manifest_manager.load_manifest(c.job_id)
        if not c.manifest:
            raise RuntimeError(f"Missing manifest at publish: {c.job_id}")
        c.manifest.status = "completed"
        c.manifest.output_video_path = published_path
        self.manifest_manager.save_manifest(c.manifest)

        try:
            from workspace_cleaner import clean_completed_job_intermediate_files, clean_stale_downloads
            await asyncio.to_thread(clean_completed_job_intermediate_files, c.job_id, self.workspace, c.out_dir)
            await asyncio.to_thread(clean_stale_downloads, Path(self.workspace) / "downloads")
        except Exception as cl_err:
            logger.warning("[CLEANUP] Lỗi dọn tệp trung gian job %s: %s", c.job_id, cl_err)

        await c.notify("publish", 9, 100.0, f"✅ Hoàn tất xuất xưởng video thành phẩm: {Path(published_path).name}", {
            "published_path": published_path,
            "qc_status": qc_report.overall_status,
        })

        return {
            "status": "success",
            "job_id": c.job_id,
            "video_mode": c.resolved_mode,
            "final_video": published_path,
            "manifest_file": str(self.manifest_manager._manifest_file(c.job_id)),
            "qc_status": qc_report.overall_status,
            "qc_report": qc_report.to_dict(),
            "tracker_finalized": tracker_finalized,
        }

    async def _stage_visual_transcript(self, c, srt_segments, asr_was_empty, srt_original, async_gpu_gatekeeper):
        if not asr_was_empty and not _looks_like_bgm_hallucination(srt_segments, c.video_dur_s):
            return srt_segments, False
        stage_id = "speech_asr"
        visual_ocr_cached = False
        asr_cue_count = len(srt_segments)
        await c.notify(
            stage_id, 3, 40.0,
            "Đang quét phụ đề tiếng Trung trên màn hình (Chế độ Video không lời thoại)...",
        )
        from ocr_utils import perform_video_ocr, release_ocr_reader
        c.ocr_reader_releaser = release_ocr_reader
        from v1_ocr_geometry import OCR_GEOMETRY_VERSION
        from ai.transcription import save_srt
        t_ocr = self._calculate_stage_timeout("visual_ocr", c.video_dur_s, c.resolved_mode)
        c.check_stop()
        try:
            async with async_gpu_gatekeeper("visual_ocr", timeout_seconds=t_ocr):
                visual_segments, vid_w_src, vid_h_src, main_y_src = await c.run_stage(
                    asyncio.to_thread(
                        perform_video_ocr,
                        str(c.v_path),
                        target_lang=c.effective_cfg.get("target_lang", "vi"),
                        sample_rate=1.0,
                        api_key=c.overrides.get("api_key") or os.getenv("GEMINI_API_KEY", ""),
                        srt_segments=[],
                        strict_geometry=True,
                        visual_sub_to_speech=True,
                        _source_duration=c.video_dur_s,
                    ),
                    timeout=t_ocr
                )
        finally:
            release_ocr_reader()
        visual_segments = visual_transcript_candidates(visual_segments)
        if _visual_ocr_transcript_usable(visual_segments, c.video_dur_s):
            srt_segments = visual_segments
            for idx, seg in enumerate(srt_segments, start=1):
                seg.index = idx
            save_srt(srt_segments, str(srt_original))
            save_payload(
                c.out_dir / "ocr.json",
                geometry_version=OCR_GEOMETRY_VERSION,
                source_policy_version=TRANSCRIPT_SOURCE_POLICY_VERSION,
                segments=srt_segments,
                floating_segments=[],
                width=vid_w_src,
                height=vid_h_src,
                main_y_pct=main_y_src,
                visual_asr_reuse=True,
                asr_was_empty=asr_was_empty,
            )
            visual_ocr_cached = True
            logger.info(
                "🎬 Visual OCR-to-TTS: dùng %d câu phụ đề trên hình thay cho ASR (%s)",
                len(srt_segments),
                "ASR rỗng" if asr_was_empty else f"ASR {asr_cue_count} câu",
            )
        elif asr_was_empty:
            raise RuntimeError(
                "Video không có lời thoại và cũng không phát hiện được phụ đề tiếng Trung trên màn hình."
            )
        else:
            logger.info(
                "Visual OCR-to-TTS: không đủ phụ đề trên hình, giữ ASR %d câu",
                len(srt_segments),
            )
        return srt_segments, visual_ocr_cached

    def _align_source_subtitles(self, srt_segments):
        # Căn chỉnh timestamp phụ đề chống lệch
        import datetime
        for i in range(len(srt_segments) - 1):
            if srt_segments[i].end > srt_segments[i + 1].start:
                new_end = srt_segments[i + 1].start - datetime.timedelta(seconds=0.05)
                if new_end > srt_segments[i].start:
                    srt_segments[i].end = new_end
                else:
                    srt_segments[i].end = srt_segments[i].start + datetime.timedelta(seconds=0.1)
        for i, seg in enumerate(srt_segments, 1):
            seg.index = i
