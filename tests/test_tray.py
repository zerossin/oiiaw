"""
Restarting from the tray menu hands off to a freshly-launched process.
The new process's own duplicate-instance guard reads status.json's
heartbeat freshness to decide "is one already running" — so a restart
has to clear that file first, or the handoff looks indistinguishable
from two instances racing.
"""

import os
import json
import types

from oiiaw.tray import StatusWindow, TrayApp


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
