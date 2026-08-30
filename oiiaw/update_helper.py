"""Detached second stage used by oiiaw's Windows self-update."""

import argparse
import ctypes
import os
import subprocess
import sys
import time

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def wait_for_parent(pid: int, timeout_seconds: int = 120) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True
        try:
            result = kernel32.WaitForSingleObject(handle, timeout_seconds * 1000)
            return result != WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def install_and_restart(version: str, parent_pid: int, tray_exe: str, log_path: str) -> bool:
    if not wait_for_parent(parent_pid):
        return False
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        f"oiiaw=={version}",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"[{stamp}] update to {version}: exit {result.returncode}\n")
            if result.stdout:
                log.write(result.stdout + "\n")
            if result.stderr:
                log.write(result.stderr + "\n")
        success = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        success = False
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] update failed: {exc}\n")
        except OSError:
            pass

    # On failure pip normally leaves or restores the previous installation.
    # The parent's final heartbeat is still fresh here; remove it only after
    # the parent has exited or the replacement process will reject itself as
    # a duplicate and synchronization will stay off silently.
    status_path = os.path.join(os.path.dirname(log_path), "status.json")
    try:
        os.remove(status_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] stale status cleanup failed: {exc}\n")
        except OSError:
            pass

    # Relaunch either way so synchronization does not stay off silently.
    restart_started = False
    try:
        kwargs = {"cwd": os.path.dirname(tray_exe), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen([tray_exe], **kwargs)
        restart_started = True
    except OSError as exc:
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] restart failed: {exc}\n")
        except OSError:
            pass
    return success and restart_started


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--tray-exe", required=True)
    parser.add_argument("--log-path", required=True)
    args = parser.parse_args()
    install_and_restart(args.version, args.parent_pid, args.tray_exe, args.log_path)


if __name__ == "__main__":
    main()
