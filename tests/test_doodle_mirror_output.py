"""Test nhẹ cho mirror_output_video: copy gương video thành phẩm sang thư mục lưu trữ (vd D:\\kich_ban)."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_mirror_copy_thanh_cong(tmp_path):
    from backend.ai import doodle_engine

    src = tmp_path / "video.mp4"
    src.write_bytes(b"video-bytes")
    dest_dir = tmp_path / "mirror"
    copied = doodle_engine.mirror_output_video(src, copy_dir=str(dest_dir))
    assert copied is not None
    assert copied.is_file()
    assert copied.read_bytes() == b"video-bytes"
    # Copy lần 2 phải ghi đè an toàn (idempotent)
    copied2 = doodle_engine.mirror_output_video(src, copy_dir=str(dest_dir))
    assert copied2 == copied


def test_mirror_tat_khi_rong(tmp_path, monkeypatch):
    from backend.ai import doodle_engine

    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    monkeypatch.delenv("SCRIPT_OUTPUT_COPY_DIR", raising=False)
    assert doodle_engine.mirror_output_video(src, copy_dir="") is None
    assert doodle_engine.mirror_output_video(src, copy_dir="   ") is None
    # Không truyền tham số + env trống -> tắt
    assert doodle_engine.mirror_output_video(src) is None


def test_mirror_doc_bien_moi_truong(tmp_path, monkeypatch):
    from backend.ai import doodle_engine

    src = tmp_path / "video.mp4"
    src.write_bytes(b"y")
    target = tmp_path / "env-mirror"
    monkeypatch.setenv("SCRIPT_OUTPUT_COPY_DIR", str(target))
    copied = doodle_engine.mirror_output_video(src)
    assert copied is not None and copied.is_file()


def test_mirror_loi_thi_im_lang_khong_throw(tmp_path):
    from backend.ai import doodle_engine

    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    # Cha cua "thu muc dich" la mot FILE -> mkdir se loi -> phai tra None, khong raise
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a dir")
    bad = blocker / "sub"
    assert doodle_engine.mirror_output_video(src, copy_dir=str(bad)) is None
