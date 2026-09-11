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
        initial = sys.argv[1] if len(sys.argv) > 1 else str(Path.home())
        if not Path(initial).is_dir(): initial = str(Path.home())
        selected = filedialog.askdirectory(parent=window, title="Chọn thư mục chứa video cần xử lý", initialdir=initial, mustexist=True)
        print(json.dumps({"path": selected or ""}, ensure_ascii=True), flush=True)
    finally:
        window.destroy()
if __name__ == "__main__": main()
