"""Shared application artwork for the tray icon and Tk windows."""

import ctypes
import sys
import tkinter as tk
from functools import lru_cache
from importlib.resources import files

from PIL import Image, ImageDraw

WINDOWS_APP_ID = "zerossin.oiiaw"


def face_asset_path():
    return files("oiiaw").joinpath("assets", "oiia-face.png")


def face_ico_asset_path():
    return files("oiiaw").joinpath("assets", "oiia-face.ico")


def configure_windows_app_identity():
    """Keep taskbar grouping and its icon independent from pythonw.exe."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):
        pass


@lru_cache(maxsize=8)
def tray_icon(color: tuple[int, int, int], size: int = 64) -> Image.Image:
    """The face stays recognizable while the badge preserves sync state."""
    with face_asset_path().open("rb") as source:
        face = Image.open(source).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)

    badge_size = max(16, round(size * 0.36))
    inset = max(1, round(size * 0.03))
    left = size - badge_size - inset
    top = size - badge_size - inset
    draw = ImageDraw.Draw(face)
    draw.ellipse((left - 2, top - 2, size - inset + 2, size - inset + 2), fill=(255, 255, 255, 255))
    draw.ellipse((left, top, size - inset, size - inset), fill=(*color, 255))
    return face


def apply_window_icon(window: tk.Misc):
    """Set both Tk's image and Windows' native taskbar/title-bar icon."""
    try:
        icon = tk.PhotoImage(file=str(face_asset_path()))
        window.iconphoto(True, icon)
        window._oiiaw_icon = icon
    except (OSError, tk.TclError):
        # An icon failure should never prevent setup or status UI from opening.
        pass
    if sys.platform == "win32":
        try:
            # Apply to this HWND explicitly. Tk's ``default=`` form only
            # changes the fallback for other toplevels and Windows may keep
            # showing pythonw.exe for the already-created taskbar button.
            window.iconbitmap(str(face_ico_asset_path()))
        except (OSError, tk.TclError):
            pass
