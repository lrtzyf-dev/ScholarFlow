"""Windows user-bound credential protection, with no plaintext fallback."""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes


class CredentialProtectionError(ValueError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(value: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise CredentialProtectionError("当前系统没有可用的系统凭证保护，请在支持的Windows环境配置密钥")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        args = (ctypes.byref(source), "ScholarFlow model credential", None, None, None, 1,
                ctypes.byref(result))
    function.restype = wintypes.BOOL
    try:
        if not function(*args):
            raise CredentialProtectionError("无法读取或保存系统保护的模型密钥，请重新配置")
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if result.pbData:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def protect(value: str) -> str:
    return "dpapi:" + base64.b64encode(_transform(value.encode("utf-8"), decrypt=False)).decode("ascii")


def unprotect(value: str) -> str:
    try:
        if not value.startswith("dpapi:"):
            raise ValueError()
        encrypted = base64.b64decode(value[6:], validate=True)
        return _transform(encrypted, decrypt=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise CredentialProtectionError("模型密钥不可用，请重新配置") from exc
