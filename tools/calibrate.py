# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""Distance calibration.

Stand at a KNOWN distance from the camera (e.g. 3 metres), then run:
    python calibrate.py --distance 3.0

It watches your bounding-box height for a few seconds, averages it, and computes
the DISTANCE_K value to paste into config.py so tier distances read in real metres.
(Distance = DISTANCE_K / box_height, so DISTANCE_K = distance x box_height.)
"""
import argparse
import cv2
import config
from detection.person_detector import PersonDetector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--distance", type=float, required=True,
                    help="your real distance from the camera, in metres")
    ap.add_argument("--source", default=str(config.CAMERA_INDEX))
    ap.add_argument("--samples", type=int, default=40)
    args = ap.parse_args()

    person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
    cap = cv2.VideoCapture(int(args.source) if args.source.isdigit() else args.source)
    heights = []
    print(f"[CALIB] Stand still at {args.distance} m. Collecting {args.samples} samples... (q = stop early)")

    while len(heights) < args.samples:
        ok, frame = cap.read()
        if not ok:
            break
        persons = person.detect(frame)
        if persons:
            p = max(persons, key=lambda x: x["box"][3] - x["box"][1])
            x1, y1, x2, y2 = p["box"]
            h = y2 - y1
            heights.append(h)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (80, 200, 120), 2)
            cv2.putText(frame, f"h={h}px  {len(heights)}/{args.samples}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow("CAPHY calibration", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()

    if not heights:
        print("[CALIB] No person detected - make sure you're in view and try again.")
        return
    avg = sum(heights) / len(heights)
    k = args.distance * avg
    print("\n[CALIB] RESULT")
    print(f"  samples: {len(heights)}   avg box height: {avg:.1f} px   distance: {args.distance} m")
    print(f"  DISTANCE_K = {k:.0f}")
    print(f"\n  -> Edit config.py and set:   DISTANCE_K = {k:.0f}")


if __name__ == "__main__":
    main()