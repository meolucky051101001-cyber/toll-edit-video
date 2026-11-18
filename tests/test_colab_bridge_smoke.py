"""
Kiểm thử khói (smoke test) cho colab_bridge mà KHÔNG tốn Compute Units.
"""
import sys
from pathlib import Path

# Thêm backend vào sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

from cloud.cloud_config import to_wsl_path, to_win_path
from cloud.colab_bridge import get_usage, get_sessions


def test_path_conversions():
    print("[*] Kiểm tra chuyển đổi đường dẫn Windows <-> WSL...")
    win_c = r"C:\Projects\Tools\video.mp4"
    wsl_c = to_wsl_path(win_c)
    assert wsl_c == "/mnt/c/Projects/Tools/video.mp4", f"Sai wsl_c: {wsl_c}"

    win_d = r"D:\video phôi\input.mp4"
    wsl_d = to_wsl_path(win_d)
    assert wsl_d == "/mnt/d/video phôi/input.mp4", f"Sai wsl_d: {wsl_d}"

    back_c = to_win_path(wsl_c)
    assert back_c == r"C:\Projects\Tools\video.mp4", f"Sai back_c: {back_c}"
    print("  -> Chuyển đổi đường dẫn OK!")


def check_colab_usage_and_sessions():
    print("[*] Kiểm tra tra cứu Colab usage & sessions (0 CU)...")
    usage = get_usage()
    assert usage["ok"] is True, f"get_usage thất bại: {usage}"
    assert usage["balance"] > 0, f"Balance không hợp lệ: {usage['balance']}"

    sessions = get_sessions()
    assert isinstance(sessions, list), "Sessions phải là danh sách"
    print("  -> Colab usage & sessions OK!")


if __name__ == "__main__":
    test_path_conversions()
    check_colab_usage_and_sessions()
    print("\n[SUCCESS] Tất cả bài test khói colab_bridge đều đạt (0 CU)!")
