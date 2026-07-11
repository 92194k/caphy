"""Phase 3 - Threat Level Engine.

Turns a confirmed person's bounding-box height into a distance estimate and a
threat tier. The farther away a person is, the shorter they appear, so distance
is inversely proportional to box height:

    distance_m  ~=  DISTANCE_K / box_height_pixels

DISTANCE_K is a calibration constant. Tune it once in real daylight: stand at a
known distance, read the box height printed on screen, and solve for K. Until
then the default gives sensible relative tiers.
"""

# What each tier does. Actual saving/siren is wired in later phases;
# here we just decide the tier and list the intended actions.
TIER_ACTIONS = {
    1: ["log"],
    2: ["snapshot", "alert"],
    3: ["snapshot", "video", "siren"],
}
TIER_LABEL = {1: "Tier 1 - Far", 2: "Tier 2 - Medium", 3: "Tier 3 - Close"}


class TierEngine:
    def __init__(self, distance_k, tier1_min_distance, tier3_max_distance):
        self.k = distance_k
        self.tier1_min = tier1_min_distance   # farther than this -> Tier 1
        self.tier3_max = tier3_max_distance   # closer than this  -> Tier 3

    def estimate_distance(self, box):
        x1, y1, x2, y2 = box
        h = max(y2 - y1, 1)          # box height in pixels (avoid divide by zero)
        return self.k / h

    def classify(self, box):
        dist = self.estimate_distance(box)
        if dist >= self.tier1_min:
            tier = 1
        elif dist <= self.tier3_max:
            tier = 3
        else:
            tier = 2
        return {
            "tier": tier,
            "distance_m": round(dist, 1),
            "actions": TIER_ACTIONS[tier],
            "label": TIER_LABEL[tier],
        }