"""Camera auto-detection and selection for CAPHY.

Scans for local cameras (USB, built-in) only. Network/IP camera scanning
(a V380 at a hardcoded survey-site IP, 161.248.58.9) was removed
2026-08-31 - config.py's own comment already said "V380 Pro removed: it
has no ONVIF/RTSP option, so it cannot stream to CAPHY. Use a phone as
the 2nd camera instead", but this file's scan_network_cameras() kept
trying to connect to that dead IP on every camera-list refresh anyway,
which just wasted a few seconds per call for zero benefit. If IP-camera
support is wanted again later, add a real ONVIF/RTSP discovery flow
rather than restoring a scan hardcoded to one specific site's IP address.
Saves selected camera to camera_config.json for offline use.
"""
import cv2
import json
import os
from datetime import datetime

CONFIG_FILE = "camera_config.json"


def scan_local_cameras():
    """Scan for local cameras (USB, built-in)"""
    cameras = []
    for i in range(10):
        try:
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    cameras.append({
                        "id": i,
                        "name": f"Local Camera {i}",
                        "type": "USB/Built-in",
                        "source": i
                    })
                cap.release()
        except Exception:
            pass
    return cameras


def get_all_cameras():
    """Return all available cameras (local USB/built-in only - see the
    module docstring for why network/IP camera scanning was removed)."""
    local = scan_local_cameras()

    return {
        "local": local,
        "network": [],
        "total": len(local)
    }


def test_camera_connection(camera_source):
    """Test if camera is accessible"""
    try:
        cap = cv2.VideoCapture(camera_source)
        cap.set(cv2.CAP_PROP_CONNECT_TIMEOUT, 2000)

        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            return {"connected": ret, "error": None}
        else:
            return {"connected": False, "error": "Cannot open camera"}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def save_camera_config(camera_source):
    """Save selected camera to config file"""
    config = {
        "selected_camera": camera_source,
        "timestamp": datetime.now().isoformat()
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return config
    except Exception as e:
        return {"error": str(e)}


def load_camera_config():
    """Load saved camera config"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                return config.get('selected_camera')
        except Exception:
            return None
    return None


def get_camera_source():
    """Get camera source - saved config or default"""
    saved = load_camera_config()
    if saved:
        return saved
    # Default fallback to first available
    return 0
