from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any


VAULT_VERSION = 1
DEFAULT_VAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "credential-vault.json"
_ENTROPY = b"RelayDeck Local supplier credential vault v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("The RelayDeck credential vault currently requires Windows DPAPI.")


def protect_data(value: bytes) -> bytes:
    _require_windows()
    input_blob, input_buffer = _blob_from_bytes(value)
    entropy_blob, entropy_buffer = _blob_from_bytes(_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "RelayDeck Local credentials",
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect_data(value: bytes) -> bytes:
    _require_windows()
    input_blob, input_buffer = _blob_from_bytes(value)
    entropy_blob, entropy_buffer = _blob_from_bytes(_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_supplier_key(value: str) -> str:
    key = str(value or "").strip().lower()
    if not key:
        raise ValueError("supplier_key is required")
    return key


def _normalize_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in (credentials or {}).items()
        if str(key).strip() and value not in (None, "")
    }


class CredentialVault:
    def __init__(self, path: str | Path = DEFAULT_VAULT_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VAULT_VERSION, "suppliers": {}}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if int(document.get("version") or 0) != VAULT_VERSION:
            raise ValueError("Unsupported credential vault version")
        if not isinstance(document.get("suppliers"), dict):
            raise ValueError("Invalid credential vault structure")
        return document

    def _write_document(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(document, ensure_ascii=True, indent=2), encoding="utf-8")
        os.replace(temporary_path, self.path)

    def _encrypt_credentials(self, credentials: dict[str, str]) -> str:
        payload = json.dumps(credentials, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(protect_data(payload)).decode("ascii")

    def _decrypt_entry(self, entry: dict[str, Any] | None) -> dict[str, str]:
        if not entry:
            return {}
        protected_data = str(entry.get("protected_data") or "")
        if not protected_data:
            return {}
        payload = unprotect_data(base64.b64decode(protected_data.encode("ascii")))
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Invalid credential payload")
        return _normalize_credentials(decoded)

    def get(self, supplier_key: str) -> dict[str, str]:
        key = _normalize_supplier_key(supplier_key)
        with self._lock:
            document = self._read_document()
            return self._decrypt_entry(document["suppliers"].get(key))

    def replace(self, supplier_key: str, credentials: dict[str, Any]) -> dict[str, Any]:
        key = _normalize_supplier_key(supplier_key)
        normalized = _normalize_credentials(credentials)
        with self._lock:
            document = self._read_document()
            if normalized:
                document["suppliers"][key] = {
                    "protected_data": self._encrypt_credentials(normalized),
                    "configured_fields": sorted(normalized),
                    "updated_at": _now_iso(),
                }
            else:
                document["suppliers"].pop(key, None)
            self._write_document(document)
            return self.status(key)

    def merge(self, supplier_key: str, updates: dict[str, Any]) -> dict[str, Any]:
        key = _normalize_supplier_key(supplier_key)
        with self._lock:
            current = self.get(key)
            current.update(_normalize_credentials(updates))
            return self.replace(key, current)

    def remove_fields(self, supplier_key: str, fields: list[str]) -> dict[str, Any]:
        key = _normalize_supplier_key(supplier_key)
        with self._lock:
            current = self.get(key)
            for field in fields:
                current.pop(str(field), None)
            return self.replace(key, current)

    def delete(self, supplier_key: str) -> bool:
        key = _normalize_supplier_key(supplier_key)
        with self._lock:
            document = self._read_document()
            existed = key in document["suppliers"]
            if existed:
                document["suppliers"].pop(key, None)
                self._write_document(document)
            return existed

    def status(self, supplier_key: str) -> dict[str, Any]:
        key = _normalize_supplier_key(supplier_key)
        with self._lock:
            document = self._read_document()
            entry = document["suppliers"].get(key)
            credentials = self._decrypt_entry(entry)
            return {
                "supplier_key": key,
                "configured": bool(credentials),
                "configured_fields": sorted(credentials),
                "updated_at": str((entry or {}).get("updated_at") or ""),
                "storage": "windows-dpapi-current-user",
            }


_default_vault: CredentialVault | None = None


def get_credential_vault() -> CredentialVault:
    global _default_vault
    if _default_vault is None:
        _default_vault = CredentialVault()
    return _default_vault


__all__ = [
    "CredentialVault",
    "DEFAULT_VAULT_PATH",
    "get_credential_vault",
    "protect_data",
    "unprotect_data",
]
