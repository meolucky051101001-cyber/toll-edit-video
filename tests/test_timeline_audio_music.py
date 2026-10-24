"""Test decode nhạc/SFX + music filter chain + waveform peaks (P5)."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

from workflow_api import _build_music_filter_lines, _decode_audio_assets  # noqa: E402

# WAV 16-bit PCM mono tối thiểu (header 44 bytes + 4 sample bytes)
_WAV_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80?\x00\x00\x02\x00\x10\x00data\x04\x00\x00\x00\x00\x00\x00\x00"
)


class TestDecodeAudioAssets(unittest.TestCase):
    def test_valid_wav_dataurl(self):
        dataurl = "data:audio/wav;base64," + base64.b64encode(_WAV_BYTES).decode()
        with tempfile.TemporaryDirectory() as td:
            paths = _decode_audio_assets([{"id": "au_1", "src": dataurl}], Path(td))
            self.assertTrue(paths[0].is_file())
            self.assertEqual(paths[0].suffix, ".wav")
            self.assertEqual(paths[0].parent.name, "audio_assets")
            self.assertTrue(paths[0].read_bytes().startswith(b"RIFF"))

    def test_mp3_alias(self):
        dataurl = "data:audio/mpeg;base64," + base64.b64encode(b"ID3 fake").decode()
        with tempfile.TemporaryDirectory() as td:
            paths = _decode_audio_assets([{"id": "m", "src": dataurl}], Path(td))
            self.assertEqual(paths[0].suffix, ".mp3")

    def test_invalid_src_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            paths = _decode_audio_assets([{"id": "x", "src": "data:image/png;base64,AAAA"}], Path(td))
            self.assertEqual(paths, [None])


class TestMusicFilterLines(unittest.TestCase):
    def _meta(self, **kw):
        m = {"a_idx": 1, "at_out": 2.0, "dur_out": 4.0, "volume": 1.0,
             "fade_in": 0.0, "fade_out": 0.0, "muted": False, "duck": False}
        m.update(kw)
        return m

    def test_all_muted_keeps_base_audio(self):
        r = _build_music_filter_lines([self._meta(muted=True)], "[basea]", 10.0)
        self.assertEqual(r["lines"], ["[basea]anull[outa]"])
        self.assertFalse(r["used_split"])

    def test_single_clip_no_duck(self):
        r = _build_music_filter_lines([self._meta(volume=0.5, fade_in=0.5)], "[basea]", 10.0)
        joined = ";".join(r["lines"])
        self.assertIn("[basea]", joined)  # base trực tiếp vào amix (không asplit)
        self.assertIn("aformat=sample_rates=44100:channel_layouts=stereo", joined)
        self.assertIn("atrim=start=0:end=4.000", joined)
        self.assertIn("volume=0.50000", joined)
        self.assertIn("afade=t=in:st=0:d=0.500", joined)
        self.assertIn("adelay=2000:all=1", joined)  # tại giây 2
        self.assertIn("amix=inputs=2:normalize=0:duration=longest", joined)
        self.assertIn("apad=whole_dur=10.000[outa]", joined)
        # không duck → không dùng sidechaincompress
        self.assertNotIn("sidechaincompress", joined)

    def test_fade_out_position(self):
        r = _build_music_filter_lines([self._meta(fade_out=1.0)], "[basea]", 10.0)
        self.assertIn("afade=t=out:st=3.000:d=1.000", ";".join(r["lines"]))

    def test_ducking_uses_sidechain_and_split(self):
        r = _build_music_filter_lines([self._meta(duck=True)], "[basea]", 10.0)
        joined = ";".join(r["lines"])
        self.assertIn("[basea]asplit=2[bs0][bs1]", joined)
        self.assertIn("[bs0]", joined)  # base vào amix qua nhánh split đầu
        self.assertIn("sidechaincompress=threshold=0.03:ratio=6:attack=80:release=500[amd0]", joined)
        self.assertIn("amix=inputs=2:", joined)
        self.assertTrue(r["used_split"])

    def test_two_clips_one_ducked(self):
        r = _build_music_filter_lines(
            [self._meta(a_idx=1, duck=True), self._meta(a_idx=2, at_out=6.0)], "[basea]", 12.0
        )
        joined = ";".join(r["lines"])
        self.assertIn("asplit=2", joined)  # chỉ 1 clip duck
        self.assertIn("[1:a]", joined)
        self.assertIn("[2:a]", joined)
        self.assertIn("adelay=6000:all=1", joined)
        self.assertIn("amix=inputs=3:", joined)  # base + 2 nhạc

    def test_mixed_duck_and_muted(self):
        r = _build_music_filter_lines(
            [self._meta(a_idx=1, muted=True), self._meta(a_idx=2, duck=True)], "[basea]", 10.0
        )
        joined = ";".join(r["lines"])
        self.assertNotIn("[1:a]", joined)  # clip muted bị bỏ hẳn
        self.assertIn("[2:a]", joined)
        self.assertIn("amix=inputs=2:", joined)


if __name__ == "__main__":
    unittest.main()
