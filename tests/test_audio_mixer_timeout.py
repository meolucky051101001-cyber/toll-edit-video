"""M4: ffmpeg runs are bounded by a timeout and never orphan a child process."""

import asyncio

import pytest

from backend.core.config import settings
from backend.services.dubbing.audio_mixer import AudioMixer


class FakeProc:
    def __init__(self, *, hang: bool = False, returncode: int = 0, stderr: bytes = b""):
        self.returncode = None if hang else returncode
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.waited = False
        self._done = asyncio.Event()
        if not hang:
            self._done.set()

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return b"", self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self):
        self.waited = True
        await self._done.wait()
        return self.returncode


def _setup_paths(tmp_path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"audio")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"bin")
    out_dir = tmp_path / "out"
    return str(voice), str(video), str(ffmpeg), str(out_dir)


def _patch_exec(monkeypatch, proc):
    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.anyio
async def test_hung_ffmpeg_times_out_kills_and_falls_back(tmp_path, monkeypatch):
    voice, video, ffmpeg, out_dir = _setup_paths(tmp_path)
    monkeypatch.setattr(settings, "ffmpeg_timeout_seconds", 0.2)
    proc = FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)

    mixer = AudioMixer(ffmpeg_bin=ffmpeg)
    audio, dubbed = await mixer.mix(voice, video, output_dir=out_dir)

    assert dubbed is None, "a hung ffmpeg must fall back to the audio-only asset"
    assert audio == voice
    assert proc.killed, "the hung child must be killed"
    assert proc.waited, "the killed child must be reaped (no orphan)"


@pytest.mark.anyio
async def test_cancelling_mix_kills_the_child(tmp_path, monkeypatch):
    voice, video, ffmpeg, out_dir = _setup_paths(tmp_path)
    monkeypatch.setattr(settings, "ffmpeg_timeout_seconds", 300)
    proc = FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)

    mixer = AudioMixer(ffmpeg_bin=ffmpeg)
    task = asyncio.create_task(mixer.mix(voice, video, output_dir=out_dir))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed, "cancellation must kill the child"
    assert proc.waited, "cancellation must reap the child (no orphan)"


@pytest.mark.anyio
async def test_successful_mix_returns_dubbed_video(tmp_path, monkeypatch):
    voice, video, ffmpeg, out_dir = _setup_paths(tmp_path)
    monkeypatch.setattr(settings, "ffmpeg_timeout_seconds", 300)
    proc = FakeProc(returncode=0)
    _patch_exec(monkeypatch, proc)
    expected = tmp_path / "out" / "voice_dubbed.mp4"
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b"dubbed")

    mixer = AudioMixer(ffmpeg_bin=ffmpeg)
    audio, dubbed = await mixer.mix(voice, video, output_dir=out_dir)

    assert dubbed == str(expected)
    assert not proc.killed, "a clean exit must not be killed"


@pytest.mark.anyio
async def test_failed_ffmpeg_returns_audio_only(tmp_path, monkeypatch):
    voice, video, ffmpeg, out_dir = _setup_paths(tmp_path)
    monkeypatch.setattr(settings, "ffmpeg_timeout_seconds", 300)
    proc = FakeProc(returncode=1, stderr=b"some error")
    _patch_exec(monkeypatch, proc)

    mixer = AudioMixer(ffmpeg_bin=ffmpeg)
    audio, dubbed = await mixer.mix(voice, video, output_dir=out_dir)

    assert dubbed is None
    assert audio == voice
    assert not proc.killed