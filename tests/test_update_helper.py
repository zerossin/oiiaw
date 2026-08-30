import types

from oiiaw import update_helper


def test_successful_update_restarts_tray(tmp_path, monkeypatch):
    tray = str(tmp_path / "oiiaw-tray.exe")
    log = str(tmp_path / "update.log")
    status = tmp_path / "status.json"
    status.write_text("stale heartbeat", encoding="utf-8")
    restarted = []
    command_seen = []
    monkeypatch.setattr(update_helper, "wait_for_parent", lambda pid: True)
    monkeypatch.setattr(
        update_helper.subprocess,
        "run",
        lambda command, **kwargs: (command_seen.append(command) or types.SimpleNamespace(returncode=0, stdout="ok", stderr="")),
    )
    monkeypatch.setattr(update_helper.subprocess, "Popen", lambda command, **kwargs: restarted.append((command, kwargs)))

    ok = update_helper.install_and_restart("0.1.4", 123, tray, log)

    assert ok is True
    assert command_seen[0][-1] == "oiiaw==0.1.4"
    assert len(restarted) == 1
    assert restarted[0][0] == [tray]
    assert restarted[0][1]["cwd"] == str(tmp_path)
    assert restarted[0][1]["close_fds"] is True
    assert not status.exists()
    assert "exit 0" in (tmp_path / "update.log").read_text(encoding="utf-8")


def test_failed_update_still_attempts_to_restore_tray(tmp_path, monkeypatch):
    tray = str(tmp_path / "oiiaw-tray.exe")
    restarted = []
    monkeypatch.setattr(update_helper, "wait_for_parent", lambda pid: True)
    monkeypatch.setattr(
        update_helper.subprocess,
        "run",
        lambda command, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="failed"),
    )
    monkeypatch.setattr(update_helper.subprocess, "Popen", lambda command, **kwargs: restarted.append((command, kwargs)))

    ok = update_helper.install_and_restart("0.1.4", 123, tray, str(tmp_path / "update.log"))

    assert ok is False
    assert len(restarted) == 1
    assert restarted[0][0] == [tray]
    assert restarted[0][1]["cwd"] == str(tmp_path)


def test_restart_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(update_helper, "wait_for_parent", lambda pid: True)
    monkeypatch.setattr(
        update_helper.subprocess,
        "run",
        lambda command, **kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        update_helper.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cannot launch")),
    )
    log = tmp_path / "update.log"

    assert update_helper.install_and_restart("0.1.6", 123, str(tmp_path / "oiiaw-tray.exe"), str(log)) is False
    assert "restart failed" in log.read_text(encoding="utf-8")
