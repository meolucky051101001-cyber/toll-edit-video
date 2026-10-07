"""Offline regression checks: no provider calls, model loads or real video jobs."""
import ast
import asyncio
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
import srt
from pydub import AudioSegment

DEFAULT_CODE = Path(__file__).resolve().parents[1] / "backend" if Path(__file__).parent.name == "tests" else Path(__file__).parent
CODE = Path(os.environ.get("V1_STABILITY_TEST_CODE", DEFAULT_CODE))
PROD = Path(r"C:\tool v1\backend")
TEMP = tempfile.TemporaryDirectory(prefix="v1-stability-test-")
SCRATCH = Path(TEMP.name)
sys.path[:0] = [str(CODE), str(PROD)]

pkg = types.ModuleType("ai")
pkg.__path__ = [str(CODE / "ai"), str(PROD / "ai")]
sys.modules["ai"] = pkg
sys.modules["shared_state"] = types.SimpleNamespace(stop_requested=False)
sys.modules["job_tracker"] = types.SimpleNamespace(record_translation_model=lambda *a: None,
    record_active_translation_model=lambda *a: None, get_status=lambda: {})
sys.modules["ai.transcription"] = types.SimpleNamespace(save_srt=lambda *a: None, extract_subtitles_whisper=lambda *a, **k: None)
sys.modules["v1_stage_metrics"] = types.SimpleNamespace(stage=lambda *a, **k: lambda f: f)


def load(name, file, omit=()):
    """Import real code, excluding only import-time production DB/cache writers."""
    source = (CODE / file).read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(CODE / file))
    tree.body = [n for n in tree.body if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in omit for t in n.targets))]
    mod = types.ModuleType(name)
    mod.__file__ = str(SCRATCH / "backend" / file)
    mod.__package__ = name.rpartition(".")[0]
    sys.modules[name] = mod
    exec(compile(tree, str(CODE / file), "exec"), mod.__dict__)
    return mod


dispatcher = load("ai.v1_gemini_dispatcher", "ai/v1_gemini_dispatcher.py", ("circuit_breaker",))
response = load("ai.v1_gemini_response", "ai/v1_gemini_response.py")
retry = load("v1_retry_policy", "v1_retry_policy.py")
guard = load("v1_speech_guard", "v1_speech_guard.py")
voice = load("ai.voice_cloning", "ai/voice_cloning.py")
translation = load("ai.translation", "ai/translation.py")
cache = load("ai.v1_voice_cache", "ai/v1_voice_cache.py")
qc = load("v1_quality_gate", "v1_quality_gate.py")
isolated = load("ai.v1_voice_isolated", "ai/v1_voice_isolated.py")
mixer = load("v1_audio_mixer", "v1_audio_mixer.py")
hierarchy = load("v1_hierarchical_mixer", "v1_hierarchical_mixer.py")


