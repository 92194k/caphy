# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""CAPHY Phase 6/7 - Storage Manager / cloud sync.  Run:  python run_sync.py

Uploads snapshots/videos of any alert not yet synced. Works offline (queues
them) and auto-uploads when the internet is back. Press Ctrl+C to stop.

Uses real Firebase Cloud Storage if firebase_key.json + FIREBASE_BUCKET are
configured (config.py). Falls back to the local 'cloud_sim' folder uploader
if Firebase isn't set up yet, so this script still runs during early testing.
"""
import config
from storage.sync import SyncManager

if __name__ == "__main__":
    uploader_factory = None

    have_firebase = bool(getattr(config, "FIREBASE_BUCKET", None)) and \
                    __import__("os").path.exists(getattr(config, "FIREBASE_KEY_PATH", "firebase_key.json"))

    if have_firebase:
        try:
            from storage.firebase_uploader import FirebaseUploader

            def uploader_factory(user_uid, device_id):
                # Alerts saved before anyone signed in have no owner yet -
                # nothing to upload to until a real account exists.
                if not user_uid or not device_id:
                    return _NullUploader()
                return FirebaseUploader(user_uid, device_id)

            print("[CAPHY] Cloud sync target: Firebase Cloud Storage "
                  f"({config.FIREBASE_BUCKET})")
        except Exception as e:
            print(f"[CAPHY] Firebase uploader unavailable ({e}); "
                  "falling back to local cloud_sim folder.")
            uploader_factory = None
    else:
        print("[CAPHY] Firebase not configured (missing firebase_key.json or "
              "FIREBASE_BUCKET) - using local cloud_sim folder instead.")


    class _NullUploader:
        """Used when an alert has no owner yet - skip, don't crash."""
        def upload(self, path):
            return None


    sm = SyncManager(config.DB_PATH, config.CLOUD_DIR,
                     config.COMPRESSED_DIR, config.IMAGE_QUALITY,
                     uploader_factory=uploader_factory)
    try:
        sm.run(config.SYNC_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\n[CAPHY] Sync stopped.")