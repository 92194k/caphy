"""Threat Level Engine - bounding-box height -> distance -> tier.

WHY THIS IS NOT A PLAIN IF/ELSE
-------------------------------
A YOLO box jitters a few percent every frame even when the person is standing
perfectly still. Feeding that raw number straight into fixed thresholds makes
the tier flicker - measured at 13 tier changes in 20 frames for a motionless
person standing near the 4.5 m line. Worse, at the Tier 2/3 boundary the
flicker reaches Tier 3, which fires the siren at random.

Two things fix it:

  1. SMOOTHING - the distance is an exponential moving average, so one bad box
     cannot move the tier on its own.
  2. HYSTERESIS - the threshold to move UP a tier is not the same as the one to
     move back DOWN. A person must cross a threshold by a clear margin before
     the tier changes, so a reading sitting exactly on the line stays put.

Call reset() when the person leaves, so the next person starts clean.

ARMED vs UNARMED MODES
----------------------
Unarmed (default):
  - Tier 1: snapshot + notification
  - Tier 2: snapshot + notification + 5-second video
  - Tier 3: snapshot + video until person exits + siren + notification (requires acknowledgment)

Armed (when ARM button clicked):
  - Any tier: snapshot + siren + continuous recording until person exits + HIGH ALERT notification (requires acknowledgment)
"""

# Unarmed mode actions (default behavior)
TIER_ACTIONS_UNARMED = {
    1: ["snapshot", "alert"],
    2: ["snapshot", "record_5sec", "alert"],
    3: ["snapshot", "record", "siren", "alert"],
}

# Armed mode actions (high alert)
TIER_ACTIONS_ARMED = {
    1: ["snapshot", "record", "siren", "alert_armed"],
    2: ["snapshot", "record", "siren", "alert_armed"],
    3: ["snapshot", "record", "siren", "alert_armed"],
}

TIER_LABEL = {1: "Tier 1 - Far", 2: "Tier 2 - Medium", 3: "Tier 3 - Close"}
TIER_LABEL_ARMED = {1: "ARMED - Tier 1", 2: "ARMED - Tier 2", 3: "ARMED - Tier 3"}


class TierEngine:
    def __init__(self, distance_k, tier1_min_distance, tier3_max_distance,
                 smoothing=0.35, hysteresis=0.12, armed=False):
        self.k = distance_k
        self.tier1_min = tier1_min_distance
        self.tier3_max = tier3_max_distance
        # 0..1 - how much a new reading counts. Lower = steadier, slower.
        self.smoothing = smoothing
        # fraction a threshold must be crossed by before the tier changes
        self.hysteresis = hysteresis
        # Armed mode: when True, triggers high-alert response on all tiers
        self.armed = armed

        self._smoothed = None
        self._tier = 0

    def set_armed(self, armed):
        """Update armed status (called when user clicks ARM/DISARM button)."""
        self.armed = armed

    def reset(self):
        """Forget the current person. Call when motion clears."""
        self._smoothed = None
        self._tier = 0

    def estimate_distance(self, box):
        x1, y1, x2, y2 = box
        h = max(y2 - y1, 1)
        return self.k / h

    def _tier_for(self, d):
        """Tier for a smoothed distance, respecting the current tier."""
        h = self.hysteresis
        t1, t3 = self.tier1_min, self.tier3_max
        cur = self._tier

        if cur == 0:                       # first reading - no bias
            return 1 if d >= t1 else (3 if d <= t3 else 2)

        if cur == 1:                       # far: needs a clear move closer
            if d < t1 * (1 - h):
                return 3 if d <= t3 * (1 - h) else 2
            return 1

        if cur == 3:                       # close: needs a clear move away
            if d > t3 * (1 + h):
                return 1 if d >= t1 * (1 + h) else 2
            return 3

        # cur == 2 (medium): needs a clear move either way
        if d >= t1 * (1 + h):
            return 1
        if d <= t3 * (1 - h):
            return 3
        return 2

    def classify(self, box):
        raw = self.estimate_distance(box)

        self._smoothed = raw if self._smoothed is None else (
            self.smoothing * raw + (1 - self.smoothing) * self._smoothed)
        d = self._smoothed

        self._tier = self._tier_for(d)

        # Choose actions based on armed status
        if self.armed:
            actions = TIER_ACTIONS_ARMED[self._tier]
            label = TIER_LABEL_ARMED[self._tier]
        else:
            actions = TIER_ACTIONS_UNARMED[self._tier]
            label = TIER_LABEL[self._tier]

        return {"tier": self._tier,
                "distance_m": round(d, 1),
                "distance_raw_m": round(raw, 1),
                "actions": actions,
                "label": label,
                "armed": self.armed}
