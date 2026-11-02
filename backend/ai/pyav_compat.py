"""Idempotent PyAV compatibility without importing ASR or model packages."""
from functools import wraps


def install_pyav_compat(av_module=None) -> bool:
    if av_module is None:
        try:
            import av as av_module
        except ImportError:
            return False
    opener = getattr(av_module, "open", None)
    if not callable(opener):
        return False
    if getattr(opener, "_v1_metadata_compat", False) is True:
        return True

    @wraps(opener)
    def compatible_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return opener(*args, **kwargs)

    compatible_open._v1_metadata_compat = True
    av_module.open = compatible_open
    return True
