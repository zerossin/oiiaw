"""Small, opt-out updater for normal PyPI installations.

The running Windows launcher cannot replace itself. This module only checks
for a newer trusted PyPI release and starts a detached helper; the helper
waits for this tray process to exit before invoking pip and relaunching it.
"""

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version
from . import __version__

PACKAGE_NAME = "oiiaw"
PYPI_JSON_URL = "https://pypi.org/pypi/oiiaw/json"
STARTUP_DELAY_SECONDS = 60
IDLE_RECHECK_SECONDS = 2
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def running_from_source() -> bool:
    return (Path(__file__).resolve().parent.parent / "pyproject.toml").is_file()


def auto_update_supported() -> bool:
    """Only index-installed wheels may overwrite themselves automatically."""
    if running_from_source():
        return False
    try:
        dist = metadata.distribution(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return False
    direct_url = dist.read_text("direct_url.json")
    # Editable and local-path installs both have direct_url metadata. Leave
    # developer checkouts and deliberate source installs under user control.
    return direct_url is None


def find_newer_version(opener=urlopen) -> str | None:
    current = Version(__version__)
    request = Request(PYPI_JSON_URL, headers={"User-Agent": f"oiiaw/{current}"})
    with opener(request, timeout=10) as response:
        payload = json.load(response)
    latest = Version(payload["info"]["version"])
    return str(latest) if latest > current else None


def launch_update_helper(version: str, tray_exe: str, logs_dir: str) -> bool:
    try:
        version = str(Version(version))
    except InvalidVersion:
        return False
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "update.log")
    command = [
        sys.executable,
        "-m",
        "oiiaw.update_helper",
        "--version",
        version,
        "--parent-pid",
        str(os.getpid()),
        "--tray-exe",
        tray_exe,
        "--log-path",
        log_path,
    ]
    try:
        subprocess.Popen(
            command,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return True
    except OSError:
        return False


class AutoUpdater:
    def __init__(self, logger, stop_event, is_idle, begin_update, check_interval: float = 86400):
        self.log = logger
        self.stop_event = stop_event
        self.is_idle = is_idle
        self.begin_update = begin_update
        self.check_interval = max(3600, check_interval)

    def run(self):
        if not auto_update_supported():
            self.log.info("UPDATE", "automatic updates disabled for a local/editable install", level="verbose")
            return
        if self.stop_event.wait(STARTUP_DELAY_SECONDS):
            return

        while not self.stop_event.is_set():
            try:
                version = find_newer_version()
            except Exception as exc:
                self.log.info("UPDATE", f"update check deferred: {exc}", level="verbose")
                version = None

            if version:
                self.log.info("UPDATE", f"oiiaw {version} is available; waiting for idle", level="important")
                while not self.stop_event.is_set() and not self.is_idle():
                    self.stop_event.wait(IDLE_RECHECK_SECONDS)
                if self.stop_event.is_set():
                    return
                if self.begin_update(version):
                    return

            if self.stop_event.wait(self.check_interval):
                return
