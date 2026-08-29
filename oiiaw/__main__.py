import argparse
import os
import sys
import time

from .config import Config
from .logger import Logger
from .sync_engine import SyncEngine
from .status_file import StatusReporter
from .tray import TrayApp
from .paths import default_config_path
from .ui_assets import apply_window_icon, configure_windows_app_identity


def _show_error(message: str):
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    apply_window_icon(root)
    root.withdraw()
    messagebox.showerror("oiiaw", message)
    root.destroy()


def _run(config_path):
    configure_windows_app_identity()
    config = Config.load(config_path)
    logger = Logger(config.logs_dir, config.console_level, config.log_retention)

    existing = StatusReporter.read(config.logs_dir)
    if StatusReporter.is_fresh(existing, max_age=5.0):
        message = f"oiiaw가 이미 실행 중입니다 (pid {existing['pid']}) — 중복 실행을 막기 위해 새로 시작하지 않습니다."
        logger.error("START", message, level="important")
        _show_error(message)
        sys.exit(1)

    problems = config.validate()
    blocking = [problem for problem in problems if problem.blocking]
    for problem in problems:
        if problem.blocking:
            logger.error("CONFIG", problem.message, level="important")
            continue
        logger.warn("CONFIG", problem.message, level="important")
    if blocking:
        details = "\n".join(f"• {problem.message}" for problem in blocking)
        _show_error(f"설정을 확인해주세요.\n\n{details}\n\n`oiiaw-setup`에서 폴더를 다시 선택할 수 있습니다.")
        sys.exit(1)

    # Never create the cloud path ourselves: a typo or a temporarily missing
    # iCloud mount must not be mistaken for a healthy, synced directory.
    for path in (config.local_vault, config.sync_baseline, config.logs_dir):
        if path:
            os.makedirs(path, exist_ok=True)

    logger.info("START", f"local={config.local_vault} cloud={config.cloud_vault}", level="important")
    engine = SyncEngine(config, logger)
    TrayApp(config, logger, engine).run()


def cmd_run(args):
    _run(args.config)


def cmd_setup(args):
    from .setup_wizard import main as run_wizard
    run_wizard()


def cmd_start(args):
    """Launches the console-less tray (existing config, no re-mapping
    folders) without blocking this terminal — unlike `run`, which stays
    attached. If oiiaw is already running, the launched process's own
    duplicate-instance check handles that."""
    from . import autostart
    if not autostart.start_now():
        print("oiiaw-tray.exe를 찾을 수 없습니다. pip install이 정상적으로 끝났는지 확인하세요.")
        sys.exit(1)
    print("oiiaw를 시작했습니다.")


def main_tray():
    """Entry point for the console-less `oiiaw-tray` GUI script — what
    Task Scheduler launches at logon, and what the setup wizard's
    "start now" offer runs. It has no console to report to, so if setup
    was never completed (no config.yaml yet), fall into the setup wizard
    instead of crashing silently."""
    config_path = default_config_path()
    if not os.path.isfile(config_path):
        from .setup_wizard import main as run_wizard
        run_wizard()
        return
    _run(config_path)


def cmd_status(args):
    config = Config.load(args.config)
    problems = config.validate()
    if not problems:
        print("config: ok")
    for problem in problems:
        tag = "blocking" if problem.blocking else "warning"
        print(f"config [{tag}]: {problem.message}")

    status = StatusReporter.read(config.logs_dir)
    if StatusReporter.is_fresh(status):
        uptime = int(time.time() - status["started_at"])
        print(
            f"daemon: running (pid {status['pid']}, uptime {uptime}s, "
            f"state={status['state']}, pending={status['pending']}, parked={status.get('parked', 0)})"
        )
        last = status.get("last_event")
        if last:
            print(f"  last event: {last['type']} {last['path']} ({int(time.time() - last['time'])}s ago)")
        print(f"  this session: {status['conflict_count']} conflicts, {status['error_count']} errors")
    else:
        print("daemon: not running (or not responding)")

    for name, path in (("local_vault", config.local_vault), ("cloud_vault", config.cloud_vault), ("sync_baseline", config.sync_baseline)):
        count = sum(len(files) for _, _, files in os.walk(path)) if path and os.path.isdir(path) else 0
        print(f"{name}: {count} files ({path or 'not set'})")


def main():
    parser = argparse.ArgumentParser(prog="oiiaw", description="Obsidian <-> iCloud sync bridge for Windows")
    parser.add_argument("-c", "--config", default=default_config_path(), help="path to config YAML (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the sync daemon (blocks this terminal)").set_defaults(func=cmd_run)
    sub.add_parser("start", help="start the tray in the background, using the existing config").set_defaults(func=cmd_start)
    sub.add_parser("status", help="check config and vault file counts").set_defaults(func=cmd_status)
    sub.add_parser("setup", help="run the setup wizard (pick folders, register autostart)").set_defaults(func=cmd_setup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
