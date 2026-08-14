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
from collections import deque

HISTORY_LIMIT = 50


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
        elif event_type == "ERROR":
            self.error_count += 1

    def write(self, state: str, pending: int):
        if not self._path:
            return
        data = {
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "state": state,
            "pending": pending,
            "last_event": self.last_event,
            "history": list(self._history),
            "conflict_count": self.conflict_count,
            "error_count": self.error_count,
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._path)

    @staticmethod
    def read(logs_dir: str | None) -> dict | None:
        if not logs_dir:
            return None
        try:
            with open(os.path.join(logs_dir, "status.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def is_fresh(status: dict | None, max_age: float = 10.0) -> bool:
        return status is not None and (time.time() - status.get("updated_at", 0)) < max_age
