#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mã hóa / giải mã file dữ liệu bằng AES-256-GCM + mật khẩu (PBKDF2-SHA256).

Cách dùng:
    python tools/sheet_crypto.py encrypt <file> [<file> ...]
    python tools/sheet_crypto.py decrypt <file.enc> [<file.enc> ...]
    python tools/sheet_crypto.py info <file.enc>

Mật khẩu được hỏi trực tiếp khi chạy (getpass), không lưu ra đĩa.
Khi cần chạy tự động: thêm --password-stdin và pipe mật khẩu vào stdin.

Định dạng file .enc:
    "SHEETENC" (8 byte) | version (1 byte) | header_len (4 byte, big-endian)
    | header JSON (header_len byte) | ciphertext (AES-256-GCM)
Header và ciphertext được xác thực cùng nhau (AAD) nên sửa 1 byte là giải mã thất bại.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

MAGIC = b"SHEETENC"
VERSION = 1
DEFAULT_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 8
MAX_HEADER_LEN = 64 * 1024

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:  # pragma: no cover
    sys.exit(
        "Thiếu thư viện 'cryptography'.\n"
        "Cài bằng: python -m pip install cryptography"
    )


def _setup_stdout() -> None:
    """Đảm bảo in tiếng Việt đúng trên console Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def _read_password(confirm: bool, use_stdin: bool) -> str:
    if use_stdin:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("Không đọc được mật khẩu từ stdin.")
        return line.rstrip("\r\n")

    pw = getpass.getpass("Mật khẩu: ")
    if confirm:
        pw2 = getpass.getpass("Nhập lại mật khẩu: ")
        if pw != pw2:
            raise SystemExit("Mật khẩu nhập lại không khớp.")
        if len(pw) < MIN_PASSWORD_LEN:
            raise SystemExit(f"Mật khẩu phải dài ít nhất {MIN_PASSWORD_LEN} ký tự.")
    if not pw:
        raise SystemExit("Mật khẩu không được để trống.")
    return pw


def _parse_enc(path: Path) -> tuple[dict, bytes, bytes]:
    """Trả về (header, aad, ciphertext)."""
    raw = path.read_bytes()
    prefix = len(MAGIC) + 1 + 4
    if len(raw) < prefix or not raw.startswith(MAGIC):
        raise ValueError("Không phải file .enc do công cụ này tạo (sai magic).")
    version = raw[len(MAGIC)]
    if version != VERSION:
        raise ValueError(f"Phiên bản file không hỗ trợ: {version}.")
    (header_len,) = struct.unpack(">I", raw[len(MAGIC) + 1:prefix])
    if header_len <= 0 or header_len > MAX_HEADER_LEN:
        raise ValueError("Header không hợp lệ.")
    end = prefix + header_len
    if len(raw) <= end:
        raise ValueError("File bị cắt ngắn hoặc hỏng.")
    try:
        header = json.loads(raw[prefix:end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Header JSON hỏng: {exc}") from exc
    if not isinstance(header, dict):
        raise ValueError("Header không hợp lệ.")
    return header, raw[:end], raw[end:]


def _decode_b64(header: dict, field: str) -> bytes:
    try:
        return base64.b64decode(header[field], validate=True)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Header thiếu/hỏng trường '{field}'.") from exc


def _safe_output_name(header: dict) -> str:
    name = str(header.get("name") or "").strip()
    name = Path(name.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise ValueError("Header không có tên file hợp lệ.")
    return name


def cmd_encrypt(args: argparse.Namespace) -> int:
    sources = [Path(p) for p in args.inputs]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        print("Không tìm thấy file: " + ", ".join(missing), file=sys.stderr)
        return 2

    password = _read_password(confirm=True, use_stdin=args.password_stdin)
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Mật khẩu phải dài ít nhất {MIN_PASSWORD_LEN} ký tự.", file=sys.stderr)
        return 2

    failed = 0
    for src in sources:
        dst = src.with_name(src.name + ".enc")
        if dst.exists() and not args.force:
            print(f"[BỎ QUA] {dst} đã tồn tại (dùng --force để ghi đè).")
            failed += 1
            continue
        try:
            data = src.read_bytes()
            salt = os.urandom(16)
            nonce = os.urandom(12)
            key = _derive_key(password, salt, args.iterations)
            header = {
                "kdf": "pbkdf2-hmac-sha256",
                "iterations": args.iterations,
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "name": src.name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            header_bytes = json.dumps(
                header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            aad = MAGIC + bytes([VERSION]) + struct.pack(">I", len(header_bytes)) + header_bytes
            ciphertext = AESGCM(key).encrypt(nonce, data, aad)

            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_bytes(aad + ciphertext)
            os.replace(tmp, dst)
            print(f"[OK] {src.name}  ->  {dst.name}  ({_human_size(len(aad) + len(ciphertext))})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[LỖI] {src}: {exc}", file=sys.stderr)

    return 1 if failed else 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    sources = [Path(p) for p in args.inputs]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        print("Không tìm thấy file: " + ", ".join(missing), file=sys.stderr)
        return 2

    password = _read_password(confirm=False, use_stdin=args.password_stdin)
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    failed = 0
    for src in sources:
        try:
            header, aad, ciphertext = _parse_enc(src)
            iterations = int(header.get("iterations") or 0)
            if not (10_000 <= iterations <= 10_000_000):
                raise ValueError("Số vòng PBKDF2 không hợp lệ.")
            salt = _decode_b64(header, "salt")
            nonce = _decode_b64(header, "nonce")
            if len(salt) < 16 or len(nonce) != 12:
                raise ValueError("Salt/nonce không hợp lệ.")

            key = _derive_key(password, salt, iterations)
            try:
                data = AESGCM(key).decrypt(nonce, ciphertext, aad)
            except InvalidTag:
                raise ValueError("Sai mật khẩu hoặc file đã bị sửa.") from None

            expected_size = int(header.get("size") or -1)
            expected_hash = str(header.get("sha256") or "")
            if len(data) != expected_size:
                raise ValueError("Kích thước sau giải mã không khớp header.")
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_hash):
                raise ValueError("SHA-256 sau giải mã không khớp header.")

            name = _safe_output_name(header)
            dst = (out_dir / name) if out_dir else src.with_name(name)
            if dst.exists() and not args.force:
                raise FileExistsError(f"{dst} đã tồn tại (dùng --force để ghi đè).")
            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            print(f"[OK] {src.name}  ->  {dst}  ({_human_size(len(data))})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[LỖI] {src}: {exc}", file=sys.stderr)

    return 1 if failed else 0


def cmd_info(args: argparse.Namespace) -> int:
    failed = 0
    for item in args.inputs:
        src = Path(item)
        try:
            header, _, ciphertext = _parse_enc(src)
            print(f"File:        {src}")
            print(f"Tên gốc:     {header.get('name')}")
            print(f"Kích thước:  {_human_size(int(header.get('size') or 0))}")
            print(f"Tạo lúc:     {header.get('created', '?')}")
            print(f"PBKDF2:      {header.get('iterations')} vòng")
            print(f"Ciphertext:  {_human_size(len(ciphertext))}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[LỖI] {src}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Mã hóa/giải mã file bằng AES-256-GCM + mật khẩu.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    enc = sub.add_parser("encrypt", help="Mã hóa file, tạo <file>.enc")
    enc.add_argument("inputs", nargs="+", help="File cần mã hóa")
    enc.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                     help=f"Số vòng PBKDF2 (mặc định {DEFAULT_ITERATIONS})")
    enc.add_argument("--force", action="store_true", help="Ghi đè file .enc đã có")
    enc.add_argument("--password-stdin", action="store_true",
                     help="Đọc mật khẩu từ stdin thay vì hỏi trực tiếp")
    enc.set_defaults(func=cmd_encrypt)

    dec = sub.add_parser("decrypt", help="Giải mã file .enc")
    dec.add_argument("inputs", nargs="+", help="File .enc cần giải mã")
    dec.add_argument("--out-dir", help="Thư mục chứa file sau khi giải mã")
    dec.add_argument("--force", action="store_true", help="Ghi đè file đã có")
    dec.add_argument("--password-stdin", action="store_true",
                     help="Đọc mật khẩu từ stdin thay vì hỏi trực tiếp")
    dec.set_defaults(func=cmd_decrypt)

    info = sub.add_parser("info", help="Xem thông tin file .enc (không cần mật khẩu)")
    info.add_argument("inputs", nargs="+", help="File .enc")
    info.set_defaults(func=cmd_info)

    return p


def main() -> int:
    _setup_stdout()
    args = build_parser().parse_args()
    if getattr(args, "iterations", DEFAULT_ITERATIONS) < 10_000:
        print("--iterations phải >= 10000.", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
