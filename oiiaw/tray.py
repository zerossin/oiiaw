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
import tkinter as tk

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


class StatusWindow:
    """Runs on its own thread with its own Tk mainloop. pystray's menu
    callbacks fire on a thread that isn't the icon's own message-pump
    thread (which this can't share anyway — Tk needs to own whichever
    thread calls its mainloop), so this gets a dedicated one. The only
    thing crossing threads is a plain Event; the window polls that plus
    status.json itself on a timer rather than being pushed to, matching
    how everything else here already works."""

    _STATE_KR = {"idle": "대기 중", "syncing": "동기화 중"}

    def __init__(self, config):
        self.config = config
        self._show_event = threading.Event()
        self._stop_event = threading.Event()
        self.root = None

    def request_show(self):
        self._show_event.set()

    def stop(self):
        self._stop_event.set()
        if self.root:
            self.root.after(0, self.root.destroy)

    def run(self):
        self.root = tk.Tk()
        self.root.title("oiiaw 상태")
        self.root.geometry("420x420")
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)

        self.state_label = tk.Label(self.root, text="", font=("Segoe UI", 11, "bold"), anchor="w", justify="left")
        self.state_label.pack(fill="x", padx=12, pady=(12, 4))

        self.paths_label = tk.Label(self.root, text="", fg="#555555", anchor="w", justify="left", wraplength=390)
        self.paths_label.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(self.root, text="최근 활동", anchor="w").pack(fill="x", padx=12)

        list_frame = tk.Frame(self.root)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.history_list = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=("Consolas", 9))
        self.history_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.history_list.yview)

        tk.Button(self.root, text="로그 폴더 열기", command=self._open_logs).pack(pady=(0, 12))

        self.root.withdraw()
        self._poll()
        self.root.mainloop()

    def _open_logs(self):
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.startfile(self.config.logs_dir)

    def _poll(self):
        if self._stop_event.is_set():
            return
        if self._show_event.is_set():
            self._show_event.clear()
            self._refresh()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        elif self.root.state() != "withdrawn":
            self._refresh()
        self.root.after(1000, self._poll)

    def _refresh(self):
        status = StatusReporter.read(self.config.logs_dir)
        if not status or not StatusReporter.is_fresh(status):
            self.state_label.config(text="상태: 시작 중...")
            self.paths_label.config(text="")
            return

        state_kr = self._STATE_KR.get(status["state"], status["state"])
        uptime_min = int((time.time() - status["started_at"]) // 60)
        summary = f"상태: {state_kr}  ·  대기 {status['pending']}개  ·  {uptime_min}분째 실행 중"
        if status.get("conflict_count"):
            summary += f"  ·  충돌 {status['conflict_count']}건"
        self.state_label.config(text=summary)
        self.paths_label.config(text=f"로컬: {self.config.local_vault}\niCloud: {self.config.cloud_vault}")

        self.history_list.delete(0, tk.END)
        history = status.get("history") or []
        for event in reversed(history):
            stamp = time.strftime("%H:%M:%S", time.localtime(event["time"]))
            self.history_list.insert(tk.END, f"{stamp}  {event['type']:<9} {event['path']}")
        if not history:
            self.history_list.insert(tk.END, "(아직 활동 없음)")


class TrayApp:
    def __init__(self, config, logger, engine):
        self.config = config
        self.log = logger
        self.engine = engine
        self._stop = threading.Event()
        self._status_window = StatusWindow(config)
        self._icon = pystray.Icon(
            "oiiaw",
            _dot(_COLORS["idle"]),
            "oiiaw",
            menu=pystray.Menu(
                pystray.MenuItem("상태 보기", self._show_status, default=True),
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
        self._status_window.request_show()

    def _open_logs(self, icon, item):
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.startfile(self.config.logs_dir)

    def _quit(self, icon, item):
        self._stop.set()
        self.engine.request_shutdown()
        self._status_window.stop()
        icon.stop()

    def run(self):
        engine_thread = threading.Thread(target=lambda: asyncio.run(self.engine.run()), daemon=True)
        engine_thread.start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        window_thread = threading.Thread(target=self._status_window.run, daemon=True)
        window_thread.start()
        self._icon.run()
        self._stop.set()
        self.engine.request_shutdown()
        self._status_window.stop()
        engine_thread.join(timeout=5)
        window_thread.join(timeout=5)
