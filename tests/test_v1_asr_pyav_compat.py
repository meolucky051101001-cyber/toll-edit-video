"""Exercise the real production adapter against strict fake PyAV APIs, including clean CI."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.ai import pyav_compat


def test_av_open_strips_metadata_errors_transparently():
    # This signature deliberately rejects the removed PyAV keyword.
    container = object()
    calls = []
    def opener(file, mode="r", *, format=None):
        calls.append((file, mode, format))
        return container
    av = SimpleNamespace(open=opener)
    assert pyav_compat.install_pyav_compat(av)
    assert av.open("sample.wav", "r", metadata_errors="ignore", format="wav") is container
    assert calls == [("sample.wav", "r", "wav")]


def test_compat_av_open_handles_extra_metadata_errors_kwarg():
    original = Mock(return_value="container")
    av = SimpleNamespace(open=original)
    assert pyav_compat.install_pyav_compat(av)
    assert av.open("sample.wav", metadata_errors="ignore", options={"threads": "1"}) == "container"
    original.assert_called_once_with("sample.wav", options={"threads": "1"})


def test_adapter_is_idempotent_across_module_reload_and_preserves_errors():
    original = Mock(side_effect=ValueError("decode failure"))
    av = SimpleNamespace(open=original)
    assert pyav_compat.install_pyav_compat(av)
    wrapper = av.open
    importlib.reload(pyav_compat)
    assert pyav_compat.install_pyav_compat(av)
    assert av.open is wrapper
    with pytest.raises(ValueError, match="decode failure"):
        av.open("bad.wav", metadata_errors="ignore")
    original.assert_called_once_with("bad.wav")


def test_normal_install_path_uses_injected_av_module_without_dependency():
    av = SimpleNamespace(open=Mock(return_value="container"))
    with patch.dict("sys.modules", {"av": av}):
        assert pyav_compat.install_pyav_compat()
        assert av.open("sample.wav", metadata_errors="ignore") == "container"
    av.open.__wrapped__.assert_called_once_with("sample.wav")
