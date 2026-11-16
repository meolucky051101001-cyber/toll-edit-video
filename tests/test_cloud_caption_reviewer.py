import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend.cloud.gpu_scripts import caption_reviewer as reviewer


def install_reader_fakes(monkeypatch, generate):
    import cv2
    from google import genai

    class Capture:
        def set(self, *_):
            pass

        def read(self):
            return True, np.zeros((100, 100, 3), dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: Capture())
    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(
        models=SimpleNamespace(generate_content=generate)))

    def extract(args, **_):
        Path(args[-1]).write_bytes(b"original-speed-audio-evidence")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(reviewer.subprocess, "run", extract)
    monkeypatch.setattr(reviewer.time, "sleep", lambda *_: None)


def cue(sid=1):
    return {"id": sid, "start": 1.0, "end": 2.0, "text": "我盯着镜头开",
            "best_block": {"text": "我盯着镜头拍所以没对准"}}


def test_unsupported_proposal_retains_original_without_raising():
    proposal = {"id": 1, "corrected_text": "另一个句子",
                "caption_reads": [{"sample_time": t, "text": "另一个句子"}
                                  for t in (1.38, 1.5, 1.62)]}
    verdict = reviewer.validate_review_proposal(cue(), proposal, (1.38, 1.5, 1.62),
                                                audio_sha256="a" * 64)
    assert not verdict["accepted"] and verdict["text"] == cue()["text"]


def test_live_and_cached_review_apply_same_bounded_substitution(tmp_path, monkeypatch):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs["model"])
        return SimpleNamespace(text=json.dumps({"id": 1,
            "corrected_text": "我盯着镜头拍所以没对准", "audio_transcript": "我盯着镜头拍",
            "caption_reads": [{"sample_time": t, "text": "我盯着镜头拍所以没对准"}
                              for t in (1.38, 1.5, 1.62)]}))

    install_reader_fakes(monkeypatch, generate)
    audit = tmp_path / "review.json"
    live = reviewer.review_caption_conflicts("input.mp4", [cue()], "synthetic", audit)
    assert live[0]["text"] == "我盯着镜头拍"
    assert len(calls) == 1
    replay = reviewer.review_caption_conflicts("input.mp4", [cue()], "synthetic", audit)
    assert replay == live
    assert len(calls) == 1
    assert replay[0]["start"] == 1.0 and replay[0]["end"] == 2.0
    assert json.loads(audit.read_text(encoding="utf-8"))["correction_count"] == 1


def test_provider_quota_exhaustion_fails_closed_and_skips_unavailable_models(tmp_path, monkeypatch):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("429 RESOURCE_EXHAUSTED synthetic")

    install_reader_fakes(monkeypatch, generate)
    segments = [cue(1), cue(2)]
    original = copy.deepcopy(segments)
    audit = tmp_path / "review.json"
    with pytest.raises(RuntimeError, match="bounded retries"):
        reviewer.review_caption_conflicts("input.mp4", segments, "synthetic", audit)
    assert len(calls) == 3 and len(set(calls)) == 3
    assert segments == original
    report = json.loads(audit.read_text(encoding="utf-8"))
    assert report["failed_cue_ids"] == [1, 2]
    assert report["correction_count"] == 0
