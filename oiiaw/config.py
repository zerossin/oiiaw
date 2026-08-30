import os
import glob
import yaml
from dataclasses import dataclass


def discover_icloud_vault() -> str | None:
    """Finds the Obsidian iCloud container under the user's iCloudDrive folder."""
    home = os.path.expanduser("~")
    candidates = glob.glob(os.path.join(home, "iCloudDrive", "iCloud~md~obsidian"))
    return candidates[0] if candidates else None


def paths_overlap(first: str, second: str) -> bool:
    """True when either configured vault contains the other."""
    try:
        first = os.path.normcase(os.path.abspath(first))
        second = os.path.normcase(os.path.abspath(second))
        common = os.path.normcase(os.path.commonpath((first, second)))
    except ValueError:
        return False
    return common in (first, second)


@dataclass(frozen=True)
class ConfigProblem:
    message: str
    blocking: bool  # True: can't start; False: worth a warning but startable


class Config:
    def __init__(self, data: dict):
        paths = data.get("paths", {})
        self.local_vault = paths.get("local_vault", "")
        self.cloud_vault = paths.get("cloud_vault") or discover_icloud_vault() or ""
        self.sync_baseline = paths.get("sync_baseline", "")
        self.logs_dir = paths.get("logs_dir", "")

        sync = data.get("sync", {})
        self.stability_window = sync.get("stability_window", 3)
        self.stabilize_wait = sync.get("stabilize_wait", 8)
        self.cooldown_seconds = sync.get("cooldown_seconds", 3)
        self.cloud_probe_timeout = sync.get("cloud_probe_timeout", 5)
        self.cloud_hydrate_timeout = sync.get("cloud_hydrate_timeout", 30)
        self.hydrate_retry_initial = sync.get("hydrate_retry_initial", 15)
        self.hydrate_retry_max = sync.get("hydrate_retry_max", 300)
        self.error_retry_initial = sync.get("error_retry_initial", 5)
        self.error_retry_max = sync.get("error_retry_max", 300)
        self.cloud_confirm_retry = sync.get("cloud_confirm_retry", 5)
        self.delete_grace_seconds = sync.get("delete_grace_seconds", 30)
        self.delete_batch_limit = sync.get("delete_batch_limit", 20)
        self.delete_batch_window = sync.get("delete_batch_window", 60)
        self.protect_nonempty_from_zero = sync.get("protect_nonempty_from_zero", True)
        self.big_file_threshold = sync.get("big_file_threshold", 102400)
        self.big_file_cooldown = sync.get("big_file_cooldown", 30)
        logging_cfg = data.get("logging", {})
        self.console_level = logging_cfg.get("console_level", "normal")
        self.log_retention = logging_cfg.get("log_retention", 10)

        updates = data.get("updates", {})
        self.auto_update = updates.get("enabled", True)
        self.update_check_interval = updates.get("check_interval", 86400)

        ignore = data.get("ignore", {})
        self.ignored_dirs = {d.lower() for d in ignore.get("dirs", [])}
        self.ignored_files = {f.lower() for f in ignore.get("files", [])}
        self.ignore_patterns = ignore.get("patterns", [])

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    def validate(self) -> list[ConfigProblem]:
        problems = []
        if not self.local_vault:
            problems.append(ConfigProblem("paths.local_vault is not set", blocking=True))
        if not self.cloud_vault:
            problems.append(ConfigProblem("paths.cloud_vault is not set and could not be auto-discovered", blocking=True))
        if self.local_vault and self.cloud_vault and paths_overlap(self.local_vault, self.cloud_vault):
            problems.append(ConfigProblem("local_vault and cloud_vault must not contain each other", blocking=True))
        if self.local_vault and not os.path.isdir(self.local_vault):
            problems.append(ConfigProblem(f"local_vault does not exist yet: {self.local_vault}", blocking=False))
        if self.cloud_vault and not os.path.isdir(self.cloud_vault):
            problems.append(ConfigProblem(f"cloud_vault is temporarily unavailable: {self.cloud_vault}", blocking=False))
        return problems
