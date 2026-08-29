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
from urllib.parse import quote
from tkinter import messagebox

import pystray

from . import autostart
from .sync_engine import SyncEngine
from .status_file import StatusReporter
from .updater import AutoUpdater, launch_update_helper
from .ui_assets import apply_window_icon, configure_windows_app_identity, tray_icon

_COLORS = {
    "idle": (70, 130, 180),
    "syncing": (240, 170, 30),
    "conflict": (210, 60, 60),
    "error": (210, 60, 60),
}

_REFRESH_SECONDS = 2
_ENGINE_RETRY_INITIAL = 2
_ENGINE_RETRY_MAX = 60


def open_document(path: str) -> bool:
    """Open Markdown in Obsidian; use the Windows default app otherwise."""
    if not path or not os.path.isfile(path):
        return False
    target = path
    if os.path.splitext(path)[1].lower() == ".md":
        target = f"obsidian://open?path={quote(os.path.abspath(path), safe='')}"
    try:
        os.startfile(target)
        return True
    except OSError:
        return False


def conflict_event_paths(config, event: dict | None) -> tuple[str | None, str | None]:
    if not event or event.get("type") != "CONFLICT":
        return None, None
    current = os.path.join(config.local_vault, event.get("path", ""))
    conflict_rel = event.get("conflict_path")
    conflict = os.path.join(config.local_vault, conflict_rel) if conflict_rel else None
    return current, conflict