def reply(values, reason="STOP"):
    return {"candidates": [{"finishReason": reason, "content": {"parts": [{"text": json.dumps(values, ensure_ascii=False)}]}}]}


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.breaker = Mock()
        self.breaker.get_candidate_models.return_value = [("high", "ACTIVE"), ("low", "ACTIVE")]
        dispatcher.circuit_breaker = self.breaker
        dispatcher._record_dispatcher_active_model = Mock()
        dispatcher._record_dispatcher_success_model = Mock()

    def dispatch(self, replies, **kw):
        fake = [Mock(status_code=status, headers={}, json=Mock(return_value=data)) for status, data in replies]
        with patch("requests.post", side_effect=fake) as post, patch.object(dispatcher, "get_gemini_api_key", return_value="synthetic-key"):
            result = dispatcher.call_gemini_api({}, models=["high", "low"], response_validator=lambda d: response.translation_response(d, 2), **kw)
            return result, post.call_count

    def test_partial_200_tries_next_model(self):
        result, count = self.dispatch([(200, reply(["xin chào"])), (200, reply(["xin", "chào"]))])
        self.assertEqual((result[1], count), ("low", 2))
        self.assertEqual(self.breaker.record_success.call_count, 1)
        self.assertEqual(self.breaker.record_failure.call_count, 1)

    def test_empty_200_tries_next(self):
        _, count = self.dispatch([(200, {}), (200, reply(["xin", "chào"]))])
        self.assertEqual(count, 2)

    def test_auth_stops_chain(self):
        with self.assertRaises(dispatcher.GeminiAuthError):
            self.dispatch([(403, {}), (200, reply(["xin", "chào"]))])
        self.breaker.record_success.assert_not_called()

    def test_bad_payload_stops_chain(self):
        with self.assertRaises(dispatcher.GeminiRequestError):
            self.dispatch([(400, {"error": {"message": "Invalid contents"}})])

    def test_cancel_before_network(self):
        with self.assertRaises(dispatcher.GeminiCancelledError):
            self.dispatch([], stop_checker=lambda: True)

    def test_cancel_after_response(self):
        calls = iter([False, False, True])
        with self.assertRaises(dispatcher.GeminiCancelledError):
            self.dispatch([(200, reply(["xin", "chào"]))], stop_checker=lambda: next(calls))
        self.breaker.record_success.assert_not_called()

    def test_expired_deadline(self):
        with self.assertRaises(dispatcher.GeminiDeadlineError):
            self.dispatch([], overall_deadline=time.monotonic() - 1)

    def test_failed_response_retains_cause(self):
        with self.assertRaises(dispatcher.GeminiAllModelsFailedError) as caught:
            self.dispatch([(200, reply([])), (200, reply([]))])
        self.assertIsInstance(caught.exception.__cause__, response.GeminiResponseError)
        self.assertFalse(retry.should_retry_job(caught.exception))

    def test_redacts_non_google_key(self):
        value = dispatcher.mask_secret("https://sample/?key=SYNTHETIC-SECRET&x=1 api_key=another-secret token=third-secret")
        for secret in ("SYNTHETIC-SECRET", "another-secret", "third-secret"):
            self.assertNotIn(secret, value)
        self.assertIn("&x=1", value)

    def test_circuit_peek_does_not_reserve_unused_models(self):
        import sqlite3
        db = SCRATCH / "circuit-peek.db"
        breaker = dispatcher.GeminiCircuitBreaker(db)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO model_circuit (account_hash,model,purpose,state,cooldown_until,consecutive_failures) VALUES ('acc','m','translation','COOLDOWN',0,1)")
        self.assertEqual(breaker.get_candidate_models('acc', 'translation', ['m'], reserve_probe=False), [('m', 'HALF_OPEN')])
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT state FROM model_circuit").fetchone()[0], 'COOLDOWN')
        self.assertEqual(breaker.get_candidate_models('acc', 'translation', ['m']), [('m', 'HALF_OPEN')])
        self.assertEqual(breaker.get_candidate_models('acc', 'translation', ['m']), [])

    def test_circuit_recovers_expired_half_open_probe(self):
        import sqlite3
        db = SCRATCH / "circuit-expired.db"
        breaker = dispatcher.GeminiCircuitBreaker(db)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO model_circuit (account_hash,model,purpose,state,cooldown_until,consecutive_failures) VALUES ('acc','m','translation','HALF_OPEN',0,1)")
        self.assertEqual(breaker.get_candidate_models('acc', 'translation', ['m']), [('m', 'HALF_OPEN')])
        self.assertEqual(breaker.get_candidate_models('acc', 'translation', ['m']), [])


class ResponseTests(unittest.TestCase):
    def test_fenced_json(self):
        data = reply([])
        data["candidates"][0]["content"]["parts"][0]["text"] = '```json\n["chào"]\n```'
        self.assertEqual(response.translation_response(data, 1), ["chào"])

    def test_dict_text(self):
        self.assertEqual(response.translation_response(reply([{"text": " chào "}]), 1), ["chào"])

    def test_invalid_types_and_counts(self):
        for value in ([], ["a", "b"], [None], [1], [""], [{}]):
            with self.subTest(value=value), self.assertRaises(response.GeminiResponseError):
                response.translation_response(reply(value), 1)

    def test_cjk_and_truncated_response(self):
        for data in (reply(["中文"]), reply(["chào"], "MAX_TOKENS"), {"candidates": []}):
            with self.subTest(data=data), self.assertRaises(response.GeminiResponseError):
                response.translation_response(data, 1)


