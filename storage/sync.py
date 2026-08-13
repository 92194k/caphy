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
from functools import lru_cache

import cv2

from storage.database import Database


# Cache internet connectivity check for 5 seconds to avoid excessive DNS lookups
@lru_cache(maxsize=1)
def _check_internet_cached(cache_key):
    """Internal cached check - cache_key is current time bucket."""
    return _internet_check_actual()


def _internet_check_actual():
    """Actual internet check without caching.

    IMPORTANT: this must NEVER call socket.setdefaulttimeout(). That call
    sets the timeout for every socket created anywhere in this Python
    process for the rest of its life - not just the one connection below.
    This function used to do exactly that, on every /api/health poll (as
    often as every 1.5s) and every Settings page load, which meant Firebase
    Admin SDK calls, WebRTC signaling, and any other network code running
    concurrently elsewhere in the app silently inherited a 2-second global
    timeout it never asked for and this function never restored. That is
    the most likely explanation for reports of the whole app "freezing" or
    dashboard tabs endlessly spinning specifically after a connectivity
    change - a slow-but-legitimate call elsewhere could get cut off by a
    timeout value that had nothing to do with it. Using socket.settimeout()
    on the individual socket object instead achieves the exact same
    connectivity-check behavior (2s timeout on this probe only) with zero
    effect on anything else running in the process.
    """
    hosts = [
        ("8.8.8.8", 53),      # Google DNS
        ("1.1.1.1", 53),      # Cloudflare DNS
        ("8.8.4.4", 53),      # Google DNS secondary
    ]
    for host, port in hosts:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect((host, port))
            return True
        except OSError:
            continue
    return False


def internet_available(use_cache=True):
    """True if we can reach the internet (tries multiple DNS servers for reliability).

    Args:
        use_cache: If True, uses 5-second cache to avoid excessive DNS checks.
                   Set to False to force immediate check.
    """
    if not use_cache:
        return _internet_check_actual()

    # Cache key based on current 5-second time bucket (so cache expires every 5s)
    cache_key = int(time.time()) // 5
    return _check_internet_cached(cache_key)


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
    def __init__(self, db_path, cloud_dir, compressed_dir, image_quality,
                uploader=None, uploader_factory=None):
        """
        uploader:         a fixed uploader instance used for every alert
                           (fine when the whole box belongs to one account).
        uploader_factory: fn(user_uid, device_id) -> uploader instance, called
                           per-alert. Use this when different alerts in the
                           same database can belong to different accounts
                           (e.g. FirebaseUploader, scoped per user/device).
                           Takes priority over `uploader` when both are set.
        """
        self.db_path = db_path
        self.compressed_dir = compressed_dir
        self.image_quality = image_quality
        self.uploader = uploader or LocalCloudUploader(cloud_dir)
        self.uploader_factory = uploader_factory
        self._uploader_cache = {}

    def pending(self, db):
        return [dict(r) for r in db.conn.execute(
            "SELECT * FROM alerts WHERE synced=0 ORDER BY alert_id").fetchall()]

    def _uploader_for(self, alert):
        """Pick the right uploader for this alert's owner (cached per user+device)."""
        if not self.uploader_factory:
            return self.uploader
        key = (alert.get("user_uid") or "", alert.get("device_id") or "")
        if key not in self._uploader_cache:
            self._uploader_cache[key] = self.uploader_factory(*key)
        return self._uploader_cache[key]

    def sync_once(self):
        """Upload every unsynced alert's media (if online). Returns (count, status)."""
        if not internet_available():
            return 0, "offline"
        db = Database(self.db_path)
        rows = self.pending(db)
        count = 0
        for a in rows:
            uploader = self._uploader_for(a)
            for key in ("snapshot_path", "video_path"):
                p = a[key]
                if not p or not os.path.exists(p):
                    continue
                upload_path = p
                if p.lower().endswith((".jpg", ".jpeg", ".png")):
                    upload_path = compress_image(p, self.image_quality, self.compressed_dir)
                uploader.upload(upload_path)
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