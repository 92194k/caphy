"""
resource_path.py — locate bundled read-only files whether CAPHY is running
from source or packaged into a PyInstaller .exe.

PyInstaller unpacks bundled data files to a temporary folder exposed as
sys._MEIPASS (both for one-file and one-folder builds). From source, the
files sit next to this module (the project root). Use this ONLY for
read-only bundled assets (the YOLO weights, firebase_key.json, templates,
etc.) - NOT for files the app writes at runtime (caphy.db, captures), which
must live in a normal writable folder.
"""

import os
import sys


def resource_path(rel):
    """Absolute path to a bundled resource given its path relative to the
    project root, e.g. resource_path('yolov8n.pt')."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def is_frozen():
    """True when running inside a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


def use_writable_workdir():
    """Chdir into a stable, per-user writable folder (%LOCALAPPDATA%\CAPHY
    on Windows) for runtime files - caphy.db, caphy.log, ui_prefs.json,
    flask_secret_key.txt, captures metadata, etc.

    Called UNCONDITIONALLY by both entry points (the packaged .exe via
    caphy_desktop.py, and the dev-mode script via desktop_launcher.py) -
    not just when frozen. Before this, only the frozen .exe called an
    equivalent of this function, so running from source (e.g. via VS Code)
    used the project folder itself as the working directory instead - two
    different callers of the exact same app ended up reading/writing two
    completely separate caphy.db files (and settings/logs), which looked
    like the .exe had a different alert history / detection settings than
    the dev run, with no clear reason why. Calling this the same way from
    both entry points means there is only ever ONE real caphy.db, wherever
    CAPHY happens to be launched from."""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        workdir = os.path.join(base, "CAPHY")
        os.makedirs(workdir, exist_ok=True)
        os.chdir(workdir)
        return workdir
    except Exception as e:
        print(f"[CAPHY] Could not set writable workdir: {e}")
        return None
