"""Desktop folder picker launched separately from the API event loop."""
import json
import sys
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

def main():
    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else str(Path.home())
        title = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else "Chọn thư mục"
        must_exist = sys.argv[3].lower() != "false" if len(sys.argv) > 3 else True
        if not Path(initial).is_dir():
            # Thử thư mục cha nếu initial là một file hoặc thư mục chưa tạo
            parent = Path(initial).parent
            initial = str(parent) if parent.is_dir() else str(Path.home())
        selected = filedialog.askdirectory(parent=window, title=title, initialdir=initial, mustexist=must_exist)
        if selected:
            selected = str(Path(selected))
        print(json.dumps({"path": selected or ""}, ensure_ascii=True), flush=True)
    finally:
        window.destroy()

if __name__ == "__main__":
    main()
