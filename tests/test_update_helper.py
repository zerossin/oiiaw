import types

from oiiaw import update_helper


def test_successful_update_restarts_tray(tmp_path, monkeypatch):
    tray = str(tmp_path / "oiiaw-tray.exe")
    log = str(tmp_path / "update.log")
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
    assert restarted == [([tray], {"cwd": str(tmp_path)})]
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
    assert restarted == [([tray], {"cwd": str(tmp_path)})]
