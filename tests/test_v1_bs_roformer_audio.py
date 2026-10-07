import os
import sys
import unittest
from pathlib import Path

# Add backend to path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from model_workers.v1_separator_worker import classify_outputs
from v1_audio_mixer import AdaptiveDuckingSettings, calculate_adaptive_duck_gain, merge_ducking_intervals
from v1_separator_lock import separator_gpu_lock
import audio_settings
import job_tracker


class TestBSRoFormerAudio(unittest.TestCase):
    def test_stem_classification_never_confuses_no_vocals(self):
        """Đảm bảo không bao giờ nhầm lẫn file instrumental/no_vocals với vocals."""
        # 1. Output chuẩn của audio-separator với BS-RoFormer
        files = [
            "/output/song_(Instrumental)_model_bs_roformer.wav",
            "/output/song_(Vocals)_model_bs_roformer.wav"
        ]
        voc, inst = classify_outputs(files, "/output")
        self.assertTrue("(vocals)" in voc.name.lower())
        self.assertTrue("(instrumental)" in inst.name.lower())

        # 2. Case tên file chứa từ 'no_vocals'
        files2 = [
            "/output/no_vocals.wav",
            "/output/vocals.wav"
        ]
        voc2, inst2 = classify_outputs(files2, "/output")
        self.assertEqual(voc2.name, "vocals.wav")
        self.assertEqual(inst2.name, "no_vocals.wav")

        # 3. Thứ tự đảo ngược
        files3 = [
            "/output/track_(Vocals).wav",
            "/output/track_no_vocals.wav"
        ]
        voc3, inst3 = classify_outputs(files3, "/output")
        self.assertEqual(voc3.name, "track_(Vocals).wav")
        self.assertEqual(inst3.name, "track_no_vocals.wav")

    def test_adaptive_ducking_modes(self):
        """Kiểm tra các chế độ hạ âm lượng BGM (Auto-Ducking)."""
        # Soft mode (mặc định)
        soft_settings = AdaptiveDuckingSettings(ducking_mode="soft")
        self.assertEqual(soft_settings.soft_duck_db, -1.5)
        self.assertEqual(soft_settings.attack_ms, 250)
        self.assertEqual(soft_settings.release_ms, 500)
        self.assertEqual(soft_settings.gap_merge_ms, 500)

        # Kiểm tra mức hạ âm lượng thực tế cho nhạc nền vừa/nhẹ (-30 dBFS)
        gain_soft = calculate_adaptive_duck_gain(-30.0, soft_settings)
        self.assertEqual(gain_soft, -1.5)

        # Medium mode
        medium_settings = AdaptiveDuckingSettings(ducking_mode="medium")
        gain_medium = calculate_adaptive_duck_gain(-30.0, medium_settings)
        self.assertEqual(gain_medium, -3.5)

        # Off mode (không dìm nhạc)
        off_settings = AdaptiveDuckingSettings(ducking_mode="off")
        gain_off = calculate_adaptive_duck_gain(-30.0, off_settings)
        self.assertEqual(gain_off, 0.0)

    def test_merge_ducking_intervals_anti_pumping(self):
        """Đảm bảo các khoảng lặng ngắn giữa 2 câu thoại được gộp lại, tránh hiện tượng pumping."""
        settings = AdaptiveDuckingSettings(ducking_mode="soft", gap_merge_ms=500)
        # Hai câu thoại cách nhau 300ms (nhỏ hơn gap_merge_ms=500ms)
        dubs = [
            {"start": 1.0, "duration": 2.0},  # 1000ms -> 3000ms
            {"start": 3.3, "duration": 2.0},  # 3300ms -> 5300ms
        ]
        intervals = merge_ducking_intervals(dubs, total_duration_ms=10000, gap_merge_ms=500)
        # Khoảng cách 300ms < 500ms => gộp thành 1 interval duy nhất
        self.assertEqual(len(intervals), 1)
        self.assertLessEqual(intervals[0][0], 1000)
        self.assertGreaterEqual(intervals[0][1], 5300)

    def test_separator_gpu_lock_concurrency(self):
        """Đảm bảo khóa độc quyền GPU hoạt động và có thể giải phóng."""
        with separator_gpu_lock(timeout_seconds=5.0):
            # Trong lock, file separator.lock phải tồn tại
            lock_path = BACKEND_DIR.parent / "workspace" / "bot_system" / "separator.lock"
            self.assertTrue(lock_path.exists())

    def test_audio_settings_persistence(self):
        """Kiểm tra lưu và đọc cấu hình âm lượng / separation_mode / ducking_mode."""
        orig = audio_settings.get_audio_settings()
        try:
            res = audio_settings.save_audio_settings(
                bgm_volume_db=-3.5,
                dubbing_volume_db=2.0,
                separation_mode="roformer",
                ducking_mode="soft",
            )
            self.assertEqual(res["status"], "ok")
            cur = audio_settings.get_audio_settings()
            self.assertEqual(cur["bgm_volume_db"], -3.5)
            self.assertEqual(cur["dubbing_volume_db"], 2.0)
            self.assertEqual(cur["separation_mode"], "roformer")
            self.assertEqual(cur["ducking_mode"], "soft")
        finally:
            # Khôi phục cấu hình ban đầu
            audio_settings.save_audio_settings(
                bgm_volume_db=orig.get("bgm_volume_db", -2.0),
                dubbing_volume_db=orig.get("dubbing_volume_db", 1.0),
                separation_mode=orig.get("separation_mode", "roformer"),
                ducking_mode=orig.get("ducking_mode", "soft"),
            )

    def test_job_tracker_recording(self):
        """Kiểm tra ghi nhận separator_info và mixer_info vào job_tracker (cô lập hoàn toàn khỏi production)."""
        import tempfile
        import copy
        orig_file = job_tracker.STATUS_FILE
        orig_state = copy.deepcopy(job_tracker._CURRENT_STATE)
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_status_file = Path(tmpdir) / "temp_job_status.json"
            job_tracker.STATUS_FILE = temp_status_file
            try:
                test_sep = {
                    "engine": "bs_roformer",
                    "model": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
                    "device": "cuda:0",
                    "device_name": "NVIDIA GeForce RTX 4050 Laptop GPU",
                    "inference_time_s": 11.25,
                    "peak_vram_mb": 1238.9,
                }
                test_mix = {
                    "ducking_mode": "soft",
                    "bgm_volume_db": -2.0,
                    "dubbing_volume_db": 1.0,
                }
                job_tracker.record_separator_info(test_sep)
                job_tracker.record_mixer_info(test_mix)

                status = job_tracker.get_status()
                self.assertIsNotNone(status.get("separator_info"))
                self.assertEqual(status["separator_info"]["engine"], "bs_roformer")
                self.assertEqual(status["separator_info"]["inference_time_s"], 11.25)
                self.assertIsNotNone(status.get("mixer_info"))
                self.assertEqual(status["mixer_info"]["ducking_mode"], "soft")
            finally:
                job_tracker.STATUS_FILE = orig_file
                job_tracker._CURRENT_STATE = orig_state

    def test_localized_soft_peak_limiter(self):
        """Đảm bảo peak limiter cục bộ chỉ nén đỉnh vượt ngưỡng, không dìm toàn bộ bản nhạc."""
        import numpy as np
        # Giả lập mảng âm thanh với 1 đỉnh đột biến duy nhất ở giữa
        samples = np.array([0.1, 0.2, 0.5, 1.8, 0.5, 0.2, 0.1], dtype=np.float32)
        ceiling = 10.0 ** (-1.0 / 20.0)  # ~0.89125
        knee_threshold = ceiling * 0.90   # ~0.8021
        abs_samples = np.abs(samples)
        over_mask = abs_samples > knee_threshold

        excess = abs_samples[over_mask] - knee_threshold
        scale_headroom = ceiling - knee_threshold
        compressed = knee_threshold + scale_headroom * np.tanh(excess / scale_headroom)
        limited = samples.copy()
        limited[over_mask] = np.sign(limited[over_mask]) * np.minimum(compressed, ceiling)

        # 1. Các mẫu bình thường <= knee_threshold phải giữ nguyên 100%
        self.assertAlmostEqual(limited[0], 0.1, places=5)
        self.assertAlmostEqual(limited[1], 0.2, places=5)
        self.assertAlmostEqual(limited[2], 0.5, places=5)
        self.assertAlmostEqual(limited[4], 0.5, places=5)

        # 2. Mẫu đỉnh 1.8 phải được nén êm ái và không vượt quá trần ceiling
        self.assertLessEqual(limited[3], ceiling + 1e-6)
        self.assertGreater(limited[3], knee_threshold)

    def test_smart_frozen_config_resolution(self):
        """Đảm bảo resolve_job_frozen_config tìm thấy snapshot từ các đường dẫn out_dir/original.wav, out_dir/mixed.wav."""
        import tempfile
        from job_config_service import freeze_job_config, get_frozen_config, clear_frozen_config
        import job_config_service

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_frozen_file = job_config_service.FROZEN_FILE
            job_config_service.FROZEN_FILE = Path(tmpdir) / "job_frozen_configs.json"
            try:
                # 1. Đóng băng cho video 'test_sample_vid.mp4'
                snapshot = freeze_job_config(
                    video_name="test_sample_vid.mp4",
                    overrides={
                        "separation_mode": "bypass",
                        "ducking_mode": "off",
                        "bgm_volume_db": -5.0,
                    }
                )
                self.assertIsNotNone(snapshot)

                # 2. Tra cứu bằng tên file chính xác
                f1 = get_frozen_config("test_sample_vid.mp4")
                self.assertIsNotNone(f1)
                self.assertEqual(f1["effective_config"]["separation_mode"], "bypass")

                # 3. Tra cứu bằng đường dẫn out_dir/original.wav
                f2 = get_frozen_config(r"C:\tool v1\workspace\test_sample_vid\original.wav")
                self.assertIsNotNone(f2)
                self.assertEqual(f2["effective_config"]["separation_mode"], "bypass")

                # 4. Tra cứu bằng đường dẫn batch out_dir/mixed.wav
                f3 = get_frozen_config(r"C:\tool v1\workspace\batch_96bc4638_test_sample_vid\mixed.wav")
                self.assertIsNotNone(f3)
                self.assertEqual(f3["effective_config"]["ducking_mode"], "off")

                # 5. Tra cứu bằng folder name không đuôi
                f4 = get_frozen_config("test_sample_vid")
                self.assertIsNotNone(f4)
            finally:
                job_config_service.FROZEN_FILE = orig_frozen_file

    def test_separate_vocals_demucs_file_not_found_raises(self):
        """Đảm bảo separate_vocals_demucs raise FileNotFoundError nếu file đầu vào không tồn tại (fail-closed)."""
        from video_utils import separate_vocals_demucs
        with self.assertRaises(FileNotFoundError):
            separate_vocals_demucs("C:\\non_existent_folder\\fake_file_xyz123.wav", "C:\\temp")

    def test_validate_separated_audio_channel_and_sample_rate_strict(self):
        """Đảm bảo validate_separated_audio reject stem nếu không đủ kênh stereo hoặc sample rate quá thấp."""
        from model_workers.v1_separator_worker import validate_separated_audio
        import soundfile as sf
        import tempfile
        import numpy as np

        with tempfile.TemporaryDirectory() as tmpdir:
            # Tạo file 1 kênh mono
            mono_file = Path(tmpdir) / "mono.wav"
            data = np.ones((16000,), dtype=np.float32) * 0.1
            sf.write(str(mono_file), data, 16000)

            # Đòi hỏi 2 kênh stereo -> phải raise ValueError
            with self.assertRaises(ValueError) as cm:
                validate_separated_audio(mono_file, expected_duration=1.0, stem_name="Instrumental", expected_channels=2)
            self.assertIn("số kênh không đạt yêu cầu", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

