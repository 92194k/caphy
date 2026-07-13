"""CAPHY Desktop Security Console - entry point.
Starts the detection worker and serves the web dashboard.
Open http://127.0.0.1:5000 and log in (admin / admin).

    python app.py
"""
import config
from web.server import app, start_worker

if __name__ == "__main__":
    start_worker(config.CAMERA_INDEX)
    print("[CAPHY] Console at http://127.0.0.1:5000  (login: admin / admin)  Ctrl+C to stop")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)