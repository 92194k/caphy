"""The two-factor gate - the defensible core of CAPHY.

    Motion?  --NO-->  ignore
      |YES
    Run YOLO  (only every Nth frame for speed - boxes reused in between)
      |
    Person?  --NO-->  ignore (empty movement / shadow)
      |YES
    THREAT  --> classify into Tier 1 / 2 / 3 by distance

Factor 1 (cheap) gates Factor 2 (expensive). To cut lag, YOLO runs every Nth
frame while a person is present and the last boxes are reused on the frames in
between - this roughly doubles the frame rate at N=2 with no loss of coverage.
"""


class TwoFactorDetector:
    def __init__(self, motion_detector, person_detector=None, tier_engine=None, person_every_n=1):
        self.motion = motion_detector
        self.person = person_detector
        self.tier = tier_engine
        self.person_every_n = max(1, int(person_every_n))
        self._frame_i = 0
        self._last_persons = []

    def process(self, frame):
        result = {"motion": False, "motion_area": 0.0, "ran_yolo": False,
                  "persons": [], "threat": False, "tier": 0, "mask": None}

        moved, area, mask = self.motion.detect(frame)
        result["motion"] = moved
        result["motion_area"] = area
        result["mask"] = mask

        if not moved:
            self._last_persons = []          # threat cleared
            return result
        if self.person is None:
            return result

        # frame-skip: run YOLO every Nth frame, reuse the last boxes otherwise
        self._frame_i += 1
        if self._frame_i % self.person_every_n == 0 or not self._last_persons:
            persons = self.person.detect(frame)
            if self.tier is not None:
                for p in persons:
                    p.update(self.tier.classify(p["box"]))
            self._last_persons = persons
        else:
            persons = self._last_persons

        result["ran_yolo"] = True
        result["persons"] = persons
        result["threat"] = len(persons) > 0
        if result["threat"] and self.tier is not None:
            result["tier"] = max(p["tier"] for p in persons)
        return result