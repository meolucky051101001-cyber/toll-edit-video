"""
recovery_service.py - Giai đoạn D: Khôi phục sau lỗi và chạy lại thông minh (Crash Recovery & Smart Resume).
Quản lý kế hoạch khôi phục, kiểm tra tính hợp lệ của artifact, chống claim trùng lặp và phân biệt 3 chế độ:
1. Tiếp tục (unpause)
2. Khôi phục từ bước đã lưu (recover from checkpoint)
3. Chạy lại từ đầu (restart from scratch)
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("recovery_service")

# Token cache for valid resume plans: { token: { "job_id": ..., "created_at": ..., "plan": ... } }
_ACTIVE_PLAN_TOKENS: Dict[str, Dict[str, Any]] = {}
TOKEN_TTL_SECONDS = 900  # 15 minutes


def compute_file_fingerprint(file_path: Path) -> Optional[str]:
    """Tính fingerprint nhanh cho video: size + mtime + sha256 của header 64KB."""
    try:
        stat = file_path.stat()
        digest = hashlib.sha256()
        digest.update(f"{stat.st_size}_{stat.st_mtime}".encode())
        with open(file_path, "rb") as f:
            chunk = f.read(65536)
            digest.update(chunk)
        return digest.hexdigest()[:20]
    except Exception as e:
        logger.warning(f"Không thể tính fingerprint cho {file_path}: {e}")
        return None


class RecoveryService:
    def __init__(self, workspace_path: Optional[str] = None):
        if workspace_path:
            self.workspace = Path(workspace_path)
        else:
            base_dir = Path(__file__).resolve().parent
            self.workspace = Path(os.getenv("AUTODUB_WORKSPACE", str(base_dir.parent / "workspace")))
        self.control_dir = self.workspace / "control"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.control_dir / "job_recovery_states.json"
        self.lock_file = self.control_dir / "recovery.lock"

    def _read_states(self) -> Dict[str, Any]:
        if not self.state_file.is_file():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_states(self, states: Dict[str, Any]):
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"Lỗi ghi recovery states: {e}")

    def find_source_video(self, job_id: str, input_dir: Optional[Path] = None) -> Optional[Path]:
        """Tìm file video nguồn gốc dựa trên tên hoặc job_id."""
        clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()
        candidates_dirs = []
        if input_dir:
            candidates_dirs.append(input_dir)
            candidates_dirs.append(input_dir / "processed")

        # Default standard paths
        candidates_dirs.extend([
            Path(r"D:\video_input"),
            Path(r"D:\video_input\processed"),
            Path(r"D:\phoi"),
            Path(r"D:\video_input_v2"),
            self.workspace / "input",
        ])

        for c_dir in candidates_dirs:
            if not c_dir.is_dir():
                continue
            # Direct match
            for ext in [".mp4", ".mov", ".mkv", ".avi", ""]:
                test_file = c_dir / f"{clean_name}{ext}"
                if test_file.is_file():
                    return test_file
            # Exact filename match
            direct_file = c_dir / job_id
            if direct_file.is_file():
                return direct_file
            # Partial match if clean_name is contained
            for f in c_dir.iterdir():
                if f.is_file() and clean_name in f.name and f.suffix.lower() in [".mp4", ".mov", ".mkv"]:
                    return f
        return None

    def find_job_workspace(self, job_id: str, source_path: Optional[Path] = None) -> Optional[Path]:
        """Tìm thư mục workspace của job."""
        clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()

        # Check by source hash batch folder if source_path is available
        if source_path and source_path.is_file():
            source_id = hashlib.sha256(str(source_path.resolve()).encode()).hexdigest()[:16]
            batch_candidate = self.workspace / f"batch_{source_id}_{clean_name}"
            if batch_candidate.is_dir():
                return batch_candidate

        # Look in workspace root
        for folder in self.workspace.iterdir():
            if not folder.is_dir():
                continue
            if folder.name == clean_name:
                return folder
            if folder.name.startswith("batch_") and clean_name in folder.name:
                return folder

        # Fallback default location if not created yet
        return self.workspace / clean_name

    def find_completed_output(self, job_id: str, output_dir: Optional[Path] = None) -> Optional[Path]:
        """Kiểm tra video thành phẩm đã được xuất ra thư mục output hay chưa."""
        clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()
        candidates_dirs = []
        if output_dir:
            candidates_dirs.append(output_dir)
        candidates_dirs.extend([
            Path(r"D:\banve"),
            Path(r"D:\video tool v2"),
            self.workspace / "output"
        ])

        for c_dir in candidates_dirs:
            if not c_dir.is_dir():
                continue
            for fname in [f"Dubbed_{clean_name}.mp4", f"{clean_name}.mp4", job_id]:
                target = c_dir / fname
                if target.is_file() and target.stat().st_size > 1024 * 1024:
                    return target
        return None

    def is_job_paused(self) -> bool:
        """Kiểm tra trạng thái tạm dừng video.pause trên hệ thống."""
        flag = self.control_dir / "video.pause"
        return flag.is_file()

    def build_resume_plan(
        self,
        job_id: str,
        input_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        requested_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Xây dựng kế hoạch khôi phục:
        - Kiểm tra video nguồn và fingerprint
        - Kiểm tra các stage: stage nào tái sử dụng, stage nào phải chạy lại, lý do
        - Kiểm tra trạng thái đã hoàn tất (tránh tạo output trùng)
        - Kiểm tra trạng thái đang tạm dừng (can_unpause)
        """
        clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()
        source_path = self.find_source_video(job_id, input_dir=input_dir)
        ws_dir = self.find_job_workspace(job_id, source_path=source_path)
        completed_file = self.find_completed_output(job_id, output_dir=output_dir)

        states = self._read_states()
        prev_state = states.get(clean_name, {})
        retry_count = prev_state.get("retry_count", 0)

        # 1. Source verification
        source_info = None
        source_fingerprint = None
        source_changed = False
        if source_path and source_path.is_file():
            stat = source_path.stat()
            source_fingerprint = compute_file_fingerprint(source_path)
            source_info = {
                "filename": source_path.name,
                "path": str(source_path.resolve()),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "last_modified": int(stat.st_mtime),
                "fingerprint": source_fingerprint,
            }
            if prev_state.get("source_fingerprint") and prev_state["source_fingerprint"] != source_fingerprint:
                source_changed = True
        else:
            source_info = {
                "filename": f"{clean_name}.mp4",
                "path": "Không tìm thấy trên đĩa",
                "size_mb": 0,
                "last_modified": 0,
                "fingerprint": None,
            }

        # 2. Check in-flight paused status
        can_unpause = self.is_job_paused()

        # 3. Check already completed
        is_completed = completed_file is not None

        # 4. Stage inspection & invalidation matrix
        # Stages definitions
        stage_definitions = [
            {
                "id": "extract_audio",
                "name": "Bước 1: Trích xuất âm thanh gốc",
                "artifacts": ["original.wav", "original_audio.mp3", "pipeline_v2/artifacts/audio/source_audio.wav"],
            },
            {
                "id": "separate_vocals",
                "name": "Bước 2: Bóc tách giọng & Nhạc nền",
                "artifacts": ["vocals.mp3", "vocals.wav", "pipeline_v2/artifacts/audio/vocals.wav"],
            },
            {
                "id": "transcribe",
                "name": "Bước 3: Nhận dạng giọng nói (Faster-Whisper)",
                "artifacts": ["original.srt", "transcribe.checkpoint.json", "pipeline_v2/artifacts/asr/original.srt"],
            },
            {
                "id": "ocr",
                "name": "Bước 3.5: Quét vị trí phụ đề gốc (OCR)",
                "artifacts": ["ocr_results.json", "pipeline_v2/artifacts/ocr/ocr_spans.json"],
            },
            {
                "id": "translate",
                "name": "Bước 4: Dịch thuật phụ đề (Gemini)",
                "artifacts": ["translated.srt", "translate.checkpoint.json", "pipeline_v2/artifacts/translate/translated.srt"],
            },
            {
                "id": "tts_dubbing",
                "name": "Bước 5: Lồng tiếng AI (TTS / RVC)",
                "artifacts": ["dubbed_voice.mp3", "pipeline_v2/artifacts/tts/segments.json", "dubbing"],
            },
            {
                "id": "mix",
                "name": "Bước 5.5: Hòa trộn âm thanh (BGM + Lời đọc)",
                "artifacts": ["mixed.wav", "mixed_audio.mp3", "pipeline_v2/artifacts/mix/mixed.wav"],
            },
            {
                "id": "render",
                "name": "Bước 6: Render video hoàn thiện (NVENC GPU)",
                "artifacts": [f"final_{clean_name}.mp4", "pipeline_v2/artifacts/render/rendered.mp4"],
            },
        ]

        stages_result = []
        downstream_invalid = False
        reusable_count = 0
        rerun_count = 0
        first_rerun_stage = None

        # Check config change invalidation
        voice_changed = False
        volume_changed = False
        if requested_config and prev_state.get("frozen_config"):
            prev_cfg = prev_state["frozen_config"]
            if requested_config.get("voice_id") != prev_cfg.get("voice_id") or requested_config.get("speed") != prev_cfg.get("speed"):
                voice_changed = True
            if requested_config.get("bgm_volume_db") != prev_cfg.get("bgm_volume_db") or requested_config.get("dubbing_volume_db") != prev_cfg.get("dubbing_volume_db"):
                volume_changed = True

        for st in stage_definitions:
            st_id = st["id"]
            st_name = st["name"]
            found_artifacts = []

            # Check if any artifact file exists in workspace
            if ws_dir and ws_dir.is_dir():
                for rel_path in st["artifacts"]:
                    art_file = ws_dir / rel_path
                    if art_file.is_file() and art_file.stat().st_size > 0:
                        size_kb = round(art_file.stat().st_size / 1024, 1)
                        found_artifacts.append(f"{art_file.name} ({size_kb} KB)")
                    elif art_file.is_dir() and any(art_file.iterdir()):
                        found_artifacts.append(f"{art_file.name}/ ({len(list(art_file.iterdir()))} files)")

            # Determine status and reason
            if source_changed:
                status = "rerun"
                reason = "File nguồn đã bị sửa đổi hoặc thay thế. Không thể dùng checkpoint cũ."
                downstream_invalid = True
            elif not source_path or not source_path.is_file():
                status = "rerun"
                reason = "Không tìm thấy file video nguồn."
                downstream_invalid = True
            elif downstream_invalid:
                status = "rerun"
                reason = "Phụ thuộc vào bước trước cần chạy lại."
            elif (st_id in ["tts_dubbing", "mix", "render"]) and voice_changed:
                status = "rerun"
                reason = "Cấu hình giọng đọc hoặc tốc độ đã thay đổi."
                downstream_invalid = True
            elif (st_id in ["mix", "render"]) and volume_changed:
                status = "rerun"
                reason = "Cấu hình âm lượng BGM / Dubbing đã thay đổi."
                downstream_invalid = True
            elif not found_artifacts:
                status = "rerun"
                reason = "Thiếu artifact hoặc file chưa được xử lý ở bước này."
                downstream_invalid = True
            else:
                status = "reusable"
                reason = f"Artifact hợp lệ trên đĩa ({', '.join(found_artifacts)})."
                reusable_count += 1

            if status == "rerun":
                rerun_count += 1
                if first_rerun_stage is None:
                    first_rerun_stage = st_id

            stages_result.append({
                "stage_id": st_id,
                "name": st_name,
                "status": status,
                "reason": reason,
                "artifacts": found_artifacts,
            })

        # Generate unique token for this evaluation
        plan_token = f"plan_{hashlib.sha256(f'{clean_name}:{source_fingerprint}:{time.time()}:{uuid.uuid4().hex}'.encode()).hexdigest()[:20]}"

        warnings = []
        if is_completed:
            warnings.append(f"Video này đã có thành phẩm hoàn tất trên đĩa ({completed_file.name}). Khôi phục hoặc chạy lại sẽ tạo file mới hoặc ghi đè nếu được chọn.")
        if source_changed:
            warnings.append("Cảnh báo: File video nguồn có sự thay đổi kích thước/thời gian sửa đổi. Hệ thống sẽ vô hiệu hóa toàn bộ checkpoint cũ để đảm bảo tính toàn vẹn.")
        if retry_count >= 3:
            warnings.append(f"Video này đã được thử lại {retry_count} lần. Hãy kiểm tra nhật ký kỹ thuật hoặc chỉnh sửa cấu hình trước khi chạy lại.")

        plan_data = {
            "job_id": clean_name,
            "display_name": source_path.name if source_path else f"{clean_name}.mp4",
            "source_file": source_info,
            "workspace_dir": str(ws_dir.resolve()) if ws_dir else None,
            "can_unpause": can_unpause,
            "is_completed": is_completed,
            "completed_output": str(completed_file.resolve()) if completed_file else None,
            "retry_count": retry_count,
            "max_retries": 3,
            "plan_token": plan_token,
            "summary": {
                "total_stages": len(stage_definitions),
                "reusable_stages": reusable_count,
                "rerun_stages": rerun_count,
                "first_rerun_stage": first_rerun_stage or "none",
            },
            "stages": stages_result,
            "warnings": warnings,
        }

        # Save to active tokens cache
        _ACTIVE_PLAN_TOKENS[plan_token] = {
            "job_id": clean_name,
            "created_at": time.time(),
            "plan": plan_data,
        }

        return plan_data

    def execute_resume_action(
        self,
        job_id: str,
        mode: str,
        plan_token: Optional[str] = None,
        config_overrides: Optional[Dict[str, Any]] = None,
        input_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Thực thi hành động theo 1 trong 3 chế độ:
        1. 'unpause': Bỏ trạng thái tạm dừng của tiến trình đang sống
        2. 'recover': Khôi phục từ checkpoint, tái sử dụng các artifact hợp lệ
        3. 'restart': Chạy lại mới hoàn toàn từ đầu
        """
        clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()

        # Action 1: Unpause
        if mode == "unpause":
            flag = self.control_dir / "video.pause"
            ack = self.control_dir / "video.pause.ack"
            flag.unlink(missing_ok=True)
            ack.unlink(missing_ok=True)
            return {
                "status": "success",
                "mode": "unpause",
                "job_id": clean_name,
                "message": "Đã tiếp tục tiến trình video đang tạm dừng.",
            }

        # Token validation for recover mode
        if mode == "recover":
            if not plan_token or plan_token not in _ACTIVE_PLAN_TOKENS:
                raise ValueError("Mã xác thực kế hoạch khôi phục (plan_token) không hợp lệ hoặc đã hết hạn. Vui lòng tải lại kế hoạch!")
            token_info = _ACTIVE_PLAN_TOKENS[plan_token]
            if token_info["job_id"] != clean_name:
                raise ValueError("plan_token không khớp với job_id được yêu cầu.")
            if time.time() - token_info["created_at"] > TOKEN_TTL_SECONDS:
                _ACTIVE_PLAN_TOKENS.pop(plan_token, None)
                raise ValueError("Kế hoạch khôi phục đã hết hạn (quá 15 phút). Vui lòng kiểm tra lại!")

        source_path = self.find_source_video(clean_name, input_dir=input_dir)
        if not source_path or not source_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy file video nguồn cho {clean_name}.")

        ws_dir = self.find_job_workspace(clean_name, source_path=source_path)

        # Atomic lock to prevent two workers claiming the job concurrently
        lock_fd = None
        try:
            lock_fd = open(self.lock_file, "w")
            # Simple non-blocking lock check
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        except (IOError, OSError):
            raise RuntimeError("Đang có tiến trình khác thực hiện khôi phục tác vụ. Vui lòng đợi trong giây lát!")

        try:
            states = self._read_states()
            prev_state = states.get(clean_name, {})
            current_retry = prev_state.get("retry_count", 0)

            if mode == "recover":
                if current_retry >= 3:
                    raise RuntimeError("Tác vụ đã vượt quá giới hạn thử lại tối đa (3 lần). Vui lòng chọn 'Chạy lại từ đầu' hoặc chỉnh sửa cấu hình.")
                plan = token_info["plan"]
                first_rerun = plan["summary"]["first_rerun_stage"]

                # Clean only downstream invalid artifacts to prevent mixed partial state
                if ws_dir and ws_dir.is_dir():
                    if first_rerun in ["tts_dubbing", "mix", "render"]:
                        # Remove downstream final renders and mixed audio
                        (ws_dir / f"final_{clean_name}.mp4").unlink(missing_ok=True)
                        (ws_dir / "mixed.wav").unlink(missing_ok=True)
                        (ws_dir / "mixed_audio.mp3").unlink(missing_ok=True)
                        (ws_dir / "pipeline_v2" / "artifacts" / "mix" / "mixed.wav").unlink(missing_ok=True)
                        (ws_dir / "pipeline_v2" / "artifacts" / "render" / "rendered.mp4").unlink(missing_ok=True)
                    if first_rerun == "tts_dubbing":
                        (ws_dir / "dubbed_voice.mp3").unlink(missing_ok=True)

                # Increment retry counter
                states[clean_name] = {
                    "source_fingerprint": plan["source_file"]["fingerprint"],
                    "retry_count": current_retry + 1,
                    "last_mode": "recover",
                    "checkpoint_stage": first_rerun,
                    "updated_at": time.time(),
                    "frozen_config": config_overrides or prev_state.get("frozen_config"),
                }
                self._write_states(states)

                # Consume token
                _ACTIVE_PLAN_TOKENS.pop(plan_token, None)

                return {
                    "status": "success",
                    "mode": "recover",
                    "job_id": clean_name,
                    "resume_from_stage": first_rerun,
                    "reused_stages": plan["summary"]["reusable_stages"],
                    "retry_count": current_retry + 1,
                    "source_video": str(source_path.resolve()),
                    "message": f"Đã bắt đầu khôi phục tác vụ '{source_path.name}' từ bước: {first_rerun}.",
                }

            elif mode == "restart":
                # Clear all previous checkpoints and temporary artifacts for this job
                if ws_dir and ws_dir.is_dir():
                    try:
                        for item in ws_dir.iterdir():
                            if item.is_file() and (item.suffix in [".srt", ".json", ".wav", ".mp3", ".mp4", ".tmp"] or "checkpoint" in item.name):
                                item.unlink(missing_ok=True)
                            elif item.is_dir() and item.name in ["dubbing", "pipeline_v2"]:
                                shutil.rmtree(item, ignore_errors=True)
                    except Exception as e:
                        logger.warning(f"Lỗi khi xóa checkpoint cũ của {clean_name}: {e}")

                # Reset retry count
                states[clean_name] = {
                    "source_fingerprint": compute_file_fingerprint(source_path),
                    "retry_count": 0,
                    "last_mode": "restart",
                    "checkpoint_stage": "extract_audio",
                    "updated_at": time.time(),
                    "frozen_config": config_overrides,
                }
                self._write_states(states)

                return {
                    "status": "success",
                    "mode": "restart",
                    "job_id": clean_name,
                    "resume_from_stage": "extract_audio",
                    "retry_count": 0,
                    "source_video": str(source_path.resolve()),
                    "message": f"Đã xóa toàn bộ checkpoint cũ và bắt đầu chạy lại từ đầu video '{source_path.name}'.",
                }
            else:
                raise ValueError(f"Chế độ chạy '{mode}' không được hỗ trợ. Chỉ chấp nhận 'recover', 'restart' hoặc 'unpause'.")

        finally:
            if lock_fd:
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                    lock_fd.close()
                except Exception:
                    pass