class StatusWindow:
    """Runs on its own thread with its own Tk mainloop. pystray's menu
    callbacks fire on a thread that isn't the icon's own message-pump
    thread (which this can't share anyway — Tk needs to own whichever
    thread calls its mainloop), so this gets a dedicated one. The only
    thing crossing threads is a plain Event; the window polls that plus
    status.json itself on a timer rather than being pushed to, matching
    how everything else here already works."""

    _STATE_KR = {"idle": "대기 중", "syncing": "동기화 중", "error": "자동 복구 중"}
    _EVENT_KR = {
        "PUSH": "iCloud로 보냄",
        "PULL": "iCloud에서 받음",
        "DELETE": "삭제 반영",
        "CONFLICT": "충돌본 보관",
        "RESOLVED": "충돌 자동 해소",
        "RECOVERED": "자동 복구 완료",
        "PARK": "iCloud 다운로드 대기",
        "PROBE_TIMEOUT": "iCloud 확인 재시도",
        "PROBE_ERROR": "iCloud 확인 재시도",
        "ERROR": "오류 자동 재시도",
    }

    def __init__(self, config):
        self.config = config
        self._show_event = threading.Event()
        self._stop_event = threading.Event()
        self.root = None

    def request_show(self):
        self._show_event.set()

    def stop(self):
        # Tk belongs to the window thread. Calling root.after() from a
        # pystray callback can race with mainloop teardown, and restart calls
        # stop twice by design. Let the existing poll destroy its own root.
        self._stop_event.set()

    def run(self):
        configure_windows_app_identity()
        self.root = tk.Tk()
        apply_window_icon(self.root)
        self.root.title("oiiaw 상태")
        self.root.geometry("460x470")
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
        self.history_list.bind("<<ListboxSelect>>", self._on_history_select)
        self.history_list.bind("<Double-Button-1>", lambda event: self._open_selected("current"))

        action_frame = tk.Frame(self.root)
        action_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.current_button = tk.Button(
            action_frame, text="현재 문서 열기", state="disabled",
            command=lambda: self._open_selected("current"),
        )
        self.current_button.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.conflict_button = tk.Button(
            action_frame, text="충돌본 열기", state="disabled",
            command=lambda: self._open_selected("conflict"),
        )
        self.conflict_button.pack(side="left", expand=True, fill="x", padx=4)
        self.folder_button = tk.Button(
            action_frame, text="파일 위치 열기", state="disabled",
            command=lambda: self._open_selected("folder"),
        )
        self.folder_button.pack(side="left", expand=True, fill="x", padx=(4, 0))

        tk.Button(self.root, text="로그 폴더 열기", command=self._open_logs).pack(pady=(0, 12))

        self.root.withdraw()
        self._poll()
        self.root.mainloop()
        self.root = None

    def _selected_event(self) -> dict | None:
        selection = self.history_list.curselection()
        if not selection or selection[0] >= len(getattr(self, "_displayed_events", [])):
            return None
        return self._displayed_events[selection[0]]

    def _on_history_select(self, event=None):
        current, conflict = conflict_event_paths(self.config, self._selected_event())
        self.current_button.config(state="normal" if current and os.path.isfile(current) else "disabled")
        self.conflict_button.config(state="normal" if conflict and os.path.isfile(conflict) else "disabled")
        folder_target = conflict if conflict and os.path.isfile(conflict) else current
        self.folder_button.config(state="normal" if folder_target and os.path.exists(folder_target) else "disabled")

    def _open_selected(self, which: str):
        current, conflict = conflict_event_paths(self.config, self._selected_event())
        path = conflict if which == "conflict" else current
        if which == "folder":
            path = conflict if conflict and os.path.exists(conflict) else current
            if path and os.path.exists(path):
                os.startfile(os.path.dirname(path))
                return
        elif open_document(path):
            return
        messagebox.showinfo("oiiaw", "파일이 아직 동기화 중이거나 더 이상 존재하지 않습니다.")

    def _open_logs(self):
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.startfile(self.config.logs_dir)

    def _poll(self):
        if self._stop_event.is_set():
            self.root.destroy()
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
        selected = self._selected_event()
        selected_time = selected.get("time") if selected else None
        status = StatusReporter.read(self.config.logs_dir)
        if not status:
            self.state_label.config(text="상태: 시작 중...")
            self.paths_label.config(text="")
            return
        if not StatusReporter.is_fresh(status):
            age = int(time.time() - status.get("updated_at", 0))
            self.state_label.config(text=f"상태: 동기화 엔진 응답 없음  ·  {age}초째", fg="#b23a2e")
            self.paths_label.config(text=f"로컬: {self.config.local_vault}\niCloud: {self.config.cloud_vault}")
            return

        state_kr = self._STATE_KR.get(status["state"], status["state"])
        uptime_min = int((time.time() - status["started_at"]) // 60)
        summary = f"상태: {state_kr}  ·  대기 {status['pending']}개  ·  {uptime_min}분째 실행 중"
        if status.get("parked"):
            summary += f"  ·  iCloud 다운로드 재시도 {status['parked']}개"
        if status.get("conflict_count"):
            summary += f"  ·  충돌 {status['conflict_count']}건"
        self.state_label.config(text=summary, fg="#000000")
        self.paths_label.config(text=f"로컬: {self.config.local_vault}\niCloud: {self.config.cloud_vault}")

        self.history_list.delete(0, tk.END)
        self._displayed_events = list(reversed(status.get("history") or []))
        selected_index = None
        for index, event in enumerate(self._displayed_events):
            stamp = time.strftime("%H:%M:%S", time.localtime(event["time"]))
            event_name = self._EVENT_KR.get(event["type"], event["type"])
            self.history_list.insert(tk.END, f"{stamp}  {event_name:<14} {event['path']}")
            if selected_time == event.get("time"):
                selected_index = index
        if not self._displayed_events:
            self.history_list.insert(tk.END, "(아직 활동 없음)")
        elif selected_index is not None:
            self.history_list.selection_set(selected_index)
        self._on_history_select()


class TrayApp:
    def __init__(self, config, logger, engine):
        self.config = config
        self.log = logger
        self.engine = engine
        self._stop = threading.Event()
        self._restart_requested = False
        self._status_window = StatusWindow(config)
        self._icon = pystray.Icon(
            "oiiaw",
            tray_icon(_COLORS["idle"]),
            "oiiaw",
            menu=pystray.Menu(
                pystray.MenuItem("상태 보기", self._show_status, default=True),
                pystray.MenuItem("로그 폴더 열기", self._open_logs),
                pystray.MenuItem("재시작", self._restart),
                pystray.MenuItem("종료", self._quit),
            ),
        )

    def _tooltip(self, status: dict | None) -> str:
        if not status:
            return "oiiaw — 시작 중..."
        if not StatusReporter.is_fresh(status):
            return "oiiaw — 동기화 엔진 응답 없음"
        state_name = StatusWindow._STATE_KR.get(status["state"], status["state"])
        lines = [f"oiiaw — {state_name}"]
        if status.get("parked"):
            lines.append(f"iCloud 다운로드 재시도 {status['parked']}개")
        last = status.get("last_event")
        if last:
            event_name = StatusWindow._EVENT_KR.get(last["type"], last["type"])
            lines.append(f"최근: {event_name} {last['path']}")
        if status.get("conflict_count"):
            lines.append(f"충돌 {status['conflict_count']}건 (이번 세션)")
        return "\n".join(lines)

    def _icon_state(self, status: dict | None) -> str:
        if not status:
            return "idle"
        if not StatusReporter.is_fresh(status):
            return "error"
        last = status.get("last_event")
        if last and last["type"] in ("CONFLICT", "ERROR", "PROBE_TIMEOUT", "PROBE_ERROR") and time.time() - last["time"] < 300:
            return "conflict"
        return status.get("state", "idle")

    def _refresh_loop(self):
        while not self._stop.is_set():
            status = StatusReporter.read(self.config.logs_dir)
            self._icon.icon = tray_icon(_COLORS[self._icon_state(status)])
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

    def _restart(self, icon, item):
        self._restart_requested = True
        self._quit(icon, item)

    def _sync_is_idle(self) -> bool:
        status = StatusReporter.read(self.config.logs_dir)
        return bool(
            StatusReporter.is_fresh(status)
            and status.get("state") == "idle"
            and status.get("pending", 0) == 0
        )

    def _begin_update(self, version: str) -> bool:
        tray_exe = autostart._locate_tray_exe()
        if not tray_exe or not launch_update_helper(version, tray_exe, self.config.logs_dir):
            self.log.error("UPDATE", f"could not launch updater for {version}", level="important")
            return False
        self.log.info("UPDATE", f"installing oiiaw {version}; restarting automatically", level="important")
        self.log.flush()
        # The detached helper owns the relaunch after this process releases
        # the running oiiaw-tray.exe file.
        self._stop.set()
        self.engine.request_shutdown()
        self._status_window.stop()
        self._icon.stop()
        return True

    def _relaunch(self):
        # Wipe the heartbeat file first — the new process's own duplicate-
        # instance check would otherwise see this one's last (still-fresh)
        # heartbeat and refuse to start, mistaking a clean handoff for two
        # instances racing.
        try:
            os.remove(os.path.join(self.config.logs_dir, "status.json"))
        except OSError:
            pass
        autostart.start_now()

    def _run_engine(self):
        """Rebuild and retry an engine that exits unexpectedly."""
        delay = _ENGINE_RETRY_INITIAL
        while not self._stop.is_set():
            failure = None
            try:
                asyncio.run(self.engine.run())
            except Exception as exc:
                failure = exc

            if self._stop.is_set():
                return

            message = str(failure) if failure else "동기화 엔진이 예기치 않게 종료됨"
            self.log.error("ENGINE", f"{message}; {delay:.0f}초 후 자동 재시작", level="important")
            self.engine.status.record_event("ERROR", message)
            self.engine.status.write("error", 0, 0)

            if self._stop.wait(delay):
                return
            # A fresh instance also gets a fresh asyncio loop, file watcher,
            # and cloud helper instead of reusing partially-failed state.
            self.engine = SyncEngine(self.config, self.log)
            delay = min(delay * 2, _ENGINE_RETRY_MAX)

    def run(self):
        engine_thread = threading.Thread(target=self._run_engine, daemon=True)
        engine_thread.start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        window_thread = threading.Thread(target=self._status_window.run, daemon=True)
        window_thread.start()
        if getattr(self.config, "auto_update", True):
            updater = AutoUpdater(
                self.log,
                self._stop,
                self._sync_is_idle,
                self._begin_update,
                getattr(self.config, "update_check_interval", 86400),
            )
            threading.Thread(target=updater.run, daemon=True).start()
        self._icon.run()
        self._stop.set()
        self.engine.request_shutdown()
        self._status_window.stop()
        engine_thread.join(timeout=5)
        window_thread.join(timeout=5)
        if self._restart_requested:
            self._relaunch()
