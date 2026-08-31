"""
The sync daemon and its tray icon run the engine on a background thread and
pystray's event loop on the main thread — they don't share memory in any
convenient way, and a future `oiiaw status` call is a separate process
entirely. So the daemon writes its live state to `<logs_dir>/status.json` on
a heartbeat, and anything that wants to know "is it running, and how's it
doing" reads that file instead of reaching into the daemon directly.
"""

import os
import json
import time
import threading
from collections import deque

HISTORY_LIMIT = 50
# Windows can reject os.replace while another thread has status.json open.
# Serialize local readers and briefly retry races with external status readers.
_STATUS_LOCK = threading.Lock()
_REPLACE_ATTEMPTS = 3
_REPLACE_RETRY_DELAY = 0.05


class StatusReporter:
    def __init__(self, logs_dir: str | None, recovery_root: str | None = None):
        self._path = os.path.join(logs_dir, "status.json") if logs_dir else None
        self._recovery_root = recovery_root
        self.pid = os.getpid()
        self.started_at = time.time()
        self.conflict_count = 0
        self._unresolved_conflicts: dict[str, str] = {}
        self.error_count = 0
        self.last_event: dict | None = None
        self._history: deque = deque(maxlen=HISTORY_LIMIT)

    def _open_conflict_paths(self) -> set[str]:
        paths = {
            path for path in self._unresolved_conflicts
            if not os.path.isabs(path) or os.path.isfile(path)
        }
        if self._recovery_root and os.path.isdir(self._recovery_root):
            for current, _, files in os.walk(self._recovery_root):
                paths.update(os.path.join(current, name) for name in files)
        return paths

    def record_event(self, event_type: str, rel_path: str, **details):
        self.last_event = {"type": event_type, "path": rel_path, "time": time.time(), **details}
        self._history.append(self.last_event)
        if event_type == "CONFLICT":
            self.conflict_count += 1
            conflict_path = details.get("conflict_path")
            if conflict_path:
                self._unresolved_conflicts[conflict_path] = rel_path
        elif event_type in (
            "ERROR",
            "PROBE_TIMEOUT",
            "PROBE_ERROR",
            "BLOCK_ZERO",
            "BASELINE_HELD",
            "DELETE_FUSE",
            "TOMBSTONE_CONFLICT",
        ):
            self.error_count += 1

    def write(self, state: str, pending: int, parked: int = 0, scan_pending: int = 0) -> bool:
        if not self._path:
            return False
        # New recovery paths are absolute and live outside the watched vaults.
        # Once the user removes a reviewed copy, it no longer counts as open.
        open_conflicts = self._open_conflict_paths()
        self._unresolved_conflicts = {
            path: rel_path for path, rel_path in self._unresolved_conflicts.items()
            if path in open_conflicts
        }
        data = {
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "state": state,
            "pending": pending,
            "scan_pending": scan_pending,
            "parked": parked,
            "last_event": self.last_event,
            "history": list(self._history),
            "conflict_count": self.conflict_count,
            "unresolved_conflict_count": len(open_conflicts),
            "error_count": self.error_count,
        }
        tmp = self._path + ".tmp"
        with _STATUS_LOCK:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                for attempt in range(_REPLACE_ATTEMPTS):
                    try:
                        os.replace(tmp, self._path)
                        return True
                    except PermissionError:
                        if attempt == _REPLACE_ATTEMPTS - 1:
                            return False
                        time.sleep(_REPLACE_RETRY_DELAY)
            except OSError:
                return False
        return False

    @staticmethod
    def read(logs_dir: str | None) -> dict | None:
        if not logs_dir:
            return None
        with _STATUS_LOCK:
            try:
                with open(os.path.join(logs_dir, "status.json"), "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return None

    @staticmethod
    def is_fresh(status: dict | None, max_age: float = 10.0) -> bool:
        return status is not None and (time.time() - status.get("updated_at", 0)) < max_age
