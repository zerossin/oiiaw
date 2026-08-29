from oiiaw.status_file import StatusReporter


def test_probe_timeout_is_counted_and_parked_count_is_reported(tmp_path):
    reporter = StatusReporter(str(tmp_path))
    reporter.record_event("PROBE_TIMEOUT", "offline.md")
    reporter.write("idle", pending=0, parked=2)

    status = StatusReporter.read(str(tmp_path))

    assert status["error_count"] == 1
    assert status["parked"] == 2
    assert status["last_event"]["type"] == "PROBE_TIMEOUT"
