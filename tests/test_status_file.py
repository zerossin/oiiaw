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
