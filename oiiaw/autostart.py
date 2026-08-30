"""
Registers oiiaw to start at Windows logon. Task Scheduler is preferred; if
Windows refuses that registration, the current user's Run registry key is a
no-admin fallback. Both target the console-less `oiiaw-tray` GUI entry point.
"""

import sys
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

TASK_NAME = "oiiaw"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "oiiaw"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _locate_tray_exe() -> str | None:
    python_dir = Path(sys.executable).parent
    for candidate in (python_dir / "oiiaw-tray.exe", python_dir / "Scripts" / "oiiaw-tray.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def _register_user_run(exe: str) -> tuple[bool, str]:
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}"')
        return True, "사용자 계정 자동 시작으로 등록했습니다."
    except OSError as exc:
        return False, f"사용자 계정 자동 시작 등록도 실패했습니다: {exc}"


def _remove_user_run() -> tuple[bool, str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        return True, ""
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, f"사용자 계정 자동 시작을 정리하지 못했습니다: {exc}"


def _scheduled_task_command() -> str | None:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/xml"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        root = ET.fromstring(result.stdout.lstrip("\ufeff"))
        command = root.find(".//{*}Command")
        return command.text.strip().strip('"') if command is not None and command.text else None
    except (ET.ParseError, AttributeError):
        return None


def _same_executable(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def register() -> tuple[bool, str]:
    exe = _locate_tray_exe()
    if not exe:
        return False, "oiiaw-tray.exe를 찾을 수 없습니다 — pip install이 정상적으로 끝났는지 확인해주세요."
    result = subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", f'"{exe}"', "/sc", "onlogon", "/rl", "limited", "/f"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        cleaned, detail = _remove_user_run()
        if not cleaned:
            return False, f"작업 스케줄러 등록은 성공했지만 이전 자동 시작을 정리하지 못했습니다.\n{detail}"
        return True, "Windows 시작 시 자동으로 실행되도록 등록했습니다."

    scheduler_reason = (result.stderr or result.stdout or "알 수 없는 오류").strip()
    existing_command = _scheduled_task_command()
    if existing_command and _same_executable(existing_command, exe):
        cleaned, detail = _remove_user_run()
        if not cleaned:
            return False, f"기존 작업 스케줄러는 정상이지만 중복 자동 시작을 정리하지 못했습니다.\n{detail}"
        return True, "기존 Windows 자동 시작 설정을 그대로 사용합니다."
    if existing_command:
        return False, (
            "기존 자동 시작 작업이 다른 실행 파일을 가리키며 권한 때문에 갱신할 수 없습니다.\n"
            f"{scheduler_reason}\n관리자 권한으로 `oiiaw-setup`을 한 번 실행해주세요."
        )

    registered, detail = _register_user_run(exe)
    if registered:
        return True, "작업 스케줄러 대신 " + detail
    return False, f"자동 시작 등록에 실패했습니다: {scheduler_reason}\n{detail}"


def unregister() -> tuple[bool, str]:
    """Remove both supported registrations so setup can safely be rerun."""
    problems = []
    query = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME],
        capture_output=True, text=True,
    )
    if query.returncode == 0:
        deleted = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True,
        )
        if deleted.returncode != 0:
            problems.append((deleted.stderr or deleted.stdout or "작업 스케줄러 삭제 실패").strip())

    removed, detail = _remove_user_run()
    if not removed:
        problems.append(detail)
    if problems:
        return False, "자동 시작을 완전히 해제하지 못했습니다:\n" + "\n".join(problems)
    return True, "Windows 자동 시작을 사용하지 않도록 설정했습니다."


def start_now() -> bool:
    exe = _locate_tray_exe()
    if not exe:
        return False
    try:
        kwargs = {"cwd": os.path.dirname(exe), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe], **kwargs)
        return True
    except OSError:
        return False
