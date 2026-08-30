"""
Restarting from the tray menu hands off to a freshly-launched process.
The new process's own duplicate-instance guard reads status.json's
heartbeat freshness to decide "is one already running" — so a restart
has to clear that file first, or the handoff looks indistinguishable
from two instances racing.
"""

import os
import json
import threading
import time
import types

from oiiaw.tray import StatusWindow, TrayApp, conflict_event_paths, open_document
from oiiaw.status_file import StatusReporter


def test_relaunch_clears_stale_heartbeat_before_starting_new_process(tmp_path, monkeypatch):
    logs_dir = str(tmp_path)
    status_path = os.path.join(logs_dir, "status.json")
    with open(status_path, "w") as f:
        json.dump({"pid": 123}, f)

    config = types.SimpleNamespace(logs_dir=logs_dir)
    app = TrayApp(config, logger=None, engine=None)

    started = []
    monkeypatch.setattr("oiiaw.tray.autostart.start_now", lambda: started.append(True))

    app._relaunch()

    assert not os.path.exists(status_path)
    assert started == [True]


def test_status_window_stop_is_thread_safe_and_idempotent():
    window = StatusWindow(config=None)

    class ForeignThreadRoot:
        def after(self, *args):
            raise AssertionError("Tk must only be called from its owning thread")

    window.root = ForeignThreadRoot()
    window.stop()
    window.stop()

    assert window._stop_event.is_set()


def test_stale_heartbeat_uses_error_icon():
    app = TrayApp.__new__(TrayApp)
    stale = {"updated_at": time.time() - 60, "last_event": None, "state": "idle"}

    assert app._icon_state(stale) == "error"
    assert app._tooltip(stale) == "oiiaw — 동기화 엔진 응답 없음"


def test_scanning_state_has_a_tray_color_and_clear_tooltip():
    app = TrayApp.__new__(TrayApp)
    status = {
        "updated_at": time.time(),
        "last_event": None,
        "state": "scanning",
        "scan_pending": 600,
        "pending": 0,
        "parked": 0,
        "conflict_count": 0,
    }

    assert app._icon_state(status) == "scanning"
    assert "초기 검사 600개" in app._tooltip(status)


def test_engine_is_rebuilt_after_unexpected_exit(monkeypatch):
    stop = threading.Event()
    errors = []

    class Logger:
        def error(self, tag, message, level="normal"):
            errors.append((tag, message))

    class CrashedEngine:
        def __init__(self):
            self.status = StatusReporter(None)

        async def run(self):
            raise RuntimeError("watcher failed")

    class RecoveredEngine:
        def __init__(self, config, logger):
            self.status = StatusReporter(None)

        async def run(self):
            stop.set()

    app = TrayApp.__new__(TrayApp)
    app.config = types.SimpleNamespace()
    app.log = Logger()
    app.engine = CrashedEngine()
    app._stop = stop

    monkeypatch.setattr("oiiaw.tray.SyncEngine", RecoveredEngine)
    monkeypatch.setattr("oiiaw.tray._ENGINE_RETRY_INITIAL", 0.001)
    monkeypatch.setattr("oiiaw.tray._ENGINE_RETRY_MAX", 0.001)

    app._run_engine()

    assert isinstance(app.engine, RecoveredEngine)
    assert errors and errors[0][0] == "ENGINE"


def test_markdown_document_opens_with_absolute_obsidian_uri(tmp_path, monkeypatch):
    note = tmp_path / "한 글.md"
    note.write_text("content", encoding="utf-8")
    opened = []
    monkeypatch.setattr("oiiaw.tray.os.startfile", opened.append)

    assert open_document(str(note)) is True
    assert opened[0].startswith("obsidian://open?path=")
    assert "%20" in opened[0]


def test_conflict_event_resolves_local_current_and_backup_paths(tmp_path):
    config = types.SimpleNamespace(local_vault=str(tmp_path))
    current, conflict = conflict_event_paths(
        config,
        {"type": "CONFLICT", "path": "notes/note.md", "conflict_path": "notes/note_CONFLICT_1.md"},
    )

    assert current == os.path.join(str(tmp_path), "notes/note.md")
    assert conflict == os.path.join(str(tmp_path), "notes/note_CONFLICT_1.md")


def test_update_handoff_stops_tray_without_normal_relaunch(tmp_path, monkeypatch):
    calls = []

    class Logger:
        def info(self, *args, **kwargs): calls.append("logged")
        def error(self, *args, **kwargs): calls.append("error")
        def flush(self): calls.append("flushed")

    app = TrayApp.__new__(TrayApp)
    app.config = types.SimpleNamespace(logs_dir=str(tmp_path))
    app.log = Logger()
    app._stop = threading.Event()
    app.engine = types.SimpleNamespace(request_shutdown=lambda: calls.append("engine-stopped"))
    app._status_window = types.SimpleNamespace(stop=lambda: calls.append("window-stopped"))
    app._icon = types.SimpleNamespace(stop=lambda: calls.append("icon-stopped"))
    app._restart_requested = False
    monkeypatch.setattr("oiiaw.tray.autostart._locate_tray_exe", lambda: r"C:\Python\oiiaw-tray.exe")
    monkeypatch.setattr("oiiaw.tray.launch_update_helper", lambda *args: True)

    assert app._begin_update("0.1.4") is True
    assert app._stop.is_set()
    assert app._restart_requested is False
    assert "engine-stopped" in calls
    assert "icon-stopped" in calls
