# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""CAPHY - 2-camera grid (simultaneous view).

Runs the full two-factor detection on TWO cameras at the same time and shows
them side by side, each with its own bounding boxes and tier.

    python multicam.py                                  # cameras 0 and 1
    python multicam.py --cams 0 1
    python multicam.py --cams 0 "http://192.168.1.5:8080/video"   # phone via IP Webcam

Press q to quit.
"""
import argparse
import cv2
import numpy as np
import config
from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine
from detection.night_vision import NightVision

GREEN = (80, 200, 120)
AMBER = (60, 160, 240)
RED = (60, 60, 230)
GREY = (150, 150, 150)
WHITE = (240, 240, 240)
TIER_COLOR = {1: GREEN, 2: AMBER, 3: RED}


def open_cap(src):
    return cv2.VideoCapture(int(src) if str(src).isdigit() else src)


def make_gate(shared_person):
    """One detection gate per camera (motion state is per-camera)."""
    motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY,
                            config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
    tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST, config.TIER3_MAX_DIST)
    nv = NightVision(config.CLAHE_CLIP, config.CLAHE_TILE, config.NIGHT_LOW_LIGHT,
                     config.NIGHT_GAMMA, config.NIGHT_VISION_AUTO)
    gate = TwoFactorDetector(motion, shared_person, tier, getattr(config, "PERSON_EVERY_N", 1))
    return gate, nv


def annotate(frame, result, name):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 30), (25, 25, 25), -1)
    f1 = "MOTION" if result["motion"] else "no motion"
    f2 = ("PERSON" if result["persons"] else "no person") if result["ran_yolo"] else "idle"
    cv2.putText(frame, f"{name}  F1:{f1}  F2:{f2}", (8, 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, WHITE, 1)
    if result["tier"] > 0:
        cv2.putText(frame, f"TIER {result['tier']}", (w - 90, 21),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, TIER_COLOR[result["tier"]], 2)
    for p in result["persons"]:
        x1, y1, x2, y2 = p["box"]
        c = TIER_COLOR.get(p.get("tier", 2), GREEN)
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        lbl = f"{p['conf']:.2f} {p.get('distance_m','?')}m"
        cv2.putText(frame, lbl, (x1 + 3, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 2)
    if result["threat"]:
        cv2.rectangle(frame, (0, h - 26), (w, h), TIER_COLOR[result["tier"]], -1)
        cv2.putText(frame, f"THREAT - Tier {result['tier']}", (8, h - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, WHITE, 2)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", nargs=2, default=["0", "1"],
                    help="two camera sources (index or URL)")
    args = ap.parse_args()

    from detection.person_detector import PersonDetector
    person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
    print("[CAPHY] YOLO loaded - two-camera mode.")

    cap0 = open_cap(args.cams[0])
    cap1 = open_cap(args.cams[1])
    gate0, nv0 = make_gate(person)
    gate1, nv1 = make_gate(person)
    H = 360   # display height per camera

    def blank(text):
        img = np.full((H, int(H * 1.33), 3), 18, np.uint8)
        cv2.putText(img, text, (30, H // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (110, 110, 110), 2)
        return img

    print("[CAPHY] Running 2-camera grid. Press 'q' to quit.")
    while True:
        ok0, f0 = cap0.read()
        ok1, f1 = cap1.read()
        if ok0:
            f0, _ = nv0.process(f0)
            annotate(f0, gate0.process(f0), "CAM 0")
            f0 = cv2.resize(f0, (int(H * f0.shape[1] / f0.shape[0]), H))
        else:
            f0 = blank("CAM 0 offline")
        if ok1:
            f1, _ = nv1.process(f1)
            annotate(f1, gate1.process(f1), "CAM 1")
            f1 = cv2.resize(f1, (int(H * f1.shape[1] / f1.shape[0]), H))
        else:
            f1 = blank("CAM 1 offline")

        grid = cv2.hconcat([f0, f1])
        cv2.imshow("CAPHY - 2 Camera Grid", grid)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap0.release()
    cap1.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()