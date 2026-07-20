# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""Camera-free check of the detection engine (Phase 2 gate + Phase 3 tiers).
YOLO is replaced by a stub so this runs without ultralytics.

Run:  python self_test.py
"""
import numpy as np

from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine


def gray_frame(v=120):
    """A plain gray 480x640 frame (our 'background')."""
    return np.full((480, 640, 3), v, dtype=np.uint8)


def warm_up(md, n=40):
    """Let MOG2 learn the empty background before we test."""
    for _ in range(n):
        md.detect(gray_frame())


class StubPerson:
    """Fake Factor 2 that returns a person with a chosen bounding box, so we can
    test tier switching without downloading YOLO. Box height decides the tier."""
    def __init__(self, box):
        self.box = box

    def detect(self, frame):
        return [{"box": self.box, "conf": 0.93}]


def moving_frame():
    f = gray_frame()
    f[120:400, 250:430] = 255     # bright object so Factor 1 sees motion
    return f


def run():
    passed = []
    TE = TierEngine(900.0, 7.0, 2.0)   # same numbers as config defaults

    # ---- 1) Detect movement ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    moved, area, _ = md.detect(moving_frame())
    passed.append(("Detect movement", moved and area >= 1500))

    # ---- 2) Detect person -> THREAT ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    e = TwoFactorDetector(md, StubPerson((250, 150, 380, 400)), TE)
    x = e.process(moving_frame())
    passed.append(("Detect person -> THREAT", x["threat"] and len(x["persons"]) == 1))

    # ---- 3) Ignore shadows ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    f = gray_frame(120)
    f[120:400, 250:430] = 80          # gentle darkening = shadow, must be ignored
    moved, area, _ = md.detect(f)
    passed.append(("Ignore shadows", not moved))

    # ---- 4) Far (short box) -> Tier 1 ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    e = TwoFactorDetector(md, StubPerson((0, 0, 60, 100)), TE)   # box height 100
    x = e.process(moving_frame())
    passed.append(("Far box -> Tier 1", x["tier"] == 1))

    # ---- 5) Medium box -> Tier 2 ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    e = TwoFactorDetector(md, StubPerson((0, 0, 60, 300)), TE)   # box height 300
    x = e.process(moving_frame())
    passed.append(("Medium box -> Tier 2", x["tier"] == 2))

    # ---- 6) Close (tall box) -> Tier 3 ----
    md = MotionDetector(1500, 500, 40, 5)
    warm_up(md)
    e = TwoFactorDetector(md, StubPerson((0, 0, 60, 460)), TE)   # box height 460
    x = e.process(moving_frame())
    passed.append(("Close box -> Tier 3", x["tier"] == 3))

    print("\nCAPHY Phase 3 - self test")
    print("-" * 40)
    allok = True
    for name, ok in passed:
        allok = allok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name}")
    print("-" * 40)
    print("RESULT:", "ALL PASSED" if allok else "SOME FAILED")
    return allok


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)