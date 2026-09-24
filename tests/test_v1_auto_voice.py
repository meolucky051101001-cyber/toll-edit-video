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

    def tearDown(self):
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
        self.assertEqual(locked["rule"], "fallback_default_voice")

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
            # Write dummy audio
            with open(p, "wb") as f:
                f.write(b"RIFF" + b"\x00" * 200)
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


if __name__ == "__main__":
    unittest.main()
