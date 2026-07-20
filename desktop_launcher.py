"""
desktop_launcher.py — CAPHY Desktop Application launcher.

Wraps the Flask app.py into a native window using pywebview. This is the
"double-click and it works" entry point for non-technical users.

The Flask backend runs in a background thread; pywebview opens a native
window pointing to http://127.0.0.1:5000.

On first run, device identity (device_id + device_secret) is generated and
persisted to %APPDATA%\CAPHY\device.json. This launcher can later be extended
to show a pairing screen with QR code, but for now it just boots the app.

Usage:
    python desktop_launcher.py       # launches the app window

PyInstaller packaging:
    pyinstaller --onefile --windowed --name CAPHY desktop_launcher.py
"""

import threading
import time
import sys

try:
    import pywebview
except ImportError:
    print("ERROR: pywebview not installed. Run: pip install pywebview")
    sys.exit(1)

from identity import get_device_identity
from web.server import app, start_workers, resolve_cameras, start_auto_arm_scheduler


def run_flask():
    """Run Flask in a background thread (non-blocking for the UI)."""
    start_workers(resolve_cameras())
    start_auto_arm_scheduler()
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False, use_reloader=False)


def on_window_close(window):
    """Graceful shutdown when the user closes the window."""
    print("[CAPHY] Closing desktop app.")
    # In a real app, clean up workers/threads here if needed.
    sys.exit(0)


if __name__ == "__main__":
    # Load device identity on startup.
    device = get_device_identity()
    print(f"[CAPHY Desktop] Device ID: {device['device_id']}")
    print(f"[CAPHY Desktop] Hostname: {device['hostname']}")
    print(f"[CAPHY Desktop] Starting Flask...")

    # Start Flask in background.
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Give Flask a moment to bind the port.
    time.sleep(2)

    # Create native window.
    print("[CAPHY Desktop] Opening window...")
    window = pywebview.create_window(
        title="CAPHY Security Console",
        url="http://127.0.0.1:5000",
        width=1200,
        height=800,
        resizable=True,
    )
    window.events.closed += on_window_close

    # Start the UI event loop (blocking until window closes).
    pywebview.start(debug=False)