class RetryTests(unittest.TestCase):
    def test_transient_network(self):
        import requests
        response = requests.Response()
        response.status_code = 503
        for err in (requests.Timeout(), requests.ConnectionError(), requests.HTTPError(response=response), ConnectionError(), TimeoutError(), RuntimeError("HTTP 503 from model")):
            with self.subTest(err=err):
                self.assertTrue(retry.should_retry_job(err))

    def test_permanent_errors(self):
        for err in (SyntaxError(), ImportError(), FileNotFoundError(), guard.SpeechTimingError(), qc.CriticalQualityError(), RuntimeError("unsupported filter"), dispatcher.GeminiAuthError()):
            with self.subTest(err=err):
                self.assertFalse(retry.should_retry_job(err))

    def test_limits_and_cancel(self):
        self.assertFalse(retry.should_retry_job(TimeoutError(), 2))
        self.assertFalse(retry.should_retry_job(TimeoutError(), stopped=True))
        self.assertFalse(retry.should_retry_job(RuntimeError("Batch stop requested")))
        self.assertFalse(retry.should_retry_job(asyncio.CancelledError()))

    def test_wrapped_transient(self):
        err = RuntimeError("worker failed")
        err.__cause__ = ConnectionError()
        self.assertTrue(retry.should_retry_job(err))
        err.__cause__ = guard.SpeechTimingError()
        self.assertFalse(retry.should_retry_job(err))


def cue(index, start, end, text="xin chào", gender=None, param="Mai"):
    seg = srt.Subtitle(index, timedelta(seconds=start), timedelta(seconds=end), text)
    seg.voice_source, seg.voice_param, seg.gender = "capcut", param, gender
    return seg


class SpeechTests(unittest.TestCase):
    def test_40ms_cue_join_keeps_words(self):
        cues = [cue(375, 0, .04, "xin"), cue(376, .04, 2, "chào"), cue(377, 3, 4)]
        guard.coalesce_tiny_same_voice_cues(cues)
        self.assertEqual([s.index for s in cues], [375, 377])
        self.assertEqual(cues[0].content, "xin chào")
        self.assertGreater(voice.calculate_reading_windows(cues)[375], 2)

    def test_do_not_merge_opposite_gender_or_voice(self):
        for options in ({"gender": "male"}, {"param": "Thanh nien"}):
            cues = [cue(1, 0, .04, gender="female"), cue(2, .04, 1, **options)]
            guard.coalesce_tiny_same_voice_cues(cues)
            self.assertEqual(len(cues), 2)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(guard.SpeechTimingError):
            guard.coalesce_tiny_same_voice_cues([cue(1, 0, 1), cue(1, 2, 3)])

    def test_reading_windows_keep_start(self):
        cues = [cue(1, 0, 1), cue(2, 2, 3)]
        self.assertEqual(voice.calculate_reading_windows(cues, 4), {1: 1.95, 2: 1.95})
        self.assertEqual(cues[1].start.total_seconds(), 2)

    def test_guard_measures_real_file_not_metadata(self):
        with self.assertRaises(guard.SpeechTimingError):
            guard.validate_speech_timeline([{"index": 1, "path": "a", "start": 0, "duration": .1}, {"index": 2, "path": "b", "start": 2}], measure=lambda p: 2.008 if p == "a" else .5)

    def test_guard_last_cue_and_speed(self):
        for d in ({"index": 1, "path": "a", "start": 1.9}, {"index": 1, "path": "a", "start": 0, "speed_ratio": 20.64}):
            with self.subTest(d=d), self.assertRaises(guard.SpeechTimingError):
                guard.validate_speech_timeline([d], total_duration=2, measure=lambda p: 1, max_speed=1.35)

    def test_guard_5ms_tolerance(self):
        self.assertEqual(len(guard.validate_speech_timeline([{"path": "a", "start": 0}], total_duration=1, measure=lambda p: 1.004)), 1)

    def test_native_fit_small_overflow(self):
        rate = 44100
        path = SCRATCH / "fit-small.wav"
        sf.write(path, .1 * np.sin(2 * np.pi * 230 * np.arange(int(2.088 * rate)) / rate), rate)
        result, duration, speed = asyncio.run(voice.fit_audio_file(str(path), 2.03, str(SCRATCH / "fit-small.mp3")))
        self.assertLessEqual(sf.info(result).duration, 2.035)
        self.assertGreater(speed, 1)
        self.assertLessEqual(speed, 1.35)

    def test_speed_cap_defers_no_chipmunk(self):
        path = SCRATCH / "too-long.wav"
        sf.write(path, np.ones(44100, dtype=np.float32) * .1, 44100)
        result, dur, speed = asyncio.run(voice.fit_audio_file(str(path), .04, str(SCRATCH / "deferred.wav")))
        self.assertGreater(dur, .9)
        self.assertGreater(speed, 1.35)

    def test_ffmpeg_failure_not_original_success(self):
        path = SCRATCH / "fit-fail.wav"
        sf.write(path, np.ones(44100, dtype=np.float32) * .1, 44100)
        with patch("batch_control.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")), self.assertRaises(guard.SpeechTimingError):
            asyncio.run(voice.fit_audio_file(str(path), .9, str(SCRATCH / "fit-fail.mp3")))
        self.assertTrue(path.is_file())


