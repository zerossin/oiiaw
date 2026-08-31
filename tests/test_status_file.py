import os

from oiiaw import status_file
from oiiaw.status_file import StatusReporter


def test_probe_timeout_is_counted_and_parked_count_is_reported(tmp_path):
    reporter = StatusReporter(str(tmp_path))
    reporter.record_event("PROBE_TIMEOUT", "offline.md")
    reporter.write("idle", pending=0, parked=2)

    status = StatusReporter.read(str(tmp_path))

    assert status["error_count"] == 1
    assert status["parked"] == 2
    assert status["last_event"]["type"] == "PROBE_TIMEOUT"


def test_startup_scan_count_is_separate_from_pending_changes(tmp_path):
    reporter = StatusReporter(str(tmp_path))

    reporter.write("scanning", pending=2, parked=0, scan_pending=600)
    status = StatusReporter.read(str(tmp_path))

    assert status["state"] == "scanning"
    assert status["pending"] == 2
    assert status["scan_pending"] == 600


def test_event_details_are_written_for_conflict_actions(tmp_path):
    reporter = StatusReporter(str(tmp_path))
    reporter.record_event("CONFLICT", "note.md", conflict_path="note_CONFLICT_1.md")
    reporter.write("idle", pending=0)

    status = StatusReporter.read(str(tmp_path))

    assert status["last_event"]["conflict_path"] == "note_CONFLICT_1.md"


def test_open_conflicts_are_separate_from_session_history(tmp_path):
    recovery = tmp_path / "recovery" / "note_CONFLICT_1.md"
    recovery.parent.mkdir()
    recovery.write_text("preserved remote bytes")
    reporter = StatusReporter(str(tmp_path))
    reporter.record_event("CONFLICT", "note.md", conflict_path=str(recovery))
    reporter.write("idle", pending=0)

    status = StatusReporter.read(str(tmp_path))
    assert status["conflict_count"] == 1
    assert status["unresolved_conflict_count"] == 1

    recovery.unlink()
    reporter.write("idle", pending=0)
    status = StatusReporter.read(str(tmp_path))
    assert status["conflict_count"] == 1
    assert status["unresolved_conflict_count"] == 0


def test_recovery_store_is_recounted_after_restart(tmp_path):
    recovery_root = tmp_path / "recovery"
    recovery = recovery_root / "notes" / "note_CONFLICT_1.md"
    recovery.parent.mkdir(parents=True)
    recovery.write_text("preserved remote bytes")

    restarted = StatusReporter(str(tmp_path), str(recovery_root))
    restarted.write("idle", pending=0)
    status = StatusReporter.read(str(tmp_path))

    assert status["conflict_count"] == 0
    assert status["unresolved_conflict_count"] == 1


def test_write_retries_when_windows_temporarily_blocks_replace(tmp_path, monkeypatch):
    reporter = StatusReporter(str(tmp_path))
    real_replace = os.replace
    attempts = 0

    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("file is being read")
        real_replace(src, dst)

    monkeypatch.setattr(status_file.os, "replace", flaky_replace)
    monkeypatch.setattr(status_file.time, "sleep", lambda _: None)

    assert reporter.write("idle", pending=0) is True
    assert attempts == 3
    assert StatusReporter.read(str(tmp_path))["state"] == "idle"


def test_write_failure_does_not_escape_into_sync_engine(tmp_path, monkeypatch):
    reporter = StatusReporter(str(tmp_path))

    def locked_replace(*_):
        raise PermissionError("still locked")

    monkeypatch.setattr(status_file.os, "replace", locked_replace)
    monkeypatch.setattr(status_file.time, "sleep", lambda _: None)

    assert reporter.write("idle", pending=0) is False
