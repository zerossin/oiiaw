import io
import json
import threading
import tomllib
import types
from pathlib import Path

from importlib import metadata

from oiiaw import updater
from oiiaw import __version__


class Distribution:
    def __init__(self, direct_url=None):
        self.direct_url = direct_url

    def read_text(self, name):
        return self.direct_url if name == "direct_url.json" else None


def test_package_and_project_versions_match():
    project = Path(__file__).parents[1] / "pyproject.toml"
    with open(project, "rb") as source:
        configured = tomllib.load(source)["project"]["version"]

    assert __version__ == configured


def test_auto_update_only_supports_index_install(monkeypatch):
    monkeypatch.setattr(updater, "running_from_source", lambda: False)
    monkeypatch.setattr(updater.metadata, "distribution", lambda name: Distribution())
    assert updater.auto_update_supported() is True

    direct = json.dumps({"url": "file:///repo", "dir_info": {"editable": True}})
    monkeypatch.setattr(updater.metadata, "distribution", lambda name: Distribution(direct))
    assert updater.auto_update_supported() is False


def test_missing_distribution_disables_auto_update(monkeypatch):
    monkeypatch.setattr(updater, "running_from_source", lambda: False)
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(updater.metadata, "distribution", missing)
    assert updater.auto_update_supported() is False


def test_find_newer_version_uses_pypi_metadata(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.3")
    payload = io.BytesIO(json.dumps({"info": {"version": "0.1.4"}}).encode())

    assert updater.find_newer_version(lambda request, timeout: payload) == "0.1.4"


def test_find_newer_version_ignores_current_release(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.4")
    payload = io.BytesIO(json.dumps({"info": {"version": "0.1.4"}}).encode())

    assert updater.find_newer_version(lambda request, timeout: payload) is None


def test_launch_helper_pins_exact_version(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda command, **kwargs: launched.append((command, kwargs)))
    monkeypatch.setattr(updater.sys, "executable", r"C:\Python\pythonw.exe")
    monkeypatch.setattr(updater.os, "getpid", lambda: 123)

    ok = updater.launch_update_helper("0.1.4", r"C:\Python\Scripts\oiiaw-tray.exe", str(tmp_path))

    assert ok is True
    command, kwargs = launched[0]
    assert command[:3] == [r"C:\Python\pythonw.exe", "-m", "oiiaw.update_helper"]
    assert command[command.index("--version") + 1] == "0.1.4"
    assert command[command.index("--parent-pid") + 1] == "123"


def test_source_checkout_never_auto_updates(monkeypatch):
    monkeypatch.setattr(updater, "running_from_source", lambda: True)
    monkeypatch.setattr(
        updater.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(AssertionError("metadata must not be consulted")),
    )

    assert updater.auto_update_supported() is False


def test_updater_waits_for_idle_then_hands_off(monkeypatch):
    stop = threading.Event()
    states = iter([False, True])
    begun = []
    logger = types.SimpleNamespace(info=lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "auto_update_supported", lambda: True)
    monkeypatch.setattr(updater, "find_newer_version", lambda: "0.1.4")
    monkeypatch.setattr(updater, "STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(updater, "IDLE_RECHECK_SECONDS", 0.001)

    def begin(version):
        begun.append(version)
        stop.set()
        return True

    worker = updater.AutoUpdater(logger, stop, lambda: next(states), begin)
    worker.run()

    assert begun == ["0.1.4"]