class QCAndCacheTests(unittest.TestCase):
    def test_warnings_never_critical(self):
        checks = [qc.QCCheckItem("asr", "asr_coverage", "WARN", "unknown"), qc.QCCheckItem("sub", "visual_mask_coverage", "UNKNOWN", "inspect")]
        self.assertEqual(qc.critical_failures(checks), [])

    def test_integrity_fails_critical(self):
        for name in ("tts_no_collisions", "tts_cue_coverage", "duration_alignment", "source_geometry_fps", "no_cjk_residual", "final_stream_layout"):
            with self.subTest(name=name):
                self.assertEqual(len(qc.critical_failures([qc.QCCheckItem("tts", name, "FAIL", "failed")])), 1)

    def test_missing_output_blocks_report_only_and_saves_report(self):
        with self.assertRaises(qc.CriticalQualityError):
            qc.run_quality_gate("missing-test", SCRATCH / "missing.mp4", policy="REPORT_ONLY", workspace_path=SCRATCH)
        report = json.loads((SCRATCH / "bot_system/qc_reports/missing-test.qc.json").read_text(encoding="utf-8"))
        self.assertEqual(report["overall_status"], "FAILED")

    def test_cache_key_includes_speed_and_exact_window(self):
        seg = cue(1, 0, 1)
        a = cache.voice_cache_key(seg, "capcut", "Mai", 1.001)
        b = cache.voice_cache_key(seg, "capcut", "Mai", 1.004)
        self.assertNotEqual(a, b)
        with patch.dict(os.environ, V1_MAX_NATURAL_SPEED="1.20"):
            self.assertNotEqual(a, cache.voice_cache_key(seg, "capcut", "Mai", 1.001))


class BatchTests(unittest.TestCase):
    def test_dual_voice_map_never_silently_falls_back(self):
        cues = [cue(1, 0, 1), cue(2, 2, 3)]
        with self.assertRaises(voice.TTSIncompleteError):
            asyncio.run(voice.generate_dubbing_audio(cues, str(SCRATCH / "voice-map"), segment_voices={"1": {"source": "capcut", "param": "Mai"}}))

    def test_post_condense_budget_survives_slow_tts(self):
        clock = [100.0]
        calls = []
        cues = [cue(1, 0, 2, "câu dài")]
        async def generate(seg, folder, src, param, key, target_max_duration=None):
            clock[0] += 120
            return {"index": seg.index, "path": "x", "start": 0, "actual_audio_duration": 2.5,
                    "speed_ratio": 2, "content": seg.content}
        def condense(items, **kw):
            calls.append(kw["deadline"] - clock[0])
            self.assertEqual(kw["api_key"], "synthetic-gemini-key")
            return {1: "câu ngắn"}
        fake_cache = types.SimpleNamespace(voice_cache_key=lambda *a, **k: "k", read_voice_cache=lambda *a, **k: None, write_voice_cache=lambda *a, **k: None)
        with patch.dict(sys.modules, {"ai.v1_voice_cache": fake_cache}), patch.dict(os.environ, GEMINI_API_KEY="synthetic-gemini-key"), patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(voice, "generate_single_tts", side_effect=generate), patch.object(translation, "condense_vietnamese_subtitles_batch", side_effect=condense), patch.object(guard, "validate_speech_timeline"), patch.object(voice.os.path, "exists", return_value=True), patch.object(voice.os.path, "getsize", return_value=1000):
            asyncio.run(voice.generate_dubbing_audio(cues, str(SCRATCH / "batch"), "capcut", "Mai", api_key="synthetic-voice-key", video_duration=2))
        self.assertTrue(calls)
        self.assertGreater(calls[-1], 0)

    def test_translation_shared_deadline_all_chunks(self):
        calls = []
        def dispatch(**kw):
            calls.append(kw)
            return reply(["chào"] * (40 if len(calls) == 1 else 1)), "high"
        fake_cache = types.SimpleNamespace(cache_key=lambda *a: "key", read_cache=lambda *a: None, write_cache=Mock())
        with patch.dict(sys.modules, {"ai.v1_translation_cache": fake_cache}), patch.object(dispatcher, "call_gemini_api", side_effect=dispatch), patch.object(translation, "extract_video_frames_base64", return_value=[]) as frames:
            result = translation.translate_with_gemini(["中文"] * 41, api_key="synthetic", overall_deadline=12345)
        self.assertEqual(frames.call_count, 1)
        self.assertEqual(len(result), 41)
        self.assertEqual([c["overall_deadline"] for c in calls], [12345, 12345])
        self.assertTrue(all(callable(c["response_validator"]) for c in calls))

    def test_invalid_translation_never_cached_or_padded(self):
        fake_cache = types.SimpleNamespace(cache_key=lambda *a: "key", read_cache=lambda *a: None, write_cache=Mock())
        with patch.dict(sys.modules, {"ai.v1_translation_cache": fake_cache}), patch.object(dispatcher, "call_gemini_api", return_value=(reply(["chào"]), "high")), patch.object(translation, "extract_video_frames_base64", return_value=[]):
            self.assertIsNone(translation.translate_with_gemini(["中文", "中文"], api_key="synthetic"))
        fake_cache.write_cache.assert_not_called()

    def test_translation_cancel_propagates(self):
        fake_cache = types.SimpleNamespace(cache_key=lambda *a: "key", read_cache=lambda *a: None, write_cache=Mock())
        with patch.dict(sys.modules, {"ai.v1_translation_cache": fake_cache}), patch.object(dispatcher, "call_gemini_api", side_effect=dispatcher.GeminiCancelledError()), patch.object(translation, "extract_video_frames_base64", return_value=[]), self.assertRaises(dispatcher.GeminiCancelledError):
            translation.translate_with_gemini(["中文"], api_key="synthetic")

    def test_invalid_condensation_never_cached(self):
        fake_cache = types.SimpleNamespace(read_condense_cache=lambda *a: None, write_condense_cache=Mock())
        items = [{"index": 1, "text": "Tôi không có 5 nghìn", "target_seconds": 1, "target_words": 5}]
        with patch.dict(sys.modules, {"ai.v1_translation_cache": fake_cache}), patch.object(translation, "call_gemini_api", return_value=(reply({"1": "Tôi có tiền"}), "high")):
            self.assertEqual(translation.condense_vietnamese_subtitles_batch(items, api_key="synthetic"), {})
        fake_cache.write_condense_cache.assert_not_called()


class MixerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.rate = 16000
        self.bgm = SCRATCH / "mixer-bgm.wav"
        self.dub = SCRATCH / "mixer-dub.wav"
        sf.write(self.bgm, np.zeros(self.rate * 6, dtype=np.float32), self.rate)
        sf.write(self.dub, .1 * np.sin(2 * np.pi * 200 * np.arange(self.rate * 2) / self.rate), self.rate)

    def test_direct_refuses_silent_speech_clipping(self):
        dubs = [{"index": 1, "path": str(self.dub), "start": 0}, {"index": 2, "path": str(self.dub), "start": 1}]
        with self.assertRaises(guard.SpeechTimingError):
            mixer.mix_adaptive_audio(str(self.bgm), dubs, str(SCRATCH / "must-not-publish.wav"))

    def test_hierarchy_refuses_global_overlap_before_slices(self):
        dubs = [{"index": 1, "path": str(self.dub), "start": 1.9}, {"index": 2, "path": str(self.dub), "start": 3}]
        with self.assertRaises(guard.SpeechTimingError):
            hierarchy.mix_hierarchical_audio(str(self.bgm), dubs, str(SCRATCH / "must-not-publish-h.wav"), cluster_size_s=2)

    def test_cluster_crossing_keeps_full_tail(self):
        path = SCRATCH / "cluster-ok.wav"
        hierarchy.mix_hierarchical_audio(str(self.bgm), [{"index": 1, "path": str(self.dub), "start": 1}], str(path), cluster_size_s=2)
        audio, rate = sf.read(path)
        self.assertAlmostEqual(len(audio) / rate, 6, places=3)
        self.assertGreater(np.max(np.abs(audio[int(2.2 * rate):int(2.8 * rate)])), .02)
        self.assertLess(np.max(np.abs(audio[int(3.2 * rate):])), .001)

    def test_qc_report_only_blocks_proven_overlap(self):
        from v1_media_streams import MainVideo
        output = SCRATCH / "qc-fixture.mp4"
        output.write_bytes(b"fixture-not-real-video" * 1500)
        ass = SCRATCH / "qc-fixture.ass"
        ass.write_text("[Script Info]\n" + "; padding\n" * 20 + "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,xin chào\n", encoding="utf-8")
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop: {7: 150, 5: 25, 3: 1800, 4: 1080}.get(prop, 0)
        cap.read.return_value = (True, None)
        with patch("v1_media_streams.probe_main_video", return_value=MainVideo(0, 1800, 1080, 25, 6, 1, 1, False)), patch("cv2.VideoCapture", return_value=cap):
            with self.assertRaises(qc.CriticalQualityError):
                qc.run_quality_gate("overlap", output, translated_subtitles=[cue(1, 0, 2), cue(2, 1, 3)],
                    dubbing_audio_files=[{"index": 1, "path": str(self.dub), "start": 0}, {"index": 2, "path": str(self.dub), "start": 1}],
                    mixed_audio_path=self.bgm, ass_subtitle_path=ass, policy="REPORT_ONLY", workspace_path=SCRATCH)

    def test_cache_ignores_forged_short_duration(self):
        path = SCRATCH / "cache-long.wav"
        sf.write(path, np.ones(32000, dtype=np.float32) * .1, 16000)
        cache.write_voice_cache(path, "synthetic-cache-key", .2, "test", skip_global=True)
        self.assertIsNone(cache.read_voice_cache(path, "synthetic-cache-key", max_duration=1))

    def test_reversed_or_nonfinite_timeline_rejected(self):
        for start in (-1, math.inf, math.nan):
            with self.subTest(start=start), self.assertRaises(guard.SpeechTimingError):
                guard.validate_speech_timeline([{"path": str(self.dub), "start": start}], total_duration=6)

    def test_verified_fixture_with_subjective_warnings_is_publishable(self):
        from v1_media_streams import MainVideo
        output = SCRATCH / "qc-valid-metadata-fixture.mp4"
        output.write_bytes(b"mock-video-stream" * 2000)
        ass = SCRATCH / "qc-valid.ass"
        ass.write_text("[Script Info]\n" + "; padding\n" * 20 + "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,xin chào\n", encoding="utf-8")
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop: {7: 150, 5: 25, 3: 1800, 4: 1080}.get(prop, 0)
        cap.read.return_value = (True, None)
        with patch("v1_media_streams.probe_main_video", return_value=MainVideo(0, 1800, 1080, 25, 6, 1, 1, False)), patch("cv2.VideoCapture", return_value=cap):
            report = qc.run_quality_gate("valid-fixture", output, original_video_path=output,
                translated_subtitles=[cue(1, 1, 3)], dubbing_audio_files=[{"index": 1, "path": str(self.dub), "start": 1}],
                mixed_audio_path=self.bgm, ass_subtitle_path=ass, policy="REPORT_ONLY", workspace_path=SCRATCH)
        self.assertEqual(report.overall_status, "PASSED_WITH_WARNINGS")
        self.assertEqual(report.failure_count, 0)

    def test_isolated_worker_coalesces_parent_and_honors_voice_snapshot(self):
        cues = [cue(1, 0, .04, "xin"), cue(2, .04, 2, "chào")]
        voice_map = {str(i): {"source": "capcut", "param": "Mai", "gender": "female"} for i in (1, 2)}
        def run(cmd, **kwargs):
            request = list(srt.parse(Path(cmd[2]).read_text(encoding="utf-8")))
            self.assertEqual(len(request), 1)
            self.assertEqual(request[0].content, "xin chào")
            Path(cmd[3]).write_text(json.dumps([{"index": 1, "start": 0, "path": str(self.dub), "duration": 2, "content": "xin chào"}]), encoding="utf-8")
        with patch("batch_control.run", side_effect=run):
            result = asyncio.run(isolated.generate_dubbing_audio_isolated(cues, str(SCRATCH / "isolated"), segment_voices=voice_map))
        self.assertEqual(len(cues), len(result))
        self.assertEqual(cues[0].voice_param, "Mai")

    def test_worker_error_preserves_transient_classification(self):
        def run(cmd, **kwargs):
            Path(cmd[3]).write_text(json.dumps({"error": "network unavailable", "retryable": True}), encoding="utf-8")
            raise subprocess.CalledProcessError(1, cmd)
        with patch("batch_control.run", side_effect=run), self.assertRaises(ConnectionError) as caught:
            asyncio.run(isolated.generate_dubbing_audio_isolated([cue(1, 0, 1)], str(SCRATCH / "isolated-error")))
        self.assertTrue(retry.should_retry_job(caught.exception))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        TEMP.cleanup()
