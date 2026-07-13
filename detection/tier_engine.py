"""Threat Level Engine - bounding-box height -> distance -> tier."""

TIER_ACTIONS = {
    1: ["snapshot", "alert"],
    2: ["snapshot", "record", "alert"],
    3: ["snapshot", "record", "siren", "alert"],
}
TIER_LABEL = {1: "Tier 1 - Far", 2: "Tier 2 - Medium", 3: "Tier 3 - Close"}


class TierEngine:
    def __init__(self, distance_k, tier1_min_distance, tier3_max_distance):
        self.k = distance_k
        self.tier1_min = tier1_min_distance
        self.tier3_max = tier3_max_distance

    def estimate_distance(self, box):
        x1, y1, x2, y2 = box
        h = max(y2 - y1, 1)
        return self.k / h

    def classify(self, box):
        dist = self.estimate_distance(box)
        if dist >= self.tier1_min:
            tier = 1
        elif dist <= self.tier3_max:
            tier = 3
        else:
            tier = 2
        return {"tier": tier, "distance_m": round(dist, 1),
                "actions": TIER_ACTIONS[tier], "label": TIER_LABEL[tier]}