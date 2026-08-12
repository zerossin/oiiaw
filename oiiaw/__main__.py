import argparse
import asyncio
import os
import sys

from .config import Config
from .logger import Logger
from .sync_engine import SyncEngine


def cmd_run(args):
    config = Config.load(args.config)
    logger = Logger(config.logs_dir, config.console_level, config.log_retention)

    for problem in config.validate():
        if problem.blocking:
            logger.error("CONFIG", problem.message, level="important")
            sys.exit(1)
        logger.warn("CONFIG", problem.message, level="important")

    for path in (config.local_vault, config.cloud_vault, config.sync_baseline, config.logs_dir):
        if path:
            os.makedirs(path, exist_ok=True)

    logger.info("START", f"local={config.local_vault} cloud={config.cloud_vault}", level="important")
    engine = SyncEngine(config, logger)
    asyncio.run(engine.run())


def cmd_status(args):
    config = Config.load(args.config)
    problems = config.validate()
    if not problems:
        print("config: ok")
    for problem in problems:
        tag = "blocking" if problem.blocking else "warning"
        print(f"config [{tag}]: {problem.message}")

    for name, path in (("local_vault", config.local_vault), ("cloud_vault", config.cloud_vault), ("sync_baseline", config.sync_baseline)):
        count = sum(len(files) for _, _, files in os.walk(path)) if path and os.path.isdir(path) else 0
        print(f"{name}: {count} files ({path or 'not set'})")


def main():
    parser = argparse.ArgumentParser(prog="oiiaw", description="Obsidian <-> iCloud sync bridge for Windows")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config YAML (default: config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the sync daemon").set_defaults(func=cmd_run)
    sub.add_parser("status", help="check config and vault file counts").set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
