import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

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


class TestV1AutoVoice(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_v1_voice_")
        self.workspace = os.path.join(self.temp_dir, "workspace")
        os.makedirs(self.workspace, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_synthetic_wav(self, path: str, f0_hz: float, duration_s: float = 1.5, sr: int = 16000):
        import soundfile as sf
        t = np.linspace(0, duration_s, int(sr * duration_s))
        # Fundamental + harmonic to simulate voiced speech
        audio = 0.6 * np.sin(2 * np.pi * f0_hz * t) + 0.3 * np.sin(2 * np.pi * 2 * f0_hz * t)
        # Add smooth envelope
        fade = int(sr * 0.05)
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
        sf.write(path, audio.astype(np.float32), sr)

    def test_01_f0_pitch_estimation_accuracy(self):
        """Kiem tra uoc tinh cao do F0 cho am thanh nam va nu."""
        sr = 16000
        t = np.linspace(0, 1.0, sr)

        # Giong nam tieu bieu: 125 Hz
        y_male = 0.8 * np.sin(2 * np.pi * 125 * t)
        f0_m, count_m, conf_m, gender_m = estimate_f0_pitch(y_male, sr)
        self.assertEqual(gender_m, "male")
        self.assertAlmostEqual(f0_m, 125.0, delta=5.0)
        self.assertGreater(conf_m, 0.80)

        # Giong nu tieu bieu: 230 Hz
        y_female = 0.8 * np.sin(2 * np.pi * 230 * t)
        f0_f, count_f, conf_f, gender_f = estimate_f0_pitch(y_female, sr)
        self.assertEqual(gender_f, "female")
        self.assertAlmostEqual(f0_f, 230.0, delta=5.0)
        self.assertGreater(conf_f, 0.80)

    def test_02_first_speaker_male_locks_capcut(self):
        """Nguoi noi dau tien la NAM -> Khoa CapCut Thanh Nien Tu Tin (BV075_streaming)."""
        male_wav = os.path.join(self.temp_dir, "vocals_male.wav")
        self._create_synthetic_wav(male_wav, f0_hz=125.0, duration_s=2.0)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.2), end=timedelta(seconds=2.0), content="Chào mừng các bạn"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.2), end=timedelta(seconds=4.0), content="Hôm nay trời rất đẹp"),
        ]

        out_job = os.path.join(self.temp_dir, "job_male")
        locked = lock_video_voice(out_job, segs, vocals_path=male_wav, workspace=self.workspace)

        self.assertEqual(locked["voice_id"], VOICE_MALE_ID)
        self.assertEqual(locked["voice_source"], "capcut")
        self.assertEqual(locked["voice_param"], VOICE_MALE_PARAM)
        self.assertEqual(locked["detected_gender"], "male")
        self.assertIn("Thanh Niên Tự Tin", locked["voice_label"])

        # File voice_lock.json phai duoc tao
        lock_file = Path(out_job) / VOICE_LOCK_FILENAME
        self.assertTrue(lock_file.is_file())
        saved_data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["voice_id"], VOICE_MALE_ID)

    def test_03_first_speaker_female_locks_chi_mai(self):
        """Nguoi noi dau tien la NU -> Khoa Chi Mai (chi-mai)."""
        female_wav = os.path.join(self.temp_dir, "vocals_female.wav")
        self._create_synthetic_wav(female_wav, f0_hz=235.0, duration_s=2.0)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.3), end=timedelta(seconds=2.2), content="Xin chào quý vị khán giả"),
        ]

        out_job = os.path.join(self.temp_dir, "job_female")
        locked = lock_video_voice(out_job, segs, vocals_path=female_wav, workspace=self.workspace)

        self.assertEqual(locked["voice_id"], VOICE_FEMALE_ID)
        self.assertEqual(locked["detected_gender"], "female")
        # Phai ho tro RVC hoac fallback CapCut Mai neu chua co model
        self.assertIn(locked["voice_source"], ("rvc", "capcut"))

    def test_04_subsequent_speakers_do_not_change_voice(self):
        """Giong xuat hien ve sau KHONG LAM HE THONG DOI GIONG (Nguyen tac bat di bat dich)."""
        # Video bat dau bang giong NAM (0.0s - 2.0s)
        # Nhung ve sau xuat hien giong NU (2.5s - 5.0s)
        mixed_wav = os.path.join(self.temp_dir, "dialogue.wav")
        import soundfile as sf
        sr = 16000
        t1 = np.linspace(0, 2.0, sr * 2)
        audio_male = 0.6 * np.sin(2 * np.pi * 125.0 * t1)
        t_gap = np.zeros(int(sr * 0.5))
        t2 = np.linspace(0, 2.5, int(sr * 2.5))
        audio_female = 0.6 * np.sin(2 * np.pi * 230.0 * t2)
        full_audio = np.concatenate([audio_male, t_gap, audio_female])
        sf.write(mixed_wav, full_audio.astype(np.float32), sr)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=1.9), content="Đoạn thoại đầu của nam"),
            SimpleNamespace(index=2, start=timedelta(seconds=2.6), end=timedelta(seconds=4.9), content="Đoạn thoại sau của nữ"),
        ]

        out_job = os.path.join(self.temp_dir, "job_dialogue")
        locked = lock_video_voice(out_job, segs, vocals_path=mixed_wav, workspace=self.workspace)

        # Do cau dau la NAM, giong phai duoc khoa la NAM cho den het video
        self.assertEqual(locked["voice_id"], VOICE_MALE_ID)
        self.assertEqual(locked["voice_param"], VOICE_MALE_PARAM)

        # Goi lai lock_video_voice (vi du o buoc tiep theo hoac resume):
        locked_again = lock_video_voice(out_job, segs, vocals_path=mixed_wav, workspace=self.workspace)
        self.assertEqual(locked_again["voice_id"], VOICE_MALE_ID)

    def test_05_persistence_and_resume_immunity(self):
        """Kiem tra tinh ben vung: Resume/Retry luon dung lai voice_lock.json da luu."""
        out_job = os.path.join(self.temp_dir, "job_resume")
        os.makedirs(out_job, exist_ok=True)
        lock_file = Path(out_job) / VOICE_LOCK_FILENAME

        # Gia su job da duoc khoa giong capcut-BV075_streaming tu truoc
        pre_locked = {
            "voice_id": "capcut-BV075_streaming",
            "voice_source": "capcut",
            "voice_param": "BV075_streaming",
            "voice_label": "CapCut · Thanh Niên Tự Tin (Khóa theo giọng nam đầu video)",
            "detected_gender": "male",
            "confidence": 0.95,
            "rule": "first_speaker_male_capcut"
        }
        lock_file.write_text(json.dumps(pre_locked), encoding="utf-8")

        # Goi lock_video_voice voi audio nu hoac segments khac
        female_wav = os.path.join(self.temp_dir, "female_noise.wav")
        self._create_synthetic_wav(female_wav, f0_hz=250.0, duration_s=1.0)
        segs = [SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=1.0), content="Sub moi")]

        result = lock_video_voice(out_job, segs, vocals_path=female_wav)

        # Ket qua phai tuyet doi giu nguyen ban ghi da khoa cu
        self.assertEqual(result["voice_id"], "capcut-BV075_streaming")
        self.assertEqual(result["voice_source"], "capcut")
        self.assertEqual(result["voice_param"], "BV075_streaming")

    def test_06_uncertain_noisy_audio_fallback(self):
        """Am thanh khong ro / nhieu trang -> Fallback ve giong mac dinh an toan."""
        noise_wav = os.path.join(self.temp_dir, "noise.wav")
        import soundfile as sf
        sr = 16000
        noise = np.random.uniform(-0.01, 0.01, sr * 2)
        sf.write(noise_wav, noise.astype(np.float32), sr)

        segs = [
            SimpleNamespace(index=1, start=timedelta(seconds=0.1), end=timedelta(seconds=1.8), content="Tiếng xì xào"),
        ]

        out_job = os.path.join(self.temp_dir, "job_noise")
        locked = lock_video_voice(out_job, segs, vocals_path=noise_wav, workspace=self.workspace)

        # Phai fallback an toan, khong throw exception
        self.assertIn("voice_id", locked)
        self.assertIn("voice_source", locked)
        self.assertEqual(locked["rule"], "fallback_default_voice")


if __name__ == "__main__":
    unittest.main()
