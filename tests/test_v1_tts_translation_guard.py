import tempfile
import importlib
import inspect
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
from backend.ai import voice_cloning as vc

tts_impl = inspect.unwrap(vc.generate_dubbing_audio)
translation = importlib.import_module(tts_impl.__module__.rsplit(".", 1)[0] + ".translation")
cache = importlib.import_module(tts_impl.__module__.rsplit(".", 1)[0] + ".v1_voice_cache")

class TranslationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_only_chinese_before_tts(self):
        items=[NS(index=i,content=t,start=timedelta(seconds=(i-1)*3),end=timedelta(seconds=(i-1)*3+2))
               for i,t in [(1,"Xin chào"),(2,"倾听悦耳的声音")]]
        def repair(segments,**kwargs):
            self.assertEqual([s.index for s in segments],[2])
            segments[0].content="Lắng nghe âm thanh dễ chịu"
        tts = AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as folder, patch.object(
                translation,"translate_subtitles",side_effect=repair) as translate, patch.object(
                cache,"read_voice_cache", return_value=None), patch.dict(
                tts_impl.__globals__, generate_single_tts=tts), patch.object(
                vc.asyncio,"sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,"TTS incomplete"):
                await vc.generate_dubbing_audio(items,folder)
            self.assertEqual(translate.call_count,1)
            self.assertEqual(tts.await_count,8)
            self.assertFalse(any("倾" in c.args[0].content for c in tts.call_args_list))

    async def test_failed_translation_never_reaches_tts(self):
        item=NS(index=2,content="倾听悦耳的声音",start=timedelta(),end=timedelta(seconds=2))
        tts = AsyncMock()
        with tempfile.TemporaryDirectory() as folder, patch.object(
                translation,"translate_subtitles",side_effect=RuntimeError("Translation failed")), patch.dict(
                tts_impl.__globals__, generate_single_tts=tts):
            with self.assertRaisesRegex(RuntimeError,"Translation failed"):
                await vc.generate_dubbing_audio([item],folder)
            tts.assert_not_awaited()
