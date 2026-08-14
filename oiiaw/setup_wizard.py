"""
Beginner-facing installer: the only two decisions that actually need a
human (which folder is the vault, which folder is iCloud's copy of it) get
a folder picker each; everything else (sync_baseline, logs_dir, config.yaml
itself) is placed under AppData automatically. No YAML editing, no command
line beyond launching this wizard once.
"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox

import yaml

from .config import discover_icloud_vault
from .paths import app_data_dir, default_config_path
from . import autostart


def default_local_vault() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "Obsidian")


def build_config(local_vault: str, cloud_vault: str) -> dict:
    data_dir = app_data_dir()
    return {
        "paths": {
            "local_vault": local_vault,
            "cloud_vault": cloud_vault,
            "sync_baseline": os.path.join(data_dir, "baseline"),
            "logs_dir": os.path.join(data_dir, "logs"),
        }
    }


def write_config(data: dict, path: str = None) -> str:
    path = path or default_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
    return path


class SetupWizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("oiiaw 설치")
        self.resizable(False, False)

        self.local_var = tk.StringVar(value=default_local_vault())
        self.cloud_var = tk.StringVar(value=discover_icloud_vault() or "")
        self.autostart_var = tk.BooleanVar(value=True)

        self._build()

    def _build(self):
        pad = {"padx": 12, "pady": 6}

        tk.Label(self, text="Obsidian이 열어볼 로컬 폴더").grid(row=0, column=0, sticky="w", **pad)
        tk.Entry(self, textvariable=self.local_var, width=48).grid(row=1, column=0, **pad)
        tk.Button(self, text="찾아보기", command=self._pick_local).grid(row=1, column=1, **pad)

        tk.Label(self, text="iCloud Drive의 Obsidian 폴더").grid(row=2, column=0, sticky="w", **pad)
        tk.Entry(self, textvariable=self.cloud_var, width=48).grid(row=3, column=0, **pad)
        tk.Button(self, text="찾아보기", command=self._pick_cloud).grid(row=3, column=1, **pad)
        if not self.cloud_var.get():
            tk.Label(
                self,
                text="자동으로 못 찾았어요 — iCloud Drive가 설치돼 있는지 확인하고 직접 선택해주세요.",
                fg="#b23a2e", wraplength=380, justify="left",
            ).grid(row=4, column=0, columnspan=2, sticky="w", **pad)

        tk.Checkbutton(
            self, text="Windows 시작할 때 자동으로 동기화 시작", variable=self.autostart_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        tk.Button(self, text="설치", command=self._install, width=20).grid(row=6, column=0, columnspan=2, pady=16)

    def _pick_local(self):
        path = filedialog.askdirectory(title="로컬 Vault 폴더 선택")
        if path:
            self.local_var.set(path)

    def _pick_cloud(self):
        path = filedialog.askdirectory(title="iCloud의 Obsidian 폴더 선택")
        if path:
            self.cloud_var.set(path)

    def _install(self):
        local_vault = self.local_var.get().strip()
        cloud_vault = self.cloud_var.get().strip()
        if not local_vault or not cloud_vault:
            messagebox.showerror("oiiaw", "두 폴더 모두 선택해주세요.")
            return
        if os.path.normcase(local_vault) == os.path.normcase(cloud_vault):
            messagebox.showerror("oiiaw", "로컬 폴더와 iCloud 폴더는 서로 달라야 합니다.")
            return

        os.makedirs(local_vault, exist_ok=True)
        write_config(build_config(local_vault, cloud_vault))

        message = "설정을 저장했습니다."
        if self.autostart_var.get():
            _, detail = autostart.register()
            message += "\n" + detail

        if messagebox.askyesno("oiiaw", message + "\n\n지금 바로 동기화를 시작할까요?"):
            autostart.start_now()
        self.destroy()


def main():
    SetupWizard().mainloop()


if __name__ == "__main__":
    main()
