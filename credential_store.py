"""用 Windows DPAPI 将登录信息绑定到当前 Windows 用户。"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FILE = ROOT / "credentials.dat"
UI_FORBIDDEN = 0x1


class CredentialStoreError(Exception):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise CredentialStoreError("登录信息加密仅支持 Windows。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = DataBlob()
    if not function(ctypes.byref(source), None, None, None, None, UI_FORBIDDEN, ctypes.byref(result)):
        raise CredentialStoreError(f"Windows 登录信息加密失败（错误码 {ctypes.get_last_error()}）。")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save_credentials(username: str, password: str) -> None:
    if not username or not password:
        raise CredentialStoreError("账号和密码不能为空。")
    payload = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
    encrypted = _dpapi(payload, decrypt=False)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=ROOT, prefix="credentials-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(encrypted)
        os.replace(temp_path, FILE)
    except OSError as exc:
        raise CredentialStoreError("无法把加密的登录信息保存到程序目录。") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_credentials() -> tuple[str, str] | None:
    if not FILE.exists():
        return None
    try:
        encrypted = FILE.read_bytes()
        data = json.loads(_dpapi(encrypted, decrypt=True).decode("utf-8"))
        username, password = data["username"], data["password"]
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise ValueError("invalid credentials")
        return username, password
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, CredentialStoreError) as exc:
        raise CredentialStoreError("无法读取本地登录信息；请重新输入并保存。") from exc
