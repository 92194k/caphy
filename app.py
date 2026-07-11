"""CAPHY Desktop Dashboard - entry point.

Starts the camera/detection worker and serves the web dashboard.
Open http://127.0.0.1:5000 in your browser after running this.

    python app.py
"""
import config
from web.server import app, start_camera

if __name__ == "__main__":
    start_camera(config.CAMERA_INDEX)
    print("[CAPHY] Dashboard at http://127.0.0.1:5000  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)