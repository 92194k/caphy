"""Battery monitoring + power-save.

Reads the laptop battery. When running on battery (likely a brownout), CAPHY
switches to POWER-SAVE mode to conserve so it keeps guarding for longer.
"""


class PowerMonitor:
    def __init__(self, low_threshold=20):
        self.low_threshold = low_threshold

    def status(self):
        b = None
        try:
            import psutil
            b = psutil.sensors_battery()
        except Exception:
            b = None
        if b is None:                       # desktop / no battery sensor
            return {"percent": None, "plugged": True, "on_battery": False,
                    "low": False, "power_save": False}
        on_bat = not b.power_plugged
        return {"percent": int(b.percent), "plugged": bool(b.power_plugged),
                "on_battery": on_bat, "low": b.percent <= self.low_threshold,
                "power_save": on_bat}       # on battery -> conserve