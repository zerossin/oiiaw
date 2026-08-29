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
    def __init__(self, logs_dir: str | None):
        self._path = os.path.join(logs_dir, "status.json") if logs_dir else None
        self.pid = os.getpid()
        self.started_at = time.time()
        self.conflict_count = 0
        self.error_count = 0
        self.last_event: dict | None = None
        self._history: deque = deque(maxlen=HISTORY_LIMIT)

    def record_event(self, event_type: str, rel_path: str):
        self.last_event = {"type": event_type, "path": rel_path, "time": time.time()}
        self._history.append(self.last_event)
        if event_type == "CONFLICT":
            self.conflict_count += 1
        elif event_type in ("ERROR", "PROBE_TIMEOUT", "PROBE_ERROR"):
            self.error_count += 1

    def write(self, state: str, pending: int, parked: int = 0) -> bool:
        if not self._path:
            return False
        data = {
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "state": state,
            "pending": pending,
            "parked": parked,
            "last_event": self.last_event,
            "history": list(self._history),
            "conflict_count": self.conflict_count,
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
