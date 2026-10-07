import sys, io, asyncio, os, torch, edge_tts
import pytest

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, r'C:\tool v1\backend')

@pytest.mark.skipif(
    os.getenv("RUN_QWEN_REAL_TEST") != "1",
    reason="Thực nghiệm Qwen3-ASR phần cứng nặng; chỉ kích hoạt khi RUN_QWEN_REAL_TEST=1"
)
def test_qwen_real_inference_manual():
    from qwen_asr import Qwen3ASRModel
    from ai.v1_qwen_asr_adapter import DEFAULT_QWEN_ASR_DIR, DEFAULT_QWEN_ALIGNER_DIR

    tmp_mp3 = r'C:\tool v1\workspace\control\test_qwen_speech.mp3'
    tmp_wav = r'C:\tool v1\workspace\control\test_qwen_speech.wav'

    async def gen():
        comm = edge_tts.Communicate('Xin chào mọi người', 'vi-VN-HoaiMyNeural')
        await comm.save(tmp_mp3)

    asyncio.run(gen())
    os.system(f'ffmpeg -y -i "{tmp_mp3}" -ar 16000 -ac 1 "{tmp_wav}" >nul 2>&1')

    try:
        print('Testing Qwen3-ASR with real speech audio...')
        model = Qwen3ASRModel.from_pretrained(
            str(DEFAULT_QWEN_ASR_DIR),
            dtype=torch.float16,
            device_map='cuda:0',
            forced_aligner=str(DEFAULT_QWEN_ALIGNER_DIR),
            forced_aligner_kwargs={'dtype': torch.float16, 'device_map': 'cuda:0'}
        )
        results = model.transcribe(audio=tmp_wav, return_time_stamps=True)
        print('Recognition output:', results)
        for r in results:
            print('Text:', getattr(r, 'text', None))
            print('Time stamps:', getattr(r, 'time_stamps', None))
        del model
        torch.cuda.empty_cache()
    finally:
        for f in (tmp_mp3, tmp_wav):
            if os.path.exists(f):
                os.remove(f)

if __name__ == '__main__':
    test_qwen_real_inference_manual()

