"""
Where oiiaw's own files live when nobody hand-picks a location. A beginner
running the setup wizard shouldn't have to think about a config.yaml path
at all, and Task Scheduler needs a fixed, cwd-independent path to launch
`oiiaw-tray` with — so both use this instead of a relative "config.yaml".
"""

import os


def app_data_dir() -> str:
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, "oiiaw")


def default_config_path() -> str:
    return os.path.join(app_data_dir(), "config.yaml")
