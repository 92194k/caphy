# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""CAPHY retention cleanup.  Run:  python tools/run_cleanup.py

Deletes alerts (and their snapshot/video files, local AND Firebase cloud
copy) older than config.RETENTION_DAYS. Same rule for every tier - Tier 3
gets no special treatment, per the retention policy.

Runs once and exits (call it from Task Scheduler / cron for a daily sweep),
or pass --loop to run continuously like run_sync.py does.

    python tools/run_cleanup.py            # one pass, then exit
    python tools/run_cleanup.py --loop      # repeat every 24h until Ctrl+C
"""
import os
import sys
import time

import config
from storage.database import Database


def _delete_local_file(path):
    """Best-effort local delete - a missing/already-gone file is not an error."""
    if not path:
        return False
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as e:
        print(f"[Cleanup] Could not delete local file {path}: {e}")
    return False


def _cloud_deleter():
    """
    Returns a fn(user_uid, device_id, filename) -> None that deletes the
    matching file from Firebase Storage, or a no-op if Firebase isn't
    configured (keeps this script runnable before Firebase setup is done).
    """
    have_firebase = bool(getattr(config, "FIREBASE_BUCKET", None)) and \
                    os.path.exists(getattr(config, "FIREBASE_KEY_PATH", "firebase_key.json"))

    if not have_firebase:
        def _noop(user_uid, device_id, filename):
            pass
        return _noop

    try:
        from storage.firebase_uploader import FirebaseUploader
        cache = {}

        def _delete(user_uid, device_id, filename):
            if not user_uid or not device_id:
                return   # never uploaded - nothing to delete
            key = (user_uid, device_id)
            if key not in cache:
                cache[key] = FirebaseUploader(user_uid, device_id)
            cache[key].delete_file(filename)

        return _delete
    except Exception as e:
        print(f"[Cleanup] Firebase uploader unavailable ({e}); skipping cloud deletes.")
        def _noop(user_uid, device_id, filename):
            pass
        return _noop


def cleanup_once():
    """One retention sweep. Returns the number of alerts deleted."""
    if not getattr(config, "RETENTION_ENABLED", True):
        print("[Cleanup] RETENTION_ENABLED is False - skipping.")
        return 0

    days = getattr(config, "RETENTION_DAYS", 30)
    db = Database(config.DB_PATH)
    expired = db.expired_alerts(days)

    if not expired:
        db.close()
        print(f"[Cleanup] Nothing older than {days} days. Nothing to delete.")
        return 0

    delete_cloud = _cloud_deleter()
    count = 0

    for a in expired:
        for key in ("snapshot_path", "video_path"):
            p = a.get(key)
            if not p:
                continue
            _delete_local_file(p)
            delete_cloud(a.get("user_uid"), a.get("device_id"), os.path.basename(p))

        db.delete_alert(a["alert_id"])
        count += 1

    db.close()
    print(f"[Cleanup] Deleted {count} alert(s) older than {days} days "
          f"(local files + cloud copies).")
    return count


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print("[CAPHY] Retention cleanup running once every 24h. Ctrl+C to stop.")
        try:
            while True:
                cleanup_once()
                time.sleep(24 * 60 * 60)
        except KeyboardInterrupt:
            print("\n[CAPHY] Cleanup stopped.")
    else:
        cleanup_once()
