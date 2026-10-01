import os
import pytest
from unittest.mock import patch
from pathlib import Path
from backend.ai.v1_qwen_asr_adapter import (
    is_qwen_asr_enabled,
    check_qwen_readiness,
    run_qwen_asr_benchmark,
    DEFAULT_QWEN_ASR_DIR,
)

def test_qwen_asr_flag_disabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        assert not is_qwen_asr_enabled()
        ready, reason = check_qwen_readiness()
        assert not ready
        assert "tắt" in reason

def test_qwen_asr_flag_enabled_readiness():
    with patch.dict(os.environ, {"ENABLE_QWEN_ASR": "true"}):
        assert is_qwen_asr_enabled()
        ready, reason = check_qwen_readiness()
        assert ready is True
        assert "Sẵn sàng" in reason

def test_run_qwen_asr_benchmark_disabled_returns_none(tmp_path):
    dummy_wav = tmp_path / "test.wav"
    dummy_wav.write_bytes(b"RIFF" + b"\x00" * 40)
    with patch.dict(os.environ, {"ENABLE_QWEN_ASR": "false"}):
        res = run_qwen_asr_benchmark(dummy_wav)
        assert res is None

def test_run_qwen_asr_benchmark_missing_file():
    with patch.dict(os.environ, {"ENABLE_QWEN_ASR": "true"}):
        with pytest.raises(FileNotFoundError):
            run_qwen_asr_benchmark("non_existent_audio_path_xyz.wav")
