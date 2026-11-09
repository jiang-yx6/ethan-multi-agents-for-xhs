"""本地凭据保护：Windows 使用当前用户 DPAPI，其他系统使用私有 Fernet 主密钥。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from pydantic import SecretStr


_key_lock = threading.Lock()


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(wintypes.BYTE))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)))
    outgoing = Blob()
    # 绑定当前 Windows 用户，不使用 CRYPTPROTECT_LOCAL_MACHINE，不弹出系统 UI。
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError("无法读取或保护本机模型密钥，请在当前用户下重新填写密钥")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


class CredentialCipher:
    def __init__(self, root: Path):
        self.key_path = root / ".credentials.key"

    def _fernet(self, *, create: bool):
        from cryptography.fernet import Fernet

        with _key_lock:
            if create:
                try:
                    descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(Fernet.generate_key())
                        handle.flush()
                        os.fsync(handle.fileno())
            try:
                return Fernet(self.key_path.read_bytes())
            except (OSError, ValueError):
                raise ValueError("本机凭据主密钥不可用，请恢复原 .credentials.key 文件") from None

    def encrypt(self, scope: str, secret: SecretStr) -> bytes:
        payload = json.dumps({"scope": scope, "key": secret.get_secret_value()}).encode()
        if os.name == "nt":
            return b"dpapi:" + _dpapi(payload)
        return b"fernet:" + self._fernet(create=True).encrypt(payload)

    def decrypt(self, scope: str, token: bytes) -> SecretStr:
        try:
            if token.startswith(b"dpapi:") and os.name == "nt":
                payload = _dpapi(token[6:], decrypt=True)
            elif token.startswith(b"fernet:") and os.name != "nt":
                payload = self._fernet(create=False).decrypt(token[7:])
            else:
                raise ValueError("凭据保护方式与当前系统不匹配")
            value = json.loads(payload)
            if value["scope"] != scope or not isinstance(value["key"], str):
                raise ValueError("凭据与接口不匹配")
            return SecretStr(value["key"])
        except Exception:
            raise ValueError("已保存的模型密钥无法解密，请在本机重新填写并保存") from None
