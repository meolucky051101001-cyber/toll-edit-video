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
import sys
import time
import math
import shutil
import logging
import asyncio
from v1_stage_runtime import save_payload, load_payload, await_stage
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# V1 callers select this orchestrator explicitly; do not mutate process mode.

try:
    from v1_checkpoint import ManifestManager, JobManifest, STAGES_ORDER, fingerprint
    from v1_video_router import route_video, create_router_snapshot, VideoMode
    from v1_model_registry import (
        ModelStage, ModelSpec, resolve_effective_model, log_stage_telemetry
    )
    from v1_quality_gate import run_quality_gate, QCPolicy
    from v1_feature_flags import get_feature_flags, sync_env_from_flags
except ImportError:
    from .v1_checkpoint import ManifestManager, JobManifest, STAGES_ORDER, fingerprint
    from .v1_video_router import route_video, create_router_snapshot, VideoMode
    from .v1_model_registry import (
        ModelStage, ModelSpec, resolve_effective_model, log_stage_telemetry
    )
    from .v1_quality_gate import run_quality_gate, QCPolicy
    from .v1_feature_flags import get_feature_flags, sync_env_from_flags


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
            if model_id == "qwen3_asr_preview":
                asr_model = "qwen3_asr"
            elif model_id == "whisper_large_v3_turbo":
                asr_model = "whisper_turbo"
            else:
                asr_model = model_id
        asr_model = asr_model or config.get("asr_model") or "whisper_turbo"
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
        if "manual_voice" not in voice_config:
            voice_config["manual_voice"] = dict(voice_selection.selected())
        snapshot = freeze_job_config(
            video_name=path.name, job_id=job_id, routing_snapshot=routing, workspace_path=self.workspace,
            overrides={**safe_overrides,
                "video_mode": resolved_mode, "planned_pipeline": planned,
                "planned_models": planned_models, "policy_version": 2,
                "separation_mode": planned["separation_model"],
                "asr_model": planned["asr_model"],
                "voice_mode": overrides.get("voice_mode") or ("auto" if voice_config.get("enabled") else "manual"),
                "voice_config": voice_config, "feature_flags": flags,
                "qc_policy": overrides.get("qc_policy") or flags["V1_QC_POLICY"],
            })
        effective_cfg = snapshot["effective_config"]
        # Config services may filter unknown preset fields; retain job-owned policy.
        effective_cfg.update({
            "policy_version": 2, "planned_pipeline": planned, "planned_models": planned_models,
            "video_mode": resolved_mode, "separation_mode": planned["separation_model"],
            "voice_config": voice_config, "feature_flags": flags,
            "voice_mode": overrides.get("voice_mode") or ("auto" if voice_config.get("enabled") else "manual"),
            "qc_policy": overrides.get("qc_policy") or flags["V1_QC_POLICY"],
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
            # Qwen3-ASR 0.6B GPU + Forced Aligner cần thời gian nạp trọng số float16 (~15s) và suy luận word tokens
            use_qwen = False
            try:
                from ai.v1_qwen_asr_adapter import is_qwen_asr_enabled
                use_qwen = is_qwen_asr_enabled() or getattr(self, "planned", {}).get("asr_model") == "qwen3_asr"
            except Exception:
                pass
            if use_qwen:
                return max(180, min(2400, int(dur * 2.5) + 180))
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
            mult = 15.0 if dur <= 420 else 8.0
            return max(900, min(5400, int(dur * mult) + 600))
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
        requested_out_dir = Path(output_dir or (self.workspace / v_path.stem)).resolve()
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

        resolved_mode = manifest.video_mode
        planned = self._plan_info_from_manifest(manifest)["planned_pipeline"]
        planned.setdefault("asr_chunking", resolved_mode != "SHORT")
        planned.setdefault("asr_chunk_size_s", 240.0)
        effective_cfg = manifest.effective_config

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

        # Thư mục làm việc cho job
        base_name = v_path.stem
        out_dir = requested_out_dir
        previous_out_dir = effective_cfg.get("output_dir")
        if previous_out_dir and Path(previous_out_dir).resolve() != out_dir:
            raise ValueError("Job đang resume phải dùng nguyên thư mục làm việc đã đóng băng.")
        effective_cfg["output_dir"] = str(out_dir)
        self.manifest_manager.save_manifest(manifest)
        out_dir.mkdir(parents=True, exist_ok=True)

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
            stop_file = Path(self.workspace) / "control" / "video.stop"
            stop_json = Path(self.workspace) / "bot_system" / "stop_request.json"
            if (stop_checker and stop_checker()) or job_tracker.is_stop_requested() or stop_file.exists() or stop_json.exists():
                raise BatchStopRequested(f"Job {job_id} đã bị dừng theo yêu cầu.")

        async def run_stage(awaitable, timeout):
            return await await_stage(awaitable, timeout, check_stop)

        # Probe metadata, not full decoded audio, for long videos.
        video_dur_s = float(manifest.routing_snapshot["metadata"]["duration_s"])
        try:
            import soundfile as sf
            temp_probe = out_dir / "original.wav"
            if temp_probe.is_file():
                video_dur_s = sf.info(str(temp_probe)).duration
        except Exception:
            pass

        # Kiểm tra Resume
        can_resume = False
        resume_stage = STAGES_ORDER[0]
        verified_stages = []
        if resume_if_possible:
            can_resume, resume_stage, resume_details = self.manifest_manager.verify_manifest_resumability(job_id)
            verified_stages = resume_details.get("verified_stages", [])
            if effective_cfg.get("speech_timing_version") != 3:
                tts_pos = STAGES_ORDER.index("ai_tts")
                verified_stages = [s for s in verified_stages if STAGES_ORDER.index(s) < tts_pos]
            from v1_ocr_geometry import OCR_GEOMETRY_VERSION
            if "visual_ocr" in verified_stages:
                try:
                    cached_geometry = load_payload(out_dir / "ocr.json").get("geometry_version")
                except (OSError, ValueError, KeyError, TypeError):
                    cached_geometry = None
                if cached_geometry != OCR_GEOMETRY_VERSION:
                    ocr_pos = STAGES_ORDER.index("visual_ocr")
                    verified_stages = [s for s in verified_stages if STAGES_ORDER.index(s) < ocr_pos]
                    logger.info('[RESUME] Old OCR geometry: recompute OCR and dependent stages; keep audio/ASR')

        artifacts_acc: Dict[str, str] = {}

        # ==================== STAGE 1: EXTRACT AUDIO ====================
        stage_id = "extract_audio"
        original_audio = out_dir / "original.wav"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        if should_run:
            await notify(stage_id, 1, 10.0, "🎧 Bước 1/8: Đang trích xuất âm thanh gốc...")
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                from video_utils import extract_audio_from_video
                check_stop()
                ok = await run_stage(
                    asyncio.to_thread(extract_audio_from_video, str(v_path), str(original_audio)),
                    timeout=t_out
                )
                if not ok or not original_audio.is_file() or original_audio.stat().st_size < 1000:
                    raise RuntimeError("Trích xuất âm thanh thất bại hoặc file rỗng.")
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"original_audio": str(original_audio)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            logger.info("[RESUME] Bỏ qua stage 1 (extract_audio) do artifact đã được xác thực.")
        artifacts_acc["original_audio"] = str(original_audio)

        # Cập nhật thời lượng audio chính xác
        try:
            import soundfile as sf
            video_dur_s = sf.info(str(original_audio)).duration
        except Exception:
            pass

        # ==================== STAGE 2: SEPARATE VOCALS ====================
        stage_id = "separate_vocals"
        vocals_audio = out_dir / "vocals.wav"
        no_vocals_audio = out_dir / "no_vocals.wav"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        if should_run:
            sep_mode = (
                effective_cfg.get("separation_mode")
                or effective_cfg.get("separation_model")
                or planned.get("separation_model")
                or planned.get("separator_engine")
                or ("demucs" if resolved_mode in ("LONG", "MEDIUM") else "roformer")
            )
            await notify(stage_id, 2, 25.0, f"🧠 Bước 2/8: Bóc tách giọng nói khỏi nhạc nền ({sep_mode.upper()})...", {"model": sep_mode})
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                if sep_mode != "bypass":
                    self._check_gpu_guard(stage_id)
                from video_utils import separate_vocals_demucs
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                check_stop()
                async with async_gpu_gatekeeper("vocal_separation", timeout_seconds=t_out):
                    v_res, nv_res = await run_stage(
                        asyncio.to_thread(
                            separate_vocals_demucs,
                            str(original_audio),
                            str(out_dir),
                            separation_mode=sep_mode,
                        ),
                        timeout=t_out
                    )
                vocals_audio = Path(v_res)
                no_vocals_audio = Path(nv_res)
                if not vocals_audio.is_file() or not no_vocals_audio.is_file():
                    raise RuntimeError("Tách âm thanh không xuất đủ file vocals hoặc no_vocals.")
                self.manifest_manager.record_stage_complete(job_id, stage_id, {
                    "vocals_audio": str(vocals_audio),
                    "no_vocals_audio": str(no_vocals_audio),
                })
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            saved = manifest.stages[stage_id]["artifacts"]
            vocals_audio = Path(saved["vocals_audio"]["path"])
            no_vocals_audio = Path(saved["no_vocals_audio"]["path"])
            logger.info("[RESUME] Khôi phục đúng đường dẫn file tách âm đã xác thực.")
        artifacts_acc["vocals_audio"] = str(vocals_audio)
        artifacts_acc["no_vocals_audio"] = str(no_vocals_audio)

        # ==================== STAGE 3: SPEECH ASR ====================
        stage_id = "speech_asr"
        srt_original = out_dir / "original.srt"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        srt_segments = []
        if should_run:
            use_qwen = planned["asr_model"] == "qwen3_asr"
            model_display = "Qwen3-ASR 0.6B (GPU CUDA)" if use_qwen else "Faster-Whisper Large-v3 Turbo"
            await notify(stage_id, 3, 40.0, f"🤖 Bước 3/8: {model_display} đang nhận dạng giọng nói...")
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                self._check_gpu_guard(stage_id)
                from ai.v1_asr_isolated import extract_subtitles_isolated as extract_subtitles_whisper
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                check_stop()
                # extract_subtitles_whisper tự động điều phối Qwen3-ASR hoặc Faster-Whisper + Chunking
                async with async_gpu_gatekeeper("speech_asr", timeout_seconds=t_out):
                    srt_segments = await run_stage(
                        asyncio.to_thread(
                            extract_subtitles_whisper,
                            str(vocals_audio),
                            str(srt_original),
                            original_audio_path=str(original_audio),
                            model_id=planned["asr_model"], chunking=planned["asr_chunking"],
                            chunk_size_s=planned["asr_chunk_size_s"], overlap_s=planned["asr_overlap_s"], timeout_s=t_out,
                        ),
                        timeout=t_out
                    )
                if not srt_segments or not srt_original.is_file():

                    raise RuntimeError(f"ASR ({model_display}) không nhận dạng được câu thoại nào.")
                save_payload(out_dir / "asr.json", segments=srt_segments)
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"srt_original": str(srt_original), "asr_data": str(out_dir / "asr.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            import srt
            logger.info("[RESUME] Tải lại srt_original đã cache.")
            sidecar = out_dir / "asr.json"
            srt_segments = load_payload(sidecar)["segments"] if sidecar.is_file() else list(srt.parse(srt_original.read_text(encoding="utf-8")))
        artifacts_acc["srt_original"] = str(srt_original)

        # ==================== STAGE 4: VISUAL OCR ====================
        stage_id = "visual_ocr"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        vid_w, vid_h, main_y_pct = 1080, 1920, 0.88
        floating_segments = []
        if should_run:
            ocr_strat = planned.get("ocr_strategy", "auto")
            short_thresh = float(os.getenv("V1_SHORT_MAX_SECONDS", "420.0"))
            if video_dur_s > 0 and video_dur_s <= short_thresh:
                ocr_strat = "full"
            await notify(stage_id, 4, 55.0, f"👀 Bước 4/8: Quét vị trí phụ đề chữ cứng (OCR {ocr_strat})...", {"ocr_strategy": ocr_strat})
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                from ocr_utils import perform_video_ocr, release_ocr_reader
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                check_stop()
                gemini_key = overrides.get("api_key") or os.getenv("GEMINI_API_KEY", "")
                async with async_gpu_gatekeeper("visual_ocr", timeout_seconds=t_out):
                    floating_segments, vid_w, vid_h, main_y_pct = await run_stage(
                        asyncio.to_thread(
                            perform_video_ocr,
                            str(v_path),
                            target_lang=effective_cfg.get("target_lang", "vi"),
                            sample_rate=1.0,
                            api_key=gemini_key,
                            srt_segments=srt_segments,
                            ocr_strategy=ocr_strat,
                            geometry_report_path=str(out_dir / 'ocr_geometry_report.json'),
                        ),
                        timeout=t_out
                    )
                from v1_ocr_geometry import OCR_GEOMETRY_VERSION

                save_payload(out_dir / "ocr.json", geometry_version=OCR_GEOMETRY_VERSION, segments=srt_segments,
                             floating_segments=floating_segments, width=vid_w, height=vid_h, main_y_pct=main_y_pct)
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"ocr_data": str(out_dir / "ocr.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
            finally:
                if "release_ocr_reader" in locals():
                    release_ocr_reader()
        else:
            saved = load_payload(out_dir / "ocr.json")
            srt_segments, floating_segments = saved["segments"], saved["floating_segments"]
            vid_w, vid_h, main_y_pct = saved["width"], saved["height"], saved["main_y_pct"]

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

        # ==================== STAGE 5: AI TRANSLATION ====================
        stage_id = "ai_translation"
        srt_translated = out_dir / "translated.srt"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        translated_segments = []
        if should_run:
            configured_gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
            try:
                import job_tracker
                job_tracker.record_active_translation_model(configured_gemini_model)
            except Exception:
                pass
            v1_script_mode = effective_cfg.get("script_mode") or overrides.get("script_mode") or os.getenv("SCRIPT_MODE", "default")
            script_badge = " (Kịch bản: Hài hước)" if str(v1_script_mode).lower() in ("humorous", "hai_huoc", "haihuoc", "comedy") else ""
            await notify(stage_id, 5, 65.0, f"🌐 Bước 5/8: Dịch phụ đề chuẩn ngữ cảnh bằng {configured_gemini_model}{script_badge} ({len(srt_segments)} câu)...", {"gemini_model": configured_gemini_model, "script_mode": v1_script_mode})
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                from ai.translation import translate_subtitles
                from ai.transcription import save_srt
                gemini_key = overrides.get("api_key") or os.getenv("GEMINI_API_KEY", "")
                check_stop()
                translated_segments = await run_stage(
                    asyncio.to_thread(
                        translate_subtitles,
                        srt_segments,
                        effective_cfg.get("target_lang", "vi"),
                        api_key=gemini_key,
                        video_path=str(v_path),
                        strict=True,
                        script_mode=v1_script_mode,
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
                save_payload(out_dir / "translation.json", segments=translated_segments)
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"srt_translated": str(srt_translated), "translation_data": str(out_dir / "translation.json")})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            import srt
            logger.info("[RESUME] Tải lại srt_translated đã cache.")
            translated_segments = load_payload(out_dir / "translation.json")["segments"]
        artifacts_acc["srt_translated"] = str(srt_translated)

        # ==================== STAGE 6: AI TTS & DUBBING ====================
        stage_id = "ai_tts"
        dubbing_dir = out_dir / "dubbing"
        ass_path = out_dir / "final.ass"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        dubbing_audio_files = []
        if should_run:
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                from ai.v1_auto_voice import decide_video_voice
                from ai.v1_voice_isolated import generate_dubbing_audio_isolated as generate_dubbing_audio
                from ass_utils import sync_and_clamp_subtitles, generate_ass_file

                # Khóa giọng đọc
                voice_lock_info = await asyncio.to_thread(
                    decide_video_voice,
                    out_dir=str(out_dir),
                    srt_segments=srt_segments,
                    vocals_path=str(vocals_audio),
                    original_audio_path=str(original_audio),
                    video_path=str(v_path),
                    voice_mode=effective_cfg.get("voice_mode"),
                    workspace=str(self.workspace),
                    voice_config=effective_cfg["voice_config"], job_id=job_id,
                )
                v_source = voice_lock_info["voice_source"]
                v_param = voice_lock_info["voice_param"]
                v_label = voice_lock_info["voice_label"]
                seg_voices = voice_lock_info.get("segment_voices")

                await notify(stage_id, 6, 75.0, f"🗣️ Bước 6/8: Lồng tiếng AI CapCut ({v_label}, 6 luồng)...", {"voice": v_label})
                check_stop()

                gemini_key = overrides.get("api_key") or os.getenv("GEMINI_API_KEY", "")
                dubbing_audio_files = await run_stage(
                    generate_dubbing_audio(
                        translated_segments,
                        str(dubbing_dir),
                        voice_source=v_source,
                        voice_param=v_param,
                        api_key=gemini_key,
                        video_duration=video_dur_s,
                        segment_voices=seg_voices,
                        tts_workers=effective_cfg["feature_flags"]["V1_TTS_WORKERS"],
                        max_natural_speed=effective_cfg["feature_flags"]["V1_MAX_NATURAL_SPEED"],
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
                        self.manifest_manager.record_tts_segment_done(job_id, int(d["index"]))

                # Đồng bộ timing và chống đè câu
                translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

                # Tạo file phụ đề ASS
                await asyncio.to_thread(
                    generate_ass_file,
                    translated_segments,
                    floating_segments,
                    str(ass_path),
                    play_res_x=vid_w,
                    play_res_y=vid_h,
                    main_y_pct=main_y_pct,
                    font_name=effective_cfg.get("font_name", "Arial"),
                    font_color=effective_cfg.get("font_color", "&H00000000"),
                    font_weight=effective_cfg.get("font_weight", 2),
                )

                save_payload(out_dir / "tts.json", segments=translated_segments, dubs=dubbing_audio_files)
                tts_artifacts = {"tts_data": str(out_dir / "tts.json"), "ass_path": str(ass_path),
                                 "voice_lock": str(out_dir / "voice_lock.json")}
                for i, dub in enumerate(dubbing_audio_files):
                    tts_artifacts[f"audio_{i}"] = dub["path"]
                self.manifest_manager.record_stage_complete(job_id, stage_id, tts_artifacts)
                current = self.manifest_manager.load_manifest(job_id)
                current.effective_config["speech_timing_version"] = 3
                self.manifest_manager.save_manifest(current)
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            saved = load_payload(out_dir / "tts.json")
            translated_segments, dubbing_audio_files = saved["segments"], saved["dubs"]
        artifacts_acc["dubbing_dir"] = str(dubbing_dir)
        artifacts_acc["ass_path"] = str(ass_path)

        # ==================== STAGE 7: AUDIO MIXING ====================
        stage_id = "audio_mixing"
        mixed_audio = out_dir / "mixed.wav"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        if should_run:
            await notify(stage_id, 7, 85.0, "🎛️ Bước 7/8: Trộn nhạc nền & giọng đọc (Hierarchical Adaptive Mixer)...")
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                from video_utils import mix_audio_pydub
                bgm_vol = effective_cfg.get("bgm_volume_db", -2.0)
                dub_vol = effective_cfg.get("dubbing_volume_db", 1.0)
                duck_mode = effective_cfg.get("ducking_mode", "soft")
                mixer_mode = planned.get("mixer_mode", "direct")

                check_stop()
                if mixer_mode == "hierarchical" or resolved_mode in ("LONG", "MEDIUM") or len(dubbing_audio_files) > 100:
                    try:
                        from v1_hierarchical_mixer import mix_hierarchical_audio
                        await run_stage(
                            asyncio.to_thread(
                                mix_hierarchical_audio,
                                str(no_vocals_audio),
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
                    await run_stage(
                        asyncio.to_thread(
                            mix_audio_pydub,
                            str(no_vocals_audio),
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
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"mixed_audio": str(mixed_audio)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            logger.info("[RESUME] Bỏ qua stage 7 (audio_mixing).")
        artifacts_acc["mixed_audio"] = str(mixed_audio)

        # ==================== STAGE 8: VIDEO RENDERING ====================
        stage_id = "video_rendering"
        final_video = out_dir / f"final_{base_name}.mp4"
        temp_render_video = out_dir / f"final_{base_name}.tmp.mp4"
        check_stop()

        should_run = not (can_resume and stage_id in verified_stages)
        if should_run:
            await notify(stage_id, 8, 92.0, "🎬 Bước 8/8: Render video thành phẩm bằng NVENC GPU...")
            self.manifest_manager.record_stage_start(job_id, stage_id)
            t_out = self._calculate_stage_timeout(stage_id, video_dur_s, resolved_mode)
            try:
                self._check_gpu_guard(stage_id)
                from video_utils import process_video
                from v1_gpu_gatekeeper import async_gpu_gatekeeper
                check_stop()
                async with async_gpu_gatekeeper("video_rendering", timeout_seconds=t_out):
                    rendered = await run_stage(
                        asyncio.to_thread(
                            process_video,
                            str(v_path),
                            str(ass_path),
                            str(mixed_audio),
                            str(temp_render_video),
                            main_y_pct=main_y_pct,
                            delogo=False,
                        ),
                        timeout=t_out
                    )
                if not rendered or not temp_render_video.is_file() or temp_render_video.stat().st_size < 10240:

                    raise RuntimeError("Render video NVENC thất bại hoặc file xuất xưởng bị lỗi.")
                self.manifest_manager.record_stage_complete(job_id, stage_id, {"temp_render": str(temp_render_video)})
            except Exception as exc:
                self.manifest_manager.record_stage_failure(job_id, stage_id, str(exc))
                raise
        else:
            logger.info("[RESUME] Bỏ qua stage 8 (video_rendering).")
            temp_render_video = Path(manifest.stages[stage_id]["artifacts"]["temp_render"]["path"])

        # ==================== STAGE 9: QUALITY GATE & PUBLISH ====================
        qc_target = temp_render_video if temp_render_video.is_file() else final_video
        qc_policy = effective_cfg.get("qc_policy", "REPORT_ONLY")
        logger.info("[QUALITY_GATE] Đang thực hiện kiểm tra chất lượng tự động 8 nhóm (policy=%s)...", qc_policy)

        try:
            qc_report = await asyncio.to_thread(run_quality_gate,
                job_id=job_id,
                final_video_path=qc_target,
                original_video_path=v_path,
                translated_subtitles=translated_segments,
                dubbing_audio_files=dubbing_audio_files,
                mixed_audio_path=mixed_audio,
                ass_subtitle_path=ass_path,
                policy=qc_policy,
                workspace_path=self.workspace,
            )
        except Exception as exc:
            self.manifest_manager.record_stage_failure(job_id, "quality_gate", str(exc))
            raise

        check_stop()
        # Xuất bản nguyên tử (Atomic Replace)
        if temp_render_video.is_file() and temp_render_video != final_video:
            os.replace(str(temp_render_video), str(final_video))
            self.manifest_manager.record_stage_complete(job_id, "video_rendering", {"temp_render": str(final_video)})

        # Phân phối tới thư mục đích nếu có (mặc định D:\banve từ AUTODUB_OUTPUT_DIR)
        published_path = str(final_video)
        effective_delivery = delivery_path
        if not effective_delivery:
            default_out_dir = os.getenv("AUTODUB_OUTPUT_DIR", "D:\\banve")
            if default_out_dir and os.path.isdir(default_out_dir):
                effective_delivery = Path(default_out_dir) / f"Dubbed_{base_name}.mp4"

        if effective_delivery:
            del_p = Path(effective_delivery)
            del_p.parent.mkdir(parents=True, exist_ok=True)
            staged_delivery = del_p.with_name(del_p.name + f".{job_id}.partial")
            shutil.copy2(str(final_video), str(staged_delivery))
            os.replace(staged_delivery, del_p)
            published_path = str(del_p)
            logger.info("[DELIVERY_GUARD] Da tu dong luu video hoan tat toi: %s", published_path)

        # Tự động dọn dẹp các tệp trung gian nặng (WAV, stems, bản sao video) sau khi phân phối thành công
        try:
            from workspace_cleaner import clean_completed_job_intermediate_files
            clean_completed_job_intermediate_files(job_id, self.workspace)
        except Exception as cl_err:
            logger.warning("[CLEANUP] Lỗi dọn tệp trung gian job %s: %s", job_id, cl_err)

        # Cập nhật manifest hoàn tất
        manifest = self.manifest_manager.load_manifest(job_id)
        if manifest:
            manifest.status = "completed"
            manifest.output_video_path = published_path
            self.manifest_manager.save_manifest(manifest)

        # Ghi nhận thời gian render/edit vào render_history và job_tracker vĩnh viễn
        job_duration_s = round(time.time() - t_job_start, 1)
        try:
            from render_history import record_render_duration
            record_render_duration(published_path, job_duration_s)
            record_render_duration(Path(published_path).name, job_duration_s)
            record_render_duration(v_path.name, job_duration_s)
            if delivery_path:
                record_render_duration(str(delivery_path), job_duration_s)
                record_render_duration(Path(delivery_path).name, job_duration_s)
        except Exception as rh_err:
            logger.warning("Không thể ghi nhận render_history: %s", rh_err)

        tracker_finalized = False
        try:
            import job_tracker
            job_tracker.finish_video(v_path.name, output_path=published_path, duration_seconds=job_duration_s)
            tracker_finalized = True
        except Exception as tracker_err:
            logger.warning("Không thể ghi nhận hoàn tất job: %s", tracker_err)

        await notify("publish", 8, 100.0, f"✅ Hoàn tất xuất xưởng video thành phẩm: {Path(published_path).name}", {
            "published_path": published_path,
            "qc_status": qc_report.overall_status,
        })

        return {
            "status": "success",
            "job_id": job_id,
            "video_mode": resolved_mode,
            "final_video": published_path,
            "manifest_file": str(self.manifest_manager._manifest_file(job_id)),
            "qc_status": qc_report.overall_status,
            "qc_report": qc_report.to_dict(),
            "tracker_finalized": tracker_finalized,
        }
