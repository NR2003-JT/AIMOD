"""AIMOD branding: the logo and icon files in art/brand.

In the built AIMOD.exe they are bundled by build/release/AIMOD.spec and unpacked under sys._MEIPASS;
from source they are read from the repo's art/brand folder.
"""
import os
import sys


def brand_dir() -> str:
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "art", "brand")


def brand_file(name: str) -> str | None:
    """Full path to art/brand/<name>, or None if it isn't there (the app still runs without it)."""
    path = os.path.join(brand_dir(), name)
    return path if os.path.isfile(path) else None


def set_window_icon(root) -> None:
    """Give the Tk root (and every Toplevel opened from it) the AIMOD icon."""
    try:
        ico = brand_file("aimod.ico")
        if ico and sys.platform == "win32":
            root.iconbitmap(default=ico)       # multi-size: crisp in the title bar and the taskbar
            return
        png = brand_file("aimod_icon.png")
        if png:
            import tkinter as tk
            root._aimod_icon = tk.PhotoImage(file=png)   # keep a reference or Tk drops the image
            root.iconphoto(True, root._aimod_icon)
    except Exception:
        pass                                   # an icon is never worth failing start-up over
