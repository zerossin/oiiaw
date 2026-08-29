import types

from oiiaw import autostart


def completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_scheduler_success_removes_registry_fallback(monkeypatch):
    monkeypatch.setattr(autostart, "_locate_tray_exe", lambda: r"C:\Python\Scripts\oiiaw-tray.exe")
    monkeypatch.setattr(autostart.subprocess, "run", lambda *args, **kwargs: completed())
    removed = []
    monkeypatch.setattr(autostart, "_remove_user_run", lambda: (removed.append(True) or True, ""))

    ok, detail = autostart.register()

    assert ok is True
    assert removed == [True]
    assert "작업 스케줄러" not in detail


def test_scheduler_failure_uses_user_run_fallback(monkeypatch):
    exe = r"C:\Users\me\Python\Scripts\oiiaw-tray.exe"
    monkeypatch.setattr(autostart, "_locate_tray_exe", lambda: exe)
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *args, **kwargs: completed(returncode=1, stderr="access denied"),
    )
    registered = []
    monkeypatch.setattr(
        autostart,
        "_register_user_run",
        lambda path: (registered.append(path) or True, "사용자 계정 자동 시작으로 등록했습니다."),
    )

    ok, detail = autostart.register()

    assert ok is True
    assert registered == [exe]
    assert "작업 스케줄러 대신" in detail


def test_matching_existing_task_is_kept_without_duplicate_fallback(monkeypatch):
    exe = r"C:\Users\me\Python\Scripts\oiiaw-tray.exe"
    task_xml = f"""<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
        <Actions><Exec><Command>\"{exe}\"</Command></Exec></Actions>
    </Task>"""

    def run(command, **kwargs):
        if "/create" in command:
            return completed(returncode=1, stderr="access denied")
        return completed(stdout=task_xml)

    monkeypatch.setattr(autostart, "_locate_tray_exe", lambda: exe)
    monkeypatch.setattr(autostart.subprocess, "run", run)
    removed = []
    monkeypatch.setattr(autostart, "_remove_user_run", lambda: (removed.append(True) or True, ""))
    monkeypatch.setattr(
        autostart,
        "_register_user_run",
        lambda path: (_ for _ in ()).throw(AssertionError("fallback must not be registered")),
    )

    ok, detail = autostart.register()

    assert ok is True
    assert removed == [True]
    assert "기존 Windows" in detail


def test_mismatched_existing_task_refuses_duplicate_registration(monkeypatch):
    current = r"C:\NewPython\Scripts\oiiaw-tray.exe"
    old = r"C:\OldPython\Scripts\oiiaw-tray.exe"
    task_xml = f"""<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
        <Actions><Exec><Command>\"{old}\"</Command></Exec></Actions>
    </Task>"""

    def run(command, **kwargs):
        if "/create" in command:
            return completed(returncode=1, stderr="access denied")
        return completed(stdout=task_xml)

    monkeypatch.setattr(autostart, "_locate_tray_exe", lambda: current)
    monkeypatch.setattr(autostart.subprocess, "run", run)
    monkeypatch.setattr(
        autostart,
        "_register_user_run",
        lambda path: (_ for _ in ()).throw(AssertionError("duplicate fallback must not be registered")),
    )

    ok, detail = autostart.register()

    assert ok is False
    assert "다른 실행 파일" in detail


def test_both_autostart_methods_report_failure(monkeypatch):
    monkeypatch.setattr(autostart, "_locate_tray_exe", lambda: r"C:\Python\Scripts\oiiaw-tray.exe")
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *args, **kwargs: completed(returncode=1, stderr="scheduler failed"),
    )
    monkeypatch.setattr(autostart, "_register_user_run", lambda path: (False, "registry failed"))

    ok, detail = autostart.register()

    assert ok is False
    assert "scheduler failed" in detail
    assert "registry failed" in detail


def test_unregister_removes_task_and_registry(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return completed()

    monkeypatch.setattr(autostart.subprocess, "run", run)
    removed = []
    monkeypatch.setattr(autostart, "_remove_user_run", lambda: (removed.append(True) or True, ""))

    ok, detail = autostart.unregister()

    assert ok is True
    assert calls[0][1] == "/query"
    assert calls[1][1] == "/delete"
    assert removed == [True]
    assert "사용하지 않도록" in detail
