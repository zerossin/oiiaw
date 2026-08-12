import sys
import os
import time
from colorama import Fore, Style, init as colorama_init

# force UTF-8 on stdout regardless of the active console codepage —
# without this, Korean filenames mangle on ko-KR Windows consoles (cp949).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

colorama_init()

LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}


class Logger:
    def __init__(self, logs_dir: str, console_level: str = "normal", retention: int = 10):
        self.logs_dir = logs_dir
        self.console_level = LEVELS.get(console_level, 1)
        self.retention = retention
        self._log_file = None

    def init_log_file(self):
        os.makedirs(self.logs_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.logs_dir, f"oiiaw_{stamp}.log")
        self._log_file = open(path, "a", encoding="utf-8")
        self._cleanup_old_logs()

    def _cleanup_old_logs(self):
        try:
            files = sorted(
                (f for f in os.listdir(self.logs_dir) if f.startswith("oiiaw_") and f.endswith(".log")),
                reverse=True,
            )
            for stale in files[self.retention:]:
                os.remove(os.path.join(self.logs_dir, stale))
        except FileNotFoundError:
            pass

    def _write(self, level: str, tag: str, message: str, color=""):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] {message}"
        if LEVELS.get(level, 1) <= self.console_level:
            print(f"{color}{line}{Style.RESET_ALL}")
        if self._log_file:
            self._log_file.write(line + "\n")

    def info(self, tag, message, level="normal"):
        self._write(level, tag, message)

    def warn(self, tag, message, level="normal"):
        self._write(level, tag, message, Fore.YELLOW)

    def error(self, tag, message, level="normal"):
        self._write(level, tag, message, Fore.RED)

    def success(self, tag, message, level="normal"):
        self._write(level, tag, message, Fore.GREEN)

    def flush(self):
        if self._log_file:
            self._log_file.flush()
