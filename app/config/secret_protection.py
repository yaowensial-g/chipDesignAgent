import base64
import ctypes
import os
from ctypes import wintypes


class SecretProtectionError(RuntimeError):
    """Raised when a secret cannot be protected or recovered."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def protect_secret(secret: str) -> str:
    if os.name != "nt":
        raise SecretProtectionError(
            "网页保存 API Key 目前仅支持 Windows DPAPI；请改用 OPENAI_API_KEY 环境变量"
        )
    raw = secret.encode("utf-8")
    input_buffer = ctypes.create_string_buffer(raw)
    input_blob = _DataBlob(
        len(raw),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "ChipFlow Agent API Key",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise SecretProtectionError("Windows DPAPI 无法加密 API Key")
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(protected: str) -> str:
    if os.name != "nt":
        raise SecretProtectionError("当前系统无法解密 Windows DPAPI 密钥")
    try:
        encrypted = base64.b64decode(protected, validate=True)
    except ValueError as exc:
        raise SecretProtectionError("保存的 API Key 密文格式无效") from exc
    input_buffer = ctypes.create_string_buffer(encrypted)
    input_blob = _DataBlob(
        len(encrypted),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    ):
        raise SecretProtectionError(
            "无法解密 API Key；它可能由其他 Windows 用户保存"
        )
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)

