"""
pystray's Windows backend expects `Icon.run()` to own the thread it's called
from, so the sync engine can't share that thread the way `oiiaw run` used to
drive it directly. Instead the engine runs its own asyncio loop on a
background thread, and this thread just polls the status file it writes
and reflects that back onto the icon.
"""

import os
import time
import asyncio
import threading

from PIL import Image, ImageDraw
import pystray

from .status_file import StatusReporter

_COLORS = {
    "idle": (70, 130, 180),
    "syncing": (240, 170, 30),
    "conflict": (210, 60, 60),
    "error": (210, 60, 60),
}

_REFRESH_SECONDS = 2


def _dot(color) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, size - 4, size - 4), fill=color)
    return img


class TrayApp:
    def __init__(self, config, logger, engine):
        self.config = config
        self.log = logger
        self.engine = engine
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "oiiaw",
            _dot(_COLORS["idle"]),
            "oiiaw",
            menu=pystray.Menu(
                pystray.MenuItem("상태 보기", self._show_status),
                pystray.MenuItem("로그 폴더 열기", self._open_logs),
                pystray.MenuItem("종료", self._quit),
            ),
        )

    def _tooltip(self, status: dict | None) -> str:
        if not status or not StatusReporter.is_fresh(status):
            return "oiiaw — 시작 중..."
        lines = [f"oiiaw — {status['state']}"]
        last = status.get("last_event")
        if last:
            lines.append(f"최근: {last['type']} {last['path']}")
        if status.get("conflict_count"):
            lines.append(f"충돌 {status['conflict_count']}건 (이번 세션)")
        return "\n".join(lines)

    def _icon_state(self, status: dict | None) -> str:
        if not status or not StatusReporter.is_fresh(status):
            return "idle"
        last = status.get("last_event")
        if last and last["type"] in ("CONFLICT", "ERROR") and time.time() - last["time"] < 300:
            return "conflict"
        return status.get("state", "idle")

    def _refresh_loop(self):
        while not self._stop.is_set():
            status = StatusReporter.read(self.config.logs_dir)
            self._icon.icon = _dot(_COLORS[self._icon_state(status)])
            self._icon.title = self._tooltip(status)
            time.sleep(_REFRESH_SECONDS)

    def _show_status(self, icon, item):
        icon.notify(self._tooltip(StatusReporter.read(self.config.logs_dir)), "oiiaw 상태")

    def _open_logs(self, icon, item):
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.startfile(self.config.logs_dir)

    def _quit(self, icon, item):
        self._stop.set()
        self.engine.request_shutdown()
        icon.stop()

    def run(self):
        engine_thread = threading.Thread(target=lambda: asyncio.run(self.engine.run()), daemon=True)
        engine_thread.start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self._icon.run()
        self._stop.set()
        self.engine.request_shutdown()
        engine_thread.join(timeout=5)
