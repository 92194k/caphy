"""
caphy_desktop.py — packaged desktop entry point for CAPHY (the .exe).

What it does, in order:
  1. Picks a stable, writable folder for runtime files (caphy.db, logs, prefs)
     so the app works no matter where the .exe is installed - including
     read-only locations like Program Files.
  2. Starts the full CAPHY backend (cameras, detection, cloud services, Flask).
  3. Opens the console: it TRIES a native desktop window (pywebview); if that
     backend isn't available on this machine, it falls back to opening the
     default web browser. Either way the user gets a working app.

Build into an .exe with:  pyinstaller CAPHY.spec   (see BUILD_EXE.md)
"""

import os
import sys
import threading
import time


def _use_writable_workdir():
    """Run from a per-user writable folder so caphy.db / caphy.log / prefs
    can always be created, even if the .exe lives in Program Files."""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        workdir = os.path.join(base, "CAPHY")
        os.makedirs(workdir, exist_ok=True)
        os.chdir(workdir)
    except Exception as e:
        print(f"[CAPHY] Could not set writable workdir: {e}")


def _open_console_window(url):
    """Try a native window; fall back to the default browser."""
    # 1) native window (nice, app-like) - but only if the backend loads.
    try:
        import webview  # pywebview
        webview.create_window("CAPHY Security Console", url,
                              width=1200, height=800, resizable=True)
        webview.start()
        return  # window closed -> exit
    except Exception as e:
        print(f"[CAPHY] Native window unavailable ({e}); opening in browser.")

    # 2) browser fallback (always works).
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:
        print(f"[CAPHY] Could not open a browser automatically: {e}")
        print(f"[CAPHY] Open this address manually:  {url}")

    # Keep the process (and the background server threads) alive.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    if getattr(sys, "frozen", False):
        _use_writable_workdir()

    # Import AFTER chdir so any import-time paths resolve against the workdir.
    from web.server import (app, start_workers, resolve_cameras,
                            start_auto_arm_scheduler, start_lan_discovery_beacon)

    from identity import get_device_identity
    dev = get_device_identity()
    print(f"[CAPHY] Device ID: {dev['device_id']}")

    def run_server():
        start_workers(resolve_cameras())      # also starts cloud services
        start_auto_arm_scheduler()
        start_lan_discovery_beacon()
        # NOTE: host="127.0.0.1" only accepts connections from THIS machine
        # - the phone cannot reach this launcher's Flask server over LAN at
        # all (compare app.py, which correctly uses host="0.0.0.0"). Same
        # pre-existing limitation as desktop_launcher.py - flagging it here
        # rather than silently leaving local/offline mode broken for
        # whoever runs the packaged/frozen build via this entry point.
        # threaded so the UI (window/browser) and the server run together;
        # use_reloader off so it doesn't try to spawn a second frozen process.
        app.run(host="127.0.0.1", port=5000, threaded=True,
                debug=False, use_reloader=False)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # give Flask a moment to bind the port before we point the UI at it
    time.sleep(2.0)
    _open_console_window("http://127.0.0.1:5000")


if __name__ == "__main__":
    main()
