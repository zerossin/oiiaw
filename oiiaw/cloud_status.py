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
import asyncio
import multiprocessing
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
CF_HYDRATE_FLAG_NONE = 0
CF_EOF = -1


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


class CloudProbeTimeout(TimeoutError):
    """The isolated Windows cloud-status probe stopped responding."""


class CloudProbeError(RuntimeError):
    """The isolated probe exited or returned an unexpected failure."""


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
    cldapi.CfHydratePlaceholder.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int64,
        ctypes.c_int64,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    cldapi.CfHydratePlaceholder.restype = ctypes.c_long
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

    def is_content_in_sync(self, path: str) -> bool:
        """True only after the cloud provider has validated the complete file.

        A hydrated placeholder is merely readable; it can still be in the
        middle of an upload/download and temporarily expose stale or empty
        content.  Destructive sync decisions must wait for both conditions.
        Plain files outside a Cloud Files provider have no placeholder info
        and are treated as immediately confirmed.
        """
        info = self.get_placeholder_info(path)
        if info is None:
            return True
        try:
            full_size = os.path.getsize(path)
        except OSError:
            return False
        return (
            info.in_sync
            and info.on_disk_bytes >= full_size
            and info.validated_bytes >= full_size
        )

    def hydrate(self, path: str) -> bool:
        """Ask the sync provider to make the complete file available locally."""
        if not self._available:
            return False

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
            return False

        try:
            result = self._cldapi.CfHydratePlaceholder(
                handle,
                0,
                CF_EOF,
                CF_HYDRATE_FLAG_NONE,
                None,
            )
        finally:
            self._k32.CloseHandle(handle)
        return result == 0 and self.is_content_available(path)


def _probe_worker(connection):
    """Runs unsafe provider calls outside the sync process's event loop.

    A cloud provider can block inside CreateFileW/CfGetPlaceholderInfo without
    raising. The parent can terminate this process; it cannot safely terminate
    a stuck Python thread.
    """
    cloud_filter = CloudFilter()
    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                return
            if request is None:
                return
            try:
                action, path = request
                if action == "available":
                    result = cloud_filter.is_content_available(path)
                elif action == "in_sync":
                    result = cloud_filter.is_content_in_sync(path)
                elif action == "hydrate":
                    result = cloud_filter.hydrate(path)
                else:
                    raise ValueError(f"unknown cloud probe action: {action}")
                connection.send(("ok", result))
            except BaseException as exc:
                connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


class CloudProbe:
    """Async facade over one disposable cloud-status helper process."""

    def __init__(self, timeout_seconds: float = 5.0, hydrate_timeout_seconds: float = 30.0, context=None):
        self.timeout_seconds = timeout_seconds
        self.hydrate_timeout_seconds = hydrate_timeout_seconds
        self._context = context or multiprocessing.get_context("spawn")
        self._connection = None
        self._process = None
        self._lock: asyncio.Lock | None = None

    def _ensure_worker(self):
        if self._process is not None and self._process.is_alive():
            return
        self._discard_worker()
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_probe_worker,
            args=(child,),
            name="oiiaw-cloud-probe",
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process

    def _discard_worker(self):
        connection, process = self._connection, self._process
        self._connection = None
        self._process = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)

    async def _request(self, action: str, path: str, timeout_seconds: float) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._ensure_worker()
            try:
                self._connection.send((action, str(path)))
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._discard_worker()
                raise CloudProbeError(f"could not contact cloud probe: {exc}") from exc

            # Pipe polling has its own hard timeout and wakes immediately on a
            # response. Running only that bounded wait in a thread avoids both
            # event-loop blocking and a 50 ms polling tax for every vault file.
            ready = await asyncio.to_thread(self._connection.poll, timeout_seconds)
            if not ready:
                self._discard_worker()
                raise CloudProbeTimeout(f"cloud {action} exceeded {timeout_seconds:.1f}s")
            try:
                kind, payload = self._connection.recv()
            except (EOFError, OSError) as exc:
                self._discard_worker()
                raise CloudProbeError(f"cloud probe exited: {exc}") from exc
            if kind == "ok":
                return bool(payload)
            raise CloudProbeError(str(payload))

    async def is_content_available(self, path: str) -> bool:
        return await self._request("available", path, self.timeout_seconds)

    async def is_content_in_sync(self, path: str) -> bool:
        return await self._request("in_sync", path, self.timeout_seconds)

    async def hydrate(self, path: str) -> bool:
        return await self._request("hydrate", path, self.hydrate_timeout_seconds)

    def close(self):
        self._discard_worker()
