"""CAPHY Phase 6 - Storage Manager / cloud sync.  Run:  python run_sync.py

Uploads snapshots/videos of any alert not yet synced. Works offline (queues
them) and auto-uploads when the internet is back. Press Ctrl+C to stop.

For now it copies into a local 'cloud_sim' folder so you can see it working.
Real Firebase Storage swaps in later (Phase 7 setup).
"""
import config
from storage.sync import SyncManager

if __name__ == "__main__":
    sm = SyncManager(config.DB_PATH, config.CLOUD_DIR,
                     config.COMPRESSED_DIR, config.IMAGE_QUALITY)
    try:
        sm.run(config.SYNC_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\n[CAPHY] Sync stopped.")