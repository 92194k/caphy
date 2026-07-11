"""Phase 6 - Storage Manager (offline queue + compress + auto cloud sync).

Alerts are saved locally first (Phase 4). This uploads their media to the
"cloud" when internet is available, compressing images on the way. The
`synced` flag in the alerts table is the offline queue: 0 = still to upload.

The default uploader copies into a local folder so you can watch syncing work
before real Firebase is set up - swap in a FirebaseUploader later.
"""
import os
import shutil
import socket
import time

import cv2

from storage.database import Database


def internet_available(host="8.8.8.8", port=53, timeout=2.0):
    """True if we can reach the internet (quick DNS-port check)."""
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except OSError:
        return False


class LocalCloudUploader:
    """Simulated cloud: copies a file into a local folder and returns its path."""
    def __init__(self, cloud_dir):
        self.cloud_dir = cloud_dir
        os.makedirs(cloud_dir, exist_ok=True)

    def upload(self, path):
        dest = os.path.join(self.cloud_dir, os.path.basename(path))
        shutil.copy2(path, dest)
        return dest


def compress_image(path, quality, out_dir):
    """Re-save a snapshot at lower JPEG quality to save bandwidth."""
    img = cv2.imread(path)
    if img is None:
        return path
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(path))
    cv2.imwrite(out, img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return out


class SyncManager:
    def __init__(self, db_path, cloud_dir, compressed_dir, image_quality, uploader=None):
        self.db_path = db_path
        self.compressed_dir = compressed_dir
        self.image_quality = image_quality
        self.uploader = uploader or LocalCloudUploader(cloud_dir)

    def pending(self, db):
        return [dict(r) for r in db.conn.execute(
            "SELECT * FROM alerts WHERE synced=0 ORDER BY alert_id").fetchall()]

    def sync_once(self):
        """Upload every unsynced alert's media (if online). Returns (count, status)."""
        if not internet_available():
            return 0, "offline"
        db = Database(self.db_path)
        rows = self.pending(db)
        count = 0
        for a in rows:
            for key in ("snapshot_path", "video_path"):
                p = a[key]
                if not p or not os.path.exists(p):
                    continue
                upload_path = p
                if p.lower().endswith((".jpg", ".jpeg", ".png")):
                    upload_path = compress_image(p, self.image_quality, self.compressed_dir)
                self.uploader.upload(upload_path)
            db.conn.execute("UPDATE alerts SET synced=1 WHERE alert_id=?", (a["alert_id"],))
            db.conn.commit()
            count += 1
        db.close()
        return count, "online"

    def run(self, interval):
        print(f"[CAPHY] Cloud sync running (every {interval}s). Ctrl+C to stop.")
        while True:
            count, status = self.sync_once()
            if count:
                print(f"[CAPHY] Synced {count} alert(s) to cloud.")
            elif status == "offline":
                print("[CAPHY] Offline - will retry (alerts stay queued locally).")
            time.sleep(interval)