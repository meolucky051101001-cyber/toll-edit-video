"""Generate docs/ENV_VARS.md: every environment variable the backend reads.

Usage:
    python scripts/gen_env_docs.py          # rewrite docs/ENV_VARS.md
    python scripts/gen_env_docs.py --check  # exit 1 when the file is stale
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "ENV_VARS.md"
SOURCE_DIRS = ("backend",)
SOURCE_FILES = ("run_cloud_node.py", "telebot_video_downloader.py")
SKIP_PARTS = {"venv", "vendor_runtime", "__pycache__", "node_modules"}
HELPERS = {"env_flag", "env_int", "env_float", "env_str"}
DYNAMIC = "(tính lúc chạy)"
NEWLINE = chr(10)


def _is_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _literal(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return "" if node.value is None else str(node.value)
    return DYNAMIC


def _reads(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = node.args[0]
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                continue
            default = node.args[1] if len(node.args) > 1 else None
            if isinstance(func, ast.Attribute):
                is_getenv = (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                is_environ_get = func.attr == "get" and _is_environ(func.value)
                if is_getenv or is_environ_get or func.attr in HELPERS:
                    yield name.value, _literal(default)
            elif isinstance(func, ast.Name) and func.id in HELPERS:
                yield name.value, _literal(default)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            yield node.slice.value, ""


def _python_files():
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if not SKIP_PARTS.intersection(path.relative_to(ROOT).parts):
                yield path
    for name in SOURCE_FILES:
        path = ROOT / name
        if path.is_file():
            yield path


def collect() -> dict[str, dict[str, set[str]]]:
    found: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"defaults": set(), "files": set()}
    )
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for name, default in _reads(tree):
            found[name]["files"].add(relative)
            if default:
                found[name]["defaults"].add(default)
    return found


def _cell(text: str) -> str:
    return text.replace("|", chr(92) + "|").replace(NEWLINE, " ")


def render() -> str:
    found = collect()
    lines = [
        "# Biến môi trường mà backend đọc",
        "",
        "File này được sinh tự động bởi `python scripts/gen_env_docs.py`. Không sửa tay;",
        "`tests/test_env_var_docs.py` báo lỗi khi code đọc biến mới mà file chưa được sinh lại.",
        "",
        "Cột *Mặc định* là giá trị viết trong code. Nhiều giá trị nghĩa là các module dùng",
        "mặc định khác nhau cho cùng một biến; nên xem lại những dòng đó.",
        "",
        f"Tổng: {len(found)} biến.",
        "",
        "| Biến | Mặc định | Nơi đọc |",
        "|---|---|---|",
    ]
    for name in sorted(found):
        entry = found[name]
        defaults = sorted(entry["defaults"])
        default_text = " / ".join(f"`{_cell(value)}`" for value in defaults) or "—"
        files = sorted(entry["files"])
        file_text = ", ".join(f"`{item}`" for item in files[:4])
        if len(files) > 4:
            file_text += f" (+{len(files) - 4})"
        lines.append(f"| `{name}` | {default_text} | {file_text} |")
    return NEWLINE.join(lines) + NEWLINE


def main(argv: list[str]) -> int:
    content = render()
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if current.replace(chr(13) + NEWLINE, NEWLINE) != content:
            print("docs/ENV_VARS.md is stale; run: python scripts/gen_env_docs.py")
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8", newline=NEWLINE)
    print(f"Wrote {OUTPUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
