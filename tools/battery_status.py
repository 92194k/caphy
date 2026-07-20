# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""Quick check of CAPHY's power monitor.  Run:  python battery_status.py"""
import config
from power import PowerMonitor
s = PowerMonitor(config.BATTERY_LOW).status()
print("Battery:", f"{s['percent']}%" if s['percent'] is not None else "no battery sensor")
print("Plugged in:", s["plugged"])
print("Low battery:", s["low"])
print("Power-save mode:", "ON (on battery - conserving)" if s["power_save"] else "OFF (wall power)")