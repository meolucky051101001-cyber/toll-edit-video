import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
from backend.ai import voice_cloning as vc

class TranslationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_only_chinese_before_tts(self):
        items=[NS(index=i,content=t,start=timedelta(),end=timedelta(seconds=2))
               for i,t in [(1,"Xin chào"),(2,"倾听悦耳的声音")]]
        def repair(segments,**kwargs):
            self.assertEqual([s.index for s in segments],[2])
            segments[0].content="Lắng nghe âm thanh dễ chịu"
        with tempfile.TemporaryDirectory() as folder, patch(
                "backend.ai.translation.translate_subtitles",side_effect=repair) as translate, patch.object(
                vc,"generate_single_tts",new=AsyncMock(return_value=None)) as tts:
            with self.assertRaisesRegex(RuntimeError,"TTS incomplete"):
                await vc.generate_dubbing_audio(items,folder)
            self.assertEqual(translate.call_count,1)
            self.assertEqual(tts.await_count,2)
            self.assertFalse(any("倾" in c.args[0].content for c in tts.call_args_list))

    async def test_failed_translation_never_reaches_tts(self):
        item=NS(index=2,content="倾听悦耳的声音",start=timedelta(),end=timedelta(seconds=2))
        with tempfile.TemporaryDirectory() as folder, patch(
                "backend.ai.translation.translate_subtitles",side_effect=RuntimeError("Translation failed")), patch.object(
                vc,"generate_single_tts",new=AsyncMock()) as tts:
            with self.assertRaisesRegex(RuntimeError,"Translation failed"):
                await vc.generate_dubbing_audio([item],folder)
            tts.assert_not_awaited()
