"""
desktop_launcher.py — CAPHY Desktop Application launcher.

Wraps the Flask app.py into a native window using pywebview. This is the
"double-click and it works" entry point for non-technical users.

The Flask backend runs in a background thread; pywebview opens a native
window pointing to http://127.0.0.1:5000.

On first run, device identity (device_id + device_secret) is generated and
persisted to %APPDATA%\\CAPHY\\device.json. This launcher can later be extended
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
    # The PyPI package is named "pywebview" but the importable module is
    # "webview" - installing pywebview and then writing `import pywebview`
    # is a common mixup that fails with ModuleNotFoundError even though
    # pip reports the install succeeded.
    import webview
except ImportError:
    print("ERROR: pywebview not installed. Run: pip install pywebview")
    sys.exit(1)

from identity import get_device_identity
from web.server import app, start_workers, resolve_cameras, start_auto_arm_scheduler


def run_flask():
    """Run Flask in a background thread (non-blocking for the UI).

    start_workers() now also starts all the cloud services (registration,
    heartbeat, remote-command + pairing listener, WebRTC) internally - see
    web.server.start_cloud_services() - so this launcher no longer has to
    (and must not, to avoid double-starting) manage those threads itself.
    That centralization is deliberate: it's exactly what makes the laptop
    reachable over the internet even when CAPHY is started via app.py
    instead of this launcher."""
    start_workers(resolve_cameras())
    start_auto_arm_scheduler()
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False, use_reloader=False)


# NOTE: the cloud registration / heartbeat / remote-command / pairing /
# WebRTC threads used to live here. They now live in web.server
# (start_cloud_services, called from start_workers) so they run for EVERY
# entry point - app.py included - not just this launcher. Keeping them in
# one place also removes the risk of the two copies drifting (an earlier
# copy here posted the wrong arm-command body, {"armed": ...} instead of
# the {"on": ...} the /api/arm route actually expects).


def run_retention_cleanup():
    """
    Background loop: once a day, delete alerts (local files + Firebase
    copies) older than config.RETENTION_DAYS. Runs silently - no terminal
    window, no user action needed. Same tools/run_cleanup.py logic, just
    called as a function instead of a separate script.

    Runs one sweep shortly after startup (covers the case where the app
    wasn't open at all yesterday), then every 24 hours after that.
    """
    from tools.run_cleanup import cleanup_once

    # Small delay so this doesn't compete with camera/model startup.
    time.sleep(15)

    while True:
        try:
            cleanup_once()
        except Exception as e:
            # A cleanup failure should never take detection down.
            print(f"[CAPHY Desktop] Retention cleanup error (skipped): {e}")
        time.sleep(24 * 60 * 60)


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

    # Start retention cleanup in background (silent, once a day - see
    # run_retention_cleanup() docstring). Daemon thread so it never blocks
    # the app from closing.
    cleanup_thread = threading.Thread(target=run_retention_cleanup, daemon=True)
    cleanup_thread.start()

    # Cloud registration, heartbeat, remote-command / pairing listener and
    # WebRTC are all started inside run_flask() -> start_workers() ->
    # start_cloud_services() now, so there's nothing extra to launch here.

    # Give Flask a moment to bind the port.
    time.sleep(2)

    # Create native window.
    print("[CAPHY Desktop] Opening window...")
    window = webview.create_window(
        title="CAPHY Security Console",
        url="http://127.0.0.1:5000",
        width=1200,
        height=800,
        resizable=True,
    )
    window.events.closed += on_window_close

    # debug=True opens DevTools inside the native window itself (right-click
    # -> Inspect, or it may open automatically depending on the platform's
    # WebView backend) - this is how to see real JS errors happening
    # inside the desktop app window, since pywebview otherwise swallows
    # them silently. Set back to False once everything works, so end
    # users never see a DevTools option.
    webview.start(debug=True)
