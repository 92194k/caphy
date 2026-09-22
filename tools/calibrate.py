# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""Distance calibration (v2 - multi-point).

WHY MULTIPLE DISTANCES INSTEAD OF ONE
--------------------------------------
CAPHY estimates distance from a single 2D camera using only the person's
bounding-box height in pixels (distance = DISTANCE_K / box_height) - this
is standard monocular distance estimation (see Liang, Ma & Zhang 2022,
cited in CAPHY's own literature review), and it fundamentally requires at
least one real-world reference measurement: no amount of software alone
can replace physically knowing "this many pixels tall = this many real
metres away" for THIS camera's actual lens and mounting angle.

The original single-shot calibrate.py took exactly one such measurement.
That's fast, but fragile - a single sample inherits whatever error
happened at that one moment (a slightly wrong tape-measure reading, bad
lighting causing a slightly-off bounding box, standing not quite upright,
etc), and that one error becomes baked into every future distance/tier
reading with no way to tell it happened.

This version takes several READINGS AT DIFFERENT KNOWN DISTANCES (e.g.
2m, 3m, 4m) and computes DISTANCE_K = distance x box_height at each one
independently, then reports the mean and standard deviation across them.
If your camera/lens genuinely behaves as the inverse-relationship model
assumes, the K values computed at each distance should all be close to
each other - a big spread (high standard deviation, printed as a % of
the mean) is itself useful diagnostic information: it tells you something
is off (uneven lighting between readings, the person not standing the
same way each time, lens distortion at the extremes) rather than silently
producing a bad final number the way a single-shot version would.

USAGE
-----
Take 2-3 readings at clearly different, precisely measured distances
(use a tape measure or a marked spot - do not eyeball it):

    python calibrate.py --distance 2.0
    python calibrate.py --distance 3.0
    python calibrate.py --distance 4.0

Each run appends its result to calibration_log.json (next to this
script) instead of overwriting anything, so you can take readings across
multiple sessions. Pass --summary at any time to see the combined
average DISTANCE_K across every logged reading so far, without taking a
new one:

    python calibrate.py --summary

Pass --reset to clear the log and start over (e.g. if you moved the
camera and old readings no longer apply).
"""
import argparse
import json
import os
import statistics
import time

import cv2
import config
from detection.person_detector import PersonDetector

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration_log.json")


def _load_log():
    if os.path.exists(_LOG_PATH):
        try:
            with open(_LOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print(f"[CALIB] Warning: {_LOG_PATH} exists but couldn't be read - starting fresh.")
    return []


def _save_log(entries):
    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _print_summary(entries):
    if not entries:
        print("[CALIB] No readings logged yet. Take one with: python calibrate.py --distance <metres>")
        return

    ks = [e["k"] for e in entries]
    mean_k = statistics.mean(ks)
    print(f"\n[CALIB] {len(entries)} reading(s) logged so far:")
    for e in entries:
        pct_off = (e["k"] - mean_k) / mean_k * 100 if mean_k else 0
        print(f"    distance={e['distance']:>5.2f}m  avg_box_height={e['avg_height_px']:>6.1f}px"
              f"  -> K={e['k']:>7.0f}  ({pct_off:+.1f}% from mean)")

    if len(ks) >= 2:
        stdev = statistics.stdev(ks)
        spread_pct = (stdev / mean_k * 100) if mean_k else 0
        print(f"\n  mean DISTANCE_K = {mean_k:.0f}   (spread: {spread_pct:.1f}% of mean across readings)")
        if spread_pct > 15:
            print("  ! Spread is fairly high (>15%) - readings disagree more than expected.")
            print("    Consider retaking a reading: check the person stood upright and fully")
            print("    in frame each time, lighting was consistent, and distances were measured")
            print("    accurately (tape measure, not eyeballed).")
        else:
            print("  Readings agree well with each other - this is a reliable calibration.")
    else:
        print(f"\n  DISTANCE_K = {mean_k:.0f} (based on only 1 reading - take at least one more")
        print("  at a different distance for a real cross-check before trusting this number).")

    print(f"\n  -> Edit config.py and set:   DISTANCE_K = {mean_k:.0f}")


def _take_reading(distance, source, samples):
    person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
    cap = cv2.VideoCapture(int(source) if str(source).isdigit() else source)
    heights = []
    print(f"[CALIB] Stand still at exactly {distance} m from the camera "
          f"(measure it, don't estimate). Collecting {samples} samples... (q = stop early)")

    # Give the camera a moment to warm up / auto-expose before trusting frames -
    # a dark/overexposed first frame can otherwise skew the very first samples.
    time.sleep(1.0)

    while len(heights) < samples:
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
            cv2.putText(frame, f"h={h}px  {len(heights)}/{samples}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow("CAPHY calibration", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()

    if not heights:
        print("[CALIB] No person detected - make sure you're in view and try again.")
        return None

    # Drop the highest/lowest 10% of samples (simple outlier trim) before
    # averaging, so one frame where the box briefly jittered wide/narrow
    # doesn't skew this reading's result.
    heights.sort()
    trim = max(0, len(heights) // 10)
    trimmed = heights[trim:len(heights) - trim] if trim else heights
    avg = sum(trimmed) / len(trimmed)
    k = distance * avg

    print(f"\n[CALIB] This reading: samples={len(heights)} (used {len(trimmed)} after outlier trim)"
          f"   avg box height={avg:.1f}px   distance={distance}m   -> K={k:.0f}")
    return {"distance": distance, "avg_height_px": avg, "k": k, "samples": len(heights)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--distance", type=float,
                     help="your real, precisely measured distance from the camera, in metres")
    ap.add_argument("--source", default=str(config.CAMERA_INDEX))
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--summary", action="store_true",
                     help="show the combined result from all logged readings without taking a new one")
    ap.add_argument("--reset", action="store_true",
                     help="clear all logged readings and start over")
    args = ap.parse_args()

    if args.reset:
        _save_log([])
        print(f"[CALIB] Cleared {_LOG_PATH}. Start fresh with --distance <metres>.")
        return

    entries = _load_log()

    if args.summary:
        _print_summary(entries)
        return

    if args.distance is None:
        ap.error("--distance is required unless using --summary or --reset")

    reading = _take_reading(args.distance, args.source, args.samples)
    if reading is None:
        return

    entries.append(reading)
    _save_log(entries)
    _print_summary(entries)


if __name__ == "__main__":
    main()
