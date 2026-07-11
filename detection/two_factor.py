"""The two-factor gate - the defensible core of CAPHY.

    Motion?  --NO-->  ignore (nothing happened)
      |YES
      v
    Run YOLO
      |
    Person?  --NO-->  ignore (empty movement: wind, pet, shadow that slipped through)
      |YES
      v
    THREAT  --> classify into Tier 1 / 2 / 3 by distance

Factor 1 (cheap) gates Factor 2 (expensive). A shadow or empty movement can
trip motion, but it fails person verification, so it never becomes a threat.
"""


class TwoFactorDetector:
    def __init__(self, motion_detector, person_detector=None, tier_engine=None):
        self.motion = motion_detector
        self.person = person_detector   # may be None if YOLO isn't loaded yet
        self.tier = tier_engine         # may be None to skip tiering

    def process(self, frame):
        """Run the gate on one frame and return a result dict."""
        result = {
            "motion": False,
            "motion_area": 0.0,
            "ran_yolo": False,
            "persons": [],
            "threat": False,
            "tier": 0,          # highest tier seen this frame (0 = none)
            "mask": None,
        }

        moved, area, mask = self.motion.detect(frame)
        result["motion"] = moved
        result["motion_area"] = area
        result["mask"] = mask

        if not moved:
            return result                     # Factor 1 failed -> stop, don't run YOLO

        if self.person is None:
            return result                     # motion-only mode (YOLO not loaded)

        persons = self.person.detect(frame)   # Factor 2 runs only because there was motion

        # Phase 3: give every confirmed person a tier + distance
        if self.tier is not None:
            for p in persons:
                p.update(self.tier.classify(p["box"]))

        result["ran_yolo"] = True
        result["persons"] = persons
        result["threat"] = len(persons) > 0   # motion + person = confirmed threat

        # overall threat level = the closest (highest) tier in view
        if result["threat"] and self.tier is not None:
            result["tier"] = max(p["tier"] for p in persons)

        return result