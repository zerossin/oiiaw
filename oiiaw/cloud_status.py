"""
Ground-truth check for whether a Windows cloud-sync placeholder (iCloud,
OneDrive, etc.) actually has its bytes on disk right now, via the Cloud
Filter API (CldApi.dll / CfGetPlaceholderInfo) — not by inferring from
GetFileAttributesW bits.

Struct layout and enum values below are taken verbatim from Microsoft Learn
(cfapi.h): CF_PLACEHOLDER_STANDARD_INFO, CF_PLACEHOLDER_INFO_CLASS,
CF_PIN_STATE. Verified against real files in the user's iCloud vault, not
just written from memory — see tests/test_cloud_status.py.
"""

import ctypes
import os
import platform
from dataclasses import dataclass
from enum import Enum

FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

CF_PLACEHOLDER_INFO_STANDARD = 1


class PinState(Enum):
    UNSPECIFIED = 0
    PINNED = 1
    UNPINNED = 2
    EXCLUDED = 3
    INHERIT = 4


class _CF_PLACEHOLDER_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("OnDiskDataSize", ctypes.c_int64),
        ("ValidatedDataSize", ctypes.c_int64),
        ("ModifiedDataSize", ctypes.c_int64),
        ("PropertiesSize", ctypes.c_int64),
        ("PinState", ctypes.c_int32),
        ("InSyncState", ctypes.c_int32),
        ("FileId", ctypes.c_int64),
        ("SyncRootFileId", ctypes.c_int64),
        ("FileIdentityLength", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class PlaceholderInfo:
    on_disk_bytes: int
    validated_bytes: int
    pin_state: PinState
    in_sync: bool


class CloudFilterUnavailable(Exception):
    """Raised when the Cloud Filter API isn't usable on this system at all."""


def _load_cldapi():
    if platform.system() != "Windows":
        raise CloudFilterUnavailable("Cloud Filter API is Windows-only")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    cldapi = ctypes.WinDLL("cldapi", use_last_error=True)
    cldapi.CfGetPlaceholderInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cldapi.CfGetPlaceholderInfo.restype = ctypes.c_long
    return k32, cldapi


class CloudFilter:
    def __init__(self):
        self._available = True
        try:
            self._k32, self._cldapi = _load_cldapi()
        except (CloudFilterUnavailable, AttributeError, OSError):
            self._available = False

    def get_placeholder_info(self, path: str) -> PlaceholderInfo | None:
        """
        Returns None if `path` isn't tracked as a cloud-filter placeholder at
        all (plain local file, or the API call otherwise failed) — callers
        should treat that as "content is available".
        """
        if not self._available:
            return None

        handle = self._k32.CreateFileW(
            str(path),
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ_WRITE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            return None

        try:
            # CF_PLACEHOLDER_STANDARD_INFO ends with a variable-length
            # FileIdentity blob — sizeof() on the fixed struct alone is too
            # small and the call fails with ERROR_MORE_DATA. Over-allocate
            # and reinterpret just the fixed prefix we care about.
            buf = ctypes.create_string_buffer(4096)
            returned = ctypes.c_uint32(0)
            hr = self._cldapi.CfGetPlaceholderInfo(
                handle,
                CF_PLACEHOLDER_INFO_STANDARD,
                buf,
                ctypes.sizeof(buf),
                ctypes.byref(returned),
            )
            if hr != 0:
                return None  # not a placeholder, or the call failed — caller falls back to "available"
            info = ctypes.cast(buf, ctypes.POINTER(_CF_PLACEHOLDER_STANDARD_INFO)).contents
            return PlaceholderInfo(
                on_disk_bytes=info.OnDiskDataSize,
                validated_bytes=info.ValidatedDataSize,
                pin_state=PinState(info.PinState),
                in_sync=bool(info.InSyncState),
            )
        finally:
            self._k32.CloseHandle(handle)

    def is_content_available(self, path: str) -> bool:
        """True if every byte of the file is already on disk — safe to read
        or copy right now without triggering a network fetch."""
        info = self.get_placeholder_info(path)
        if info is None:
            return True
        try:
            full_size = os.path.getsize(path)
        except OSError:
            return False
        return info.on_disk_bytes >= full_size
