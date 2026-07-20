# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""Quick look at what CAPHY has saved.  Run:  python view_db.py"""
import config
from storage.database import Database

db = Database(config.DB_PATH)
print(f"Total alerts: {db.count_alerts()}\n")
print(f"{'ID':<4}{'Tier':<6}{'Dist':<8}{'Conf':<7}{'Timestamp':<21}Snapshot")
print("-" * 70)
for a in db.recent_alerts(50):
    print(f"{a['alert_id']:<4}{a['tier']:<6}{a['distance_m']:<8}{round(a['confidence'],2):<7}"
          f"{a['timestamp']:<21}{a['snapshot_path'] or '-'}")
db.close()