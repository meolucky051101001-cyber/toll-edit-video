import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch, AsyncMock
from backend.ai import translation as tr, voice_cloning as vc
from backend import ocr_utils as ocr

class StabilityTests(unittest.TestCase):
    def test_gemini_skips_recent_failed_model_and_keeps_full_array(self):
        tr._gemini_cooldown.clear()
        tr._gemini_last_good.clear()
        good=Mock(status_code=200)
        good.json.return_value={"candidates":[{"content":{"parts":[{"text":'["Xin chào"]'}]}}]}
        bad=Mock(status_code=503)
        with patch.object(tr,"extract_video_frames_base64",return_value=[]), patch.object(
                tr.requests,"post",side_effect=[bad,good,good]) as post:
            self.assertEqual(tr.translate_with_gemini(["你好"],api_key="test-only"),["Xin chào"])
            first_success=post.call_args_list[1].args[0]
            self.assertEqual(tr.translate_with_gemini(["你好"],api_key="test-only"),["Xin chào"])
            self.assertEqual(post.call_args_list[2].args[0],first_success)
            self.assertIsInstance(post.call_args.kwargs["timeout"],tuple)

    def test_ocr_closes_failed_worker_before_fallback_and_does_not_retry(self):
        ocr._paddle_failed=False
        events=[]
        reader=Mock()
        reader.readtext.side_effect=lambda *a,**kw: events.append("fallback") or []
        with patch.object(ocr,"runtime_module_available",return_value=True), patch.object(
                ocr.cv2,"imwrite",return_value=True), patch.object(
                ocr,"run_v1_ocr",side_effect=RuntimeError("memory")) as run, patch.object(
                ocr,"get_ocr_reader",return_value=reader), patch(
                "backend.ai.v1_model_runtime.close_session",side_effect=lambda:events.append("close")):
            ocr._recognize_batch([object()])
            ocr._recognize_batch([object()])
            self.assertEqual(run.call_count,1)
            self.assertEqual(events,["close","fallback","fallback"])
        ocr._paddle_failed=False

    def test_exact_frame_cache_and_changed_pixels(self):
        import numpy as np
        ocr._frame_cache.clear()
        a=np.zeros((2,2,3),dtype=np.uint8)
        b=a.copy(); b[0,0,0]=1
        with patch.object(ocr,"_recognize_batch",side_effect=lambda frames:[["ok"] for f in frames]) as run:
            self.assertEqual(ocr._readtext_batch([a,a,b]),[["ok"]]*3)
            self.assertEqual(len(run.call_args.args[0]),2)
            ocr._readtext_batch([a,b])
            self.assertEqual(run.call_count,1)
        ocr._frame_cache.clear()

class VoiceStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cue_reused_but_changed_text_invalidates(self):
        item=NS(index=1,content="Xin chào",start=timedelta(0),end=timedelta(seconds=2))
        async def generate(seg,folder,*args):
            p=Path(folder)/"1.mp3"; p.write_bytes(b"a"*256)
            return dict(index=1,path=str(p),actual_audio_duration=1.,content=seg.content,start=0,end=2)
        with tempfile.TemporaryDirectory() as d, patch.object(vc,"generate_single_tts",side_effect=generate) as fn:
            await vc.generate_dubbing_audio([item],d)
            await vc.generate_dubbing_audio([item],d)
            self.assertEqual(fn.call_count,1)
            item.content="Chào bạn"
            await vc.generate_dubbing_audio([item],d)
            self.assertEqual(fn.call_count,2)

    async def test_missing_voice_fails_instead_of_silently_omitting(self):
        item=NS(index=1,content="Xin chào",start=timedelta(0),end=timedelta(seconds=2))
        with tempfile.TemporaryDirectory() as d, patch.object(vc,"generate_single_tts",new=AsyncMock(return_value=None)):
            with self.assertRaisesRegex(RuntimeError,"TTS incomplete"):
                await vc.generate_dubbing_audio([item],d)
