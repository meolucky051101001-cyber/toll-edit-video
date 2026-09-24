import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

import sys
V1_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V1_ROOT / "backend"))

from ai.v1_auto_voice import (
    estimate_f0_pitch,
    detect_first_speaker_gender,
    resolve_locked_voice,
    lock_video_voice,
    get_locked_voice,
    VOICE_FEMALE_ID,
    VOICE_MALE_ID,
    VOICE_MALE_PARAM,
    VOICE_LOCK_FILENAME,
)
from ai.voice_cloning import generate_dubbing_audio, TTSIncompleteError


class TestV1AutoVoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.voice_checks_dir = V1_ROOT / "workspace_backup_safety" / "control" / "voice_checks"
        cls.real_male_sample = cls.voice_checks_dir / "capcut-BV075_streaming.mp3"
        cls.real_namminh_sample = cls.voice_checks_dir / "microsoft-namminh.mp3"
        cls.real_female_sample = cls.voice_checks_dir / "capcut-BV562_streaming.mp3"
        cls.real_hoaimy_sample = cls.voice_checks_dir / "microsoft-hoaimy.mp3"

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_v1_voice_")
        self.workspace = os.path.join(self.temp_dir, "workspace")
        os.makedirs(self.workspace, exist_ok=True)
        # Co lap cache hoan toan vao thu muc temp cua test (Codex Point 1.1)
        from ai import v1_voice_cache
        self.orig_cache_dir = v1_voice_cache.GLOBAL_CACHE_DIR
        self.test_cache_dir = Path(self.temp_dir) / "test_voice_cache"
        self.test_cache_dir.mkdir(parents=True, exist_ok=True)
        v1_voice_cache.GLOBAL_CACHE_DIR = self.test_cache_dir

    def tearDown(self):
        from ai import v1_voice_cache
        v1_voice_cache.GLOBAL_CACHE_DIR = self.orig_cache_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_synthetic_wav(self, path: str, f0_hz: float, duration_s: float = 1.5, sr: int = 16000):
        import soundfile as sf
        t = np.linspace(0, duration_s, int(sr * duration_s))
        audio = 0.6 * np.sin(2 * np.pi * f0_hz * t) + 0.3 * np.sin(2 * np.pi * 2 * f0_hz * t)
        fade = int(sr * 0.05)
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
        sf.write(path, audio.astype(np.float32), sr)

    def test_01_real_voice_samples_pitch_and_gender(self):
        """Kiem tra nhan dien cao do F0 tren MAU GIONG NOI THUC TE (Codex Requirement 4)."""
        import librosa

        # 1. Real Male: CapCut BV075 (Thanh Niên Tự Tin)
        self.assertTrue(self.real_male_sample.is_file(), f"Missing: {self.real_male_sample}")
        y_m, sr_m = librosa.load(str(self.real_male_sample), sr=16000, duration=2.5)
        f0_m, count_m, conf_m, gender_m, hnr_m = estimate_f0_pitch(y_m, sr_m)
        self.assertEqual(gender_m, "male")
        self.assertGreater(hnr_m, 5.0)  # Real voice has high harmonicity
        self.assertGreater(conf_m, 0.70)
        self.assertLess(f0_m, 175.0)

        # 2. Real Female: CapCut BV562 (Mai)
        self.assertTrue(self.real_female_sample.is_file(), f"Missing: {self.real_female_sample}")
        y_f, sr_f = librosa.load(str(self.real_female_sample), sr=16000, duration=2.5)
        f0_f, count_f, conf_f, gender_f, hnr_f = estimate_f0_pitch(y_f, sr_f)
        self.assertEqual(gender_f, "female")
        self.assertGreater(hnr_f, 5.0)
        self.assertGreater(conf_f, 0.70)
        self.assertGreater(f0_f, 185.0)

    def test_02_first_speaker_male_real_audio_locks_capcut(self):
        """Mau giong thuc te NAM o cau dau -> Khoa CapCut Thanh Nien Tu Tin."""
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.5), content="Xin chào các bạn đã quay trở lại"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.8), end=timedelta(seconds=4.5), content="Hôm nay mình sẽ hướng dẫn tiếp"),
        ]

        out_job = os.path.join(self.temp_dir, "job_male_real")
        locked = lock_video_voice(out_job, segs, vocals_path=self.real_male_sample, workspace=self.workspace)

        self.assertEqual(locked["voice_id"], VOICE_MALE_ID)
        self.assertEqual(locked["voice_source"], "capcut")
        self.assertEqual(locked["voice_param"], VOICE_MALE_PARAM)
        self.assertEqual(locked["detected_gender"], "male")

        # Snapshot file phai ton tai
        lock_file = Path(out_job) / VOICE_LOCK_FILENAME
        self.assertTrue(lock_file.is_file())

    def test_03_first_speaker_female_real_audio_locks_chi_mai(self):
        """Mau giong thuc te NU o cau dau -> Khoa Chi Mai (chi-mai)."""
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.5), content="Kính chào quý vị và các bạn"),
        ]

        out_job = os.path.join(self.temp_dir, "job_female_real")
        locked = lock_video_voice(out_job, segs, vocals_path=self.real_female_sample, workspace=self.workspace)

        self.assertEqual(locked["voice_id"], VOICE_FEMALE_ID)
        self.assertEqual(locked["detected_gender"], "female")
        self.assertIn(locked["voice_source"], ("rvc", "capcut"))

    def test_04_strictly_uses_first_utterance_never_switches_to_later_speaker(self):
        """SIET CHAT: Chi xet cau thoai dau, khong nhay sang cau 2 du cau 2 la giong khac (Codex Point 3)."""
        # Video bat dau bang am thanh trang/nhiễu ở câu 1, câu 2 là giọng NỮ
        noise_wav = os.path.join(self.temp_dir, "noise_seg1.wav")
        import soundfile as sf
        sr = 16000
        noise = np.random.uniform(-0.005, 0.005, sr * 2)
        sf.write(noise_wav, noise.astype(np.float32), sr)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=1.5), content="Tiếng động nhỏ"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.0), end=timedelta(seconds=4.0), content="Lời nói rõ của nữ"),
        ]

        # detect_first_speaker_gender chi phan tich cau 1
        gender, conf, f0, seg_idx = detect_first_speaker_gender(audio_path=noise_wav, srt_segments=segs)
        self.assertEqual(seg_idx, 1)  # Phai luon la cau 1
        self.assertEqual(gender, "unknown")  # Vi cau 1 la nhieu, khong duoc lay giong nu cau 2!

        # Khi lock_video_voice chay voi audio nay, no phai fallback chu KHONG khoa thanh nu
        out_job = os.path.join(self.temp_dir, "job_fallback_noise")
        locked = lock_video_voice(out_job, segs, vocals_path=noise_wav, workspace=self.workspace)
        self.assertTrue(locked["rule"].startswith("fallback_"))

    def test_05_demucs_vocals_low_hnr_recovers_from_original_audio(self):
        """Khi vocals bi Demucs loc mat tan so / HNR thap, tu dong thu lai tren original_audio (Codex Point 3)."""
        # Gia lap vocals bi loi (im lang/nhiễu), nhung original_audio chua giong nam that
        silent_vocals = os.path.join(self.temp_dir, "silent_vocals.wav")
        import soundfile as sf
        sf.write(silent_vocals, np.zeros(16000 * 2, dtype=np.float32), 16000)

        real_original = str(self.real_male_sample)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.5), content="Xin chào các bạn"),
        ]

        gender, conf, f0, seg_idx = detect_first_speaker_gender(
            audio_path=silent_vocals,
            srt_segments=segs,
            backup_audio_path=real_original
        )

        self.assertEqual(gender, "male")
        self.assertGreater(conf, 0.70)
        self.assertEqual(seg_idx, 1)

    def test_06_dubbing_audio_voice_purity_no_silent_voice_mix(self):
        """Kiem tra toan luong TTS: KHONG BAO GIO tron giong giua video khi 1 cau loi (Codex Point 1)."""
        # Gia lap danh sach 3 segments
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="Câu thứ nhất"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.5), end=timedelta(seconds=4.0), content="Câu thứ hai bị lỗi"),
            SimpleNamespace(index=3, start=timedelta(seconds=4.5), end=timedelta(seconds=6.0), content="Câu thứ ba"),
        ]

        out_folder = os.path.join(self.temp_dir, "tts_pure")
        os.makedirs(out_folder, exist_ok=True)

        # Mock generate_single_tts: cau 1 va 3 thanh cong, cau 2 that bai sau retry
        async def mock_single_tts(seg, folder, source, param, key):
            if seg.index == 2:
                return None  # Failure
            p = os.path.join(folder, f"{seg.index}.mp3")
            # Tạo audio giả lập hợp lệ để test decodability
            self._create_synthetic_wav(p, f0_hz=150.0, duration_s=1.5)
            return {
                "index": seg.index,
                "path": p,
                "start": seg.start.total_seconds(),
                "end": seg.end.total_seconds(),
                "actual_audio_duration": 1.5,
                "content": seg.content
            }

        with patch("ai.voice_cloning.generate_single_tts", side_effect=mock_single_tts):
            # Goi generate_dubbing_audio voi giong locked la capcut-BV075_streaming
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(generate_dubbing_audio(
                    segs, out_folder, voice_source="capcut", voice_param="BV075_streaming"
                ))

            # Kiem tra nguyen nhan loi: Phai la TTSIncompleteError voi thong diep ro rang
            cause = ctx.exception.__cause__
            self.assertIsInstance(cause, TTSIncompleteError)
            self.assertIn("thất bại với giọng đã khóa", str(cause))
            cue2_audio = os.path.join(out_folder, "2.mp3")
            self.assertFalse(os.path.exists(cue2_audio), "File loi khong duoc phep ton tai tren dia!")

    def test_07_resume_and_persistence_integrity(self):
        """Kiem tra tinh ben vung khi retry/resume tu voice_lock.json."""
        out_job = os.path.join(self.temp_dir, "job_persist")
        os.makedirs(out_job, exist_ok=True)
        lock_file = Path(out_job) / VOICE_LOCK_FILENAME

        initial_data = {
            "voice_id": "capcut-BV075_streaming",
            "voice_source": "capcut",
            "voice_param": "BV075_streaming",
            "voice_label": "CapCut · Thanh Niên Tự Tin (Khóa theo giọng nam đầu video)",
            "detected_gender": "male",
            "confidence": 0.95,
            "rule": "first_speaker_male_capcut"
        }
        lock_file.write_text(json.dumps(initial_data), encoding="utf-8")

        # Goi lai lock_video_voice voi bat ky audio nao khac (vi du giong nu)
        segs = [SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=2.0), content="Test")]
        locked = lock_video_voice(out_job, segs, vocals_path=self.real_female_sample)

        # Phai luon giu nguyen ban ghi da khoa tu truoc
        self.assertEqual(locked["voice_id"], "capcut-BV075_streaming")
        self.assertEqual(locked["voice_param"], "BV075_streaming")

    def test_08_short_opening_cue_prioritized_over_longer_subsequent_cue(self):
        """Kiem tra cau mo dau ngan (0.35s) van duoc uu tien cao nhat, khong bi bo qua (Codex Point 4)."""
        male_wav = os.path.join(self.temp_dir, "short_male.wav")
        self._create_synthetic_wav(male_wav, f0_hz=125.0, duration_s=0.5)

        # Segment 1 ngan (0.35s) la giong Nam, Segment 2 dai (2.5s) la giong Nu
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=0.35), content="Này!"),
            SimpleNamespace(index=2, start=timedelta(seconds=0.5), end=timedelta(seconds=3.0), content="Chào các bạn đã đến kênh...")
        ]
        gender, conf, f0, seg_idx = detect_first_speaker_gender(
            audio_path=male_wav,
            srt_segments=segs
        )
        self.assertEqual(seg_idx, 1, "Phải luôn phân tích câu số 1, không được nhảy sang câu 2 dù câu 1 ngắn!")
        self.assertEqual(gender, "male")

    def test_09_cache_version_invalidation_prevents_legacy_v4_mix(self):
        """Kiem tra CACHE_KEY_VERSION = 5 loai bo hoan toan cache v4 co nguy co cuu ho lech giong (Codex Point 1)."""
        from ai.v1_voice_cache import voice_cache_key, CACHE_KEY_VERSION
        import hashlib

        self.assertEqual(CACHE_KEY_VERSION, 5)

        test_seg = SimpleNamespace(
            start=timedelta(seconds=0.0),
            end=timedelta(seconds=2.0),
            content="Xin chào các bạn"
        )
        key_v5 = voice_cache_key(test_seg, "capcut", "BV075_streaming")

        # Gia lap cach tinh key cua phien ban v4 cu
        target_dur = round((test_seg.end - test_seg.start).total_seconds(), 1)
        raw_v4 = [4, test_seg.content.strip(), "capcut", "BV075_streaming", None, target_dur]
        key_v4 = hashlib.sha256(json.dumps(raw_v4, ensure_ascii=False).encode("utf-8")).hexdigest()

        # Key v5 bat buoc phai khac Key v4 -> cache cu v4 hoan toan bi vo hieu hoa
        self.assertNotEqual(key_v5, key_v4)

    def test_10_auto_voice_toggle_and_persistence(self):
        """Kiem tra bat / tat / xem trang thai che do Auto Voice va ghi nhan file config nguyen tu."""
        from ai.v1_auto_voice import (
            get_auto_voice_enabled,
            set_auto_voice_enabled,
            get_auto_voice_mode,
            get_auto_voice_config_path,
        )
        self.assertTrue(get_auto_voice_enabled(self.workspace))
        self.assertEqual(get_auto_voice_mode(self.workspace), "auto")

        ok = set_auto_voice_enabled(False, updated_by="test_user", workspace=self.workspace)
        self.assertTrue(ok)
        self.assertFalse(get_auto_voice_enabled(self.workspace))
        self.assertEqual(get_auto_voice_mode(self.workspace), "manual")

        cfg_file = get_auto_voice_config_path(self.workspace)
        self.assertTrue(cfg_file.is_file())
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertFalse(data["enabled"])
        self.assertEqual(data["updated_by"], "test_user")

        ok = set_auto_voice_enabled(True, updated_by="test_user2", workspace=self.workspace)
        self.assertTrue(ok)
        self.assertTrue(get_auto_voice_enabled(self.workspace))
        self.assertEqual(get_auto_voice_mode(self.workspace), "auto")

    def test_11_decide_voice_manual_mode_bypasses_analysis(self):
        """Che do MANUAL: Bo qua hoan toan buoc tinh F0/HNR, analysis_time_sec = 0, lay giong thu cong."""
        from ai.v1_auto_voice import decide_video_voice
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.5), content="Xin chao cac ban"),
        ]
        out_job = os.path.join(self.temp_dir, "job_manual_mode")

        with patch("ai.v1_auto_voice.detect_first_speaker_gender") as mock_detect:
            locked = decide_video_voice(
                out_dir=out_job,
                srt_segments=segs,
                vocals_path=self.real_female_sample,
                voice_mode="manual",
                workspace=self.workspace,
            )
            mock_detect.assert_not_called()

        self.assertEqual(locked["voice_mode"], "manual")
        self.assertEqual(locked["analysis_time_sec"], 0.0)
        self.assertEqual(locked["detected_gender"], "manual")
        self.assertEqual(locked["rule"], "manual_selection")

    def test_12_decide_voice_auto_female_fails_closed_without_rvc(self):
        """Che do AUTO: Nguoi noi dau la Nu nhung thieu RVC runtime/model -> Phai raise RuntimeError truoc TTS."""
        from ai.v1_auto_voice import decide_video_voice
        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.5), content="Kính chào quý vị và các bạn"),
        ]
        out_job = os.path.join(self.temp_dir, "job_female_no_rvc")

        with patch("ai.v1_auto_voice.find_rvc_model_path", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                decide_video_voice(
                    out_dir=out_job,
                    srt_segments=segs,
                    vocals_path=self.real_female_sample,
                    voice_mode="auto",
                    workspace=self.workspace,
                )
            self.assertIn("RVC Chí Mai", str(ctx.exception))
            self.assertIn("Không âm thầm thay thế bằng CapCut Mai", str(ctx.exception))

    def test_13_snapshot_hash_and_mode_invalidation(self):
        """Kiem tra snapshot tu dong huy neu hash noi dung video doi hoac che do giong doi."""
        from ai.v1_auto_voice import decide_video_voice, get_locked_voice, compute_file_sha256

        dummy_video = os.path.join(self.temp_dir, "dummy.mp4")
        with open(dummy_video, "wb") as f:
            f.write(b"video content v1")

        out_job = os.path.join(self.temp_dir, "job_hash_test")
        segs = [SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=2.0), content="Test")]

        locked1 = decide_video_voice(
            out_dir=out_job,
            srt_segments=segs,
            vocals_path=self.real_male_sample,
            video_path=dummy_video,
            voice_mode="auto",
            workspace=self.workspace,
        )
        self.assertEqual(locked1["voice_mode"], "auto")

        with open(dummy_video, "wb") as f:
            f.write(b"video content v2 changed")

        new_hash = compute_file_sha256(dummy_video)
        stale = get_locked_voice(out_job, expected_video_hash=new_hash)
        self.assertIsNone(stale, "Snapshot cu phai bi loai bo khi video hash doi!")

        mode_mismatch = get_locked_voice(out_job, expected_mode="manual")
        self.assertIsNone(mode_mismatch, "Snapshot cu phai bi loai bo khi che do voice_mode doi!")

    def test_14_pause_gate_allows_voice_auto(self):
        """Kiem tra tool_control_runtime cho phep /voice_auto di qua ke ca khi bot dang bi pause."""
        from tool_control_runtime import is_allowed_command_during_pause

        self.assertTrue(is_allowed_command_during_pause("/voice_auto"))
        self.assertTrue(is_allowed_command_during_pause("/voice_auto on"))
        self.assertTrue(is_allowed_command_during_pause("/voice_auto off"))
        self.assertTrue(is_allowed_command_during_pause("/voice"))
        self.assertTrue(is_allowed_command_during_pause("/start"))
        self.assertFalse(is_allowed_command_during_pause("/batch"))
        self.assertFalse(is_allowed_command_during_pause("/llm"))

    def test_15_dual_directory_synchronization_and_verification(self):
        """Kiem tra dong bo 100% giua workspace/bot_system/control va workspace/control (Codex Point 1 & 2)."""
        from ai.v1_auto_voice import (
            set_auto_voice_enabled,
            get_auto_voice_enabled,
            CONFIG_FILENAME,
        )

        ctrl1 = Path(self.workspace) / "bot_system" / "control"
        ctrl2 = Path(self.workspace) / "control"
        ctrl1.mkdir(parents=True, exist_ok=True)
        ctrl2.mkdir(parents=True, exist_ok=True)

        # Ban dau co tinh dat 2 file lech nhau
        (ctrl1 / CONFIG_FILENAME).write_text(json.dumps({"enabled": False}), encoding="utf-8")
        (ctrl2 / CONFIG_FILENAME).write_text(json.dumps({"enabled": True}), encoding="utf-8")

        # Goi set_auto_voice_enabled(True)
        ok = set_auto_voice_enabled(True, updated_by="test_sync", workspace=self.workspace)
        self.assertTrue(ok)

        # Ca 2 file deu phai la True
        data1 = json.loads((ctrl1 / CONFIG_FILENAME).read_text(encoding="utf-8"))
        data2 = json.loads((ctrl2 / CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.assertTrue(data1["enabled"])
        self.assertTrue(data2["enabled"])
        self.assertEqual(data1["updated_by"], "test_sync")
        self.assertEqual(data2["updated_by"], "test_sync")

        # Goi set_auto_voice_enabled(False)
        ok = set_auto_voice_enabled(False, updated_by="test_sync2", workspace=self.workspace)
        self.assertTrue(ok)
        data1 = json.loads((ctrl1 / CONFIG_FILENAME).read_text(encoding="utf-8"))
        data2 = json.loads((ctrl2 / CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.assertFalse(data1["enabled"])
        self.assertFalse(data2["enabled"])

        # Kiem tra neu write loi hoac verify khong khop thi return False
        with patch("os.replace", side_effect=OSError("Disk write error")):
            fail_ok = set_auto_voice_enabled(True, updated_by="test_err", workspace=self.workspace)
            self.assertFalse(fail_ok)


if __name__ == "__main__":
    unittest.main()
