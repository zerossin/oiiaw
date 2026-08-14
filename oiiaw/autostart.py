"""
Registers oiiaw to start at Windows logon via Task Scheduler. Targets the
`oiiaw-tray` GUI entry point (Windows-subsystem exe, no console window) —
not the `oiiaw` console script — since this runs unattended on every login.
"""

import sys
import subprocess
from pathlib import Path

TASK_NAME = "oiiaw"


def _locate_tray_exe() -> str | None:
    python_dir = Path(sys.executable).parent
    for candidate in (python_dir / "oiiaw-tray.exe", python_dir / "Scripts" / "oiiaw-tray.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def register() -> tuple[bool, str]:
    exe = _locate_tray_exe()
    if not exe:
        return False, "oiiaw-tray.exe를 찾을 수 없습니다 — pip install이 정상적으로 끝났는지 확인해주세요."
    result = subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", f'"{exe}"', "/sc", "onlogon", "/rl", "limited", "/f"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "알 수 없는 오류").strip()
        return False, (
            f"자동 시작 등록에 실패했습니다: {reason}\n"
            "일부 PC에서는 관리자 권한이 필요할 수 있어요 — 마법사를 관리자 권한으로 "
            "다시 실행해보세요. (동기화 자체는 계속 쓸 수 있고, 나중에 `oiiaw run`으로 "
            "직접 실행하면 됩니다.)"
        )
    return True, "Windows 시작 시 자동으로 실행되도록 등록했습니다."


def start_now() -> bool:
    exe = _locate_tray_exe()
    if not exe:
        return False
    subprocess.Popen([exe])
    return True
