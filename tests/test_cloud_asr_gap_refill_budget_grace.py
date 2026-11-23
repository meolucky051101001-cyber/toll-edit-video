"""Cloud regressions from 23-11: Whisper dropping a 30 s window tail (job d98ef8)
and one failed Colab balance probe killing running jobs (be2abe/d635c0)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.cloud import cloud_orchestrator
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_contract import GPU_CHECK_TASK
from backend.cloud.cloud_job_store import _normalize_provider_usage_payload
from backend.cloud.gpu_scripts import full_pipeline

GAP = {"start": 24.38, "end": 30.0}


def _seg(start, end, text, **extra):
    step = (end - start) / len(text)
    words = [{"word": ch, "start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3)}
             for i, ch in enumerate(text)]
    return dict({"start": start, "end": end, "text": text, "words": words, "avg_logprob": -0.1}, **extra)


KNOWN = [
    _seg(0.0, 4.0, "假如我自己家盖房子", id=0), _seg(4.0, 8.0, "身为内行人我会怎么盖", id=1),
    _seg(8.0, 12.0, "首先基础我肯定直接上", id=2), _seg(12.0, 16.0, "说实话确实贵几万块钱", id=3),
    _seg(16.0, 20.0, "但调形基础又是挖坑", id=4), _seg(20.0, 24.38, "这点钱花的值", id=5),
    _seg(30.0, 35.0, "哪怕就是外面淹了", id=6), _seg(35.0, 40.0, "我也不用担心反潮", id=7),
]
# Segments recovered when decoding 24.08-30.30s on its own (shape of the real replay).
REFILL = [
    _seg(23.9, 24.36, "这点钱花的值"),
    _seg(24.30, 25.34, "地基一完工"),
    _seg(25.34, 28.58, "我就让工人顺手帮我把房子周围做一圈"),
    _seg(28.58, 30.14, "这样以后没雨季回南天"),
]


def _clone(segments):
    return [json.loads(json.dumps(s, ensure_ascii=False)) for s in segments]


def test_refill_merges_only_segments_inside_the_gap_and_clamps_them():
    result = {"segments": _clone(KNOWN)}
    clips_seen = []

    def transcribe_clip(clips):
        clips_seen.append(clips)
        return {"segments": _clone(REFILL)}

    report = full_pipeline._refill_uncovered_speech(result, [GAP], transcribe_clip, 40 * 16000)

    assert clips_seen == [[24.08, 30.3]]
    assert report == [{"start": 24.38, "end": 30.0, "added": 3}]
    texts = [s["text"] for s in result["segments"]]
    assert texts[5:9] == ["这点钱花的值", "地基一完工", "我就让工人顺手帮我把房子周围做一圈", "这样以后没雨季回南天"]
    assert len(texts) == len(KNOWN) + 3
    assert [s["id"] for s in result["segments"]] == list(range(len(texts)))
    added = result["segments"][6:9]
    assert added[0]["start"] == 24.38 and added[0]["words"][0]["start"] == 24.38
    assert added[-1]["end"] == 30.0 and added[-1]["words"][-1]["end"] == 30.0
    assert full_pipeline._asr_uncovered_speech(result["segments"], [{"start": 0.0, "end": 40.0}]) == []


def test_refill_leaves_result_untouched_when_nothing_new_is_found():
    result = {"segments": _clone(KNOWN)}
    report = full_pipeline._refill_uncovered_speech(
        result, [GAP], lambda _clips: {"segments": _clone(KNOWN)}, 40 * 16000)
    assert report == [{"start": 24.38, "end": 30.0, "added": 0}]
    assert result["segments"] == KNOWN


def _run_asr(tmp_path, monkeypatch, refill_segments):
    total = 40.0
    audio = [0] * int(total * 16000)
    calls = []

    def transcribe(target, **options):
        calls.append(options["clip_timestamps"])
        if options["clip_timestamps"] == [0.0, total]:
            return {"segments": _clone(KNOWN)}
        return {"segments": _clone(refill_segments)}

    windows = [{"start": 0.0, "end": total}]
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(
        load_model=lambda *_a, **_k: SimpleNamespace(transcribe=transcribe)))
    monkeypatch.setattr(full_pipeline, "_asr_speech_audio", lambda *_a: (
        audio, {"speech_windows_seconds": windows, "track_windows_seconds": {"original": windows}}))
    monkeypatch.setattr(full_pipeline, "validate_asr_source_timeline", lambda *_a: total)
    srt = tmp_path / "refill.srt"
    return srt, calls, lambda: full_pipeline.step3_asr_whisper("vocals.wav", str(srt))


def test_asr_recovers_window_tail_gap_instead_of_failing(tmp_path, monkeypatch):
    srt, calls, run = _run_asr(tmp_path, monkeypatch, REFILL)
    cues = run()
    assert calls == [[0.0, 40.0], [24.08, 30.3]]
    assert "地基一完工" in "".join(c["text"] for c in cues)
    audit = json.loads(srt.with_suffix(".asr.json").read_text(encoding="utf-8"))
    assert audit["gap_refill"] == [{"start": 24.38, "end": 30.0, "added": 3}]
    assert audit["uncovered_speech"] == []


def test_asr_still_fails_closed_when_refill_finds_no_speech(tmp_path, monkeypatch):
    srt, calls, run = _run_asr(tmp_path, monkeypatch, [])
    with pytest.raises(full_pipeline.PipelineStageError, match="ASR bỏ trống vùng có lời") as error:
        run()
    assert error.value.error_code == "ASR_TIMING_INVALID"
    assert calls == [[0.0, 40.0], [24.08, 30.3]]
    assert not srt.exists()


class _Store:
    def record_provider_usage_snapshot(self, usage):
        return _normalize_provider_usage_payload(usage)

    def record_runtime_usage(self, *_args, **_kwargs):
        return {"within_budget": True}


class _Adapter:
    def __init__(self, responses):
        self.responses = list(responses)

    def get_usage(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


GOOD = {"ok": True, "balance": 370.0, "usage_rate": 1.15, "active_assignments": 1}
DOWN = {"ok": False, "error": "colab usage timed out"}


def _run(responses):
    job = SimpleNamespace(job_id="cloud-test-grace", attempt=1, owner_id="w1", task_type=GPU_CHECK_TASK)
    orchestrator = SimpleNamespace(store=_Store(), adapter=_Adapter(responses))
    run = cloud_orchestrator._JobRun.from_job(orchestrator, job)
    run.session_may_exist = True
    run.runtime_start_time = time.time()
    return run


def test_single_probe_failure_after_fresh_balance_does_not_abort(monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "provider_balance_stale_seconds", 300)
    run = _run([GOOD, DOWN, RuntimeError("wsl crashed"), GOOD])
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    assert run.provider_probe_failures == 2 and run.budget_abort_code == ""
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    assert run.provider_probe_failures == 0


def test_probe_failure_aborts_without_a_fresh_good_balance(monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "provider_balance_stale_seconds", 300)
    run = _run([DOWN])
    assert run._sync_runtime_usage(check_provider=True) == (False, "budget_uncertain")
    assert run.budget_abort_code == "budget_uncertain"

    run = _run([GOOD, DOWN])
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    run.last_provider_ok_ts = time.time() - 301
    assert run._sync_runtime_usage(check_provider=True) == (False, "budget_uncertain")
    assert "bị cũ" in run.budget_abort_reason


def test_grace_zero_restores_fail_fast(monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "provider_balance_stale_seconds", 0)
    run = _run([GOOD, DOWN])
    assert run._sync_runtime_usage(check_provider=True) == (True, "ok")
    assert run._sync_runtime_usage(check_provider=True) == (False, "budget_uncertain")
