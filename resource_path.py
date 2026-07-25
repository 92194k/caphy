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
