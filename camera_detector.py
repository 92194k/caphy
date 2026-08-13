"""Camera auto-detection and selection for CAPHY.

Scans for local cameras (USB, built-in) and network cameras (V380 at 161.248.58.9).
Saves selected camera to camera_config.json for offline use.
"""
import cv2
import json
import os
from datetime import datetime

# V380 IP from survey setup
V380_IP = "161.248.58.9"
V380_URLS = [
    f"rtsp://{V380_IP}:554/stream",
    f"rtsp://{V380_IP}:554/11",
    f"http://{V380_IP}:80/video"
]

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


def scan_network_cameras():
    """Scan for IP cameras (V380, etc)"""
    cameras = []
    for url in V380_URLS:
        try:
            cap = cv2.VideoCapture(url)
            cap.set(cv2.CAP_PROP_CONNECT_TIMEOUT, 2000)

            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    cameras.append({
                        "url": url,
                        "name": f"V380 IP Camera ({V380_IP})",
                        "type": "IP Camera",
                        "source": url
                    })
                    cap.release()
                    break  # Found working V380, stop searching
            cap.release()
        except Exception:
            pass

    return cameras


def get_all_cameras():
    """Return all available cameras"""
    local = scan_local_cameras()
    network = scan_network_cameras()

    return {
        "local": local,
        "network": network,
        "total": len(local) + len(network)
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
