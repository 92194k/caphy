"""CAPHY Desktop Security Console - entry point.

Run modes:
    python app.py            -> auto-scan and use whatever cameras are connected
    python app.py 0          -> LAPTOP only  (camera index 0)
    python app.py 1          -> PHONE only   (DroidCam index - check with list_cameras.py)
    python app.py 0 1        -> BOTH at once (CCTV grid)

Then open http://127.0.0.1:5000 and log in (admin / admin).

Voice lives ONLY in the phone app. The app does speech-to-text on the device,
POSTs the words to /api/assistant, and speaks the reply with its own text-to-speech.
This PC never listens and never talks - no microphone, no Vosk, no TTS.
"""
import sys

# Chdir into the same stable, per-user writable folder every other CAPHY
# entry point uses (%LOCALAPPDATA%\CAPHY), BEFORE importing anything that
# reads config-derived paths at import time (web.server imports config,
# which computes DB_PATH/YOLO_MODEL/FIREBASE_KEY/the Flask session secret
# key as plain relative strings resolved against the current working
# directory). Without this, "python app.py" used yet a THIRD working
# directory (wherever it happened to be launched from) - its own separate
# database, and its own separate Flask secret key regenerated every run,
# on top of the desktop_launcher.py/caphy_desktop.py divergence already
# fixed. All three entry points must agree, or "the web system thru VS
# Code must work like the desktop app" (and vice versa) keeps breaking in
# a new place every time.
from resource_path import use_writable_workdir
use_writable_workdir()

from identity import get_device_identity
from web.server import (app, start_workers, resolve_cameras,
                        start_auto_arm_scheduler, start_cloud_sync_retry,
                        start_lan_discovery_beacon)


def parse_args(argv):
    cams = []
    for a in argv:
        cams.append(int(a) if a.isdigit() else a)   # number = device, else a URL
    return cams


if __name__ == "__main__":
    # Load device identity (generated once, persisted forever).
    device = get_device_identity()
    print(f"[CAPHY] Device ID: {device['device_id']}")
    print(f"[CAPHY] Hostname: {device['hostname']}")

    # strip flags so they are never mistaken for a camera index / URL
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        cameras = parse_args(args)
        print(f"[CAPHY] Cameras from command line: {cameras}")
    else:
        cameras = resolve_cameras()
    start_workers(cameras)
    start_auto_arm_scheduler()
    start_cloud_sync_retry()
    start_lan_discovery_beacon()
    # host="0.0.0.0" makes the console reachable from the phone over the LAN,
    # not just from this PC. Open http://<this-PC-IP>:5000 on the phone.
    print("[CAPHY] Console: http://127.0.0.1:5000 (this PC) or http://<your-LAN-IP>:5000 (phone)")
    print("[CAPHY] login: admin / admin   Ctrl+C to stop")
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)