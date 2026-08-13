# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

"""CAPHY evaluation - turns recorded runs into the numbers your thesis needs.

    python tools\\evaluate.py                 all recorded sessions
    python tools\\evaluate.py --session X     one session only
    python tools\\evaluate.py --list          what sessions exist
    python tools\\evaluate.py --csv out.csv   export raw rows for SPSS/Excel
    python tools\\evaluate.py --reset         wipe recorded events (start over)

WHAT IT REPORTS

1. FALSE ALARM REDUCTION - the headline number for your two-factor claim.
   A motion-only system alerts on every motion event. CAPHY alerts only when
   YOLO also confirms a person. The gap is what your contribution prevented.

2. PRECISION / RECALL / F1 - only when you labelled runs with a ground truth
   (EVAL_GROUND_TRUTH in config.py). Needs both "person" and "no_person" runs.

3. TIER DISTRIBUTION and FPS - for the performance section.
"""
import argparse
import csv
import sqlite3

import config


def rows(conn, session=None):
    q = "SELECT * FROM detection_events"
    a = ()
    if session:
        q += " WHERE session=?"
        a = (session,)
    q += " ORDER BY event_id"
    return [dict(r) for r in conn.execute(q, a)]


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def bar(p, width=28):
    fill = int(round(width * p / 100.0))
    return "#" * fill + "." * (width - fill)


# ---------------------------------------------------------------- reports

def false_alarm_reduction(data):
    motion = len(data)
    confirmed = sum(1 for r in data if r["person"])
    rejected = motion - confirmed

    print("\n" + "=" * 62)
    print("1. FALSE ALARM REDUCTION  (the two-factor claim)")
    print("=" * 62)
    if motion == 0:
        print("  No events recorded yet.")
        return
    print(f"  Motion events (Factor 1 fired)        : {motion}")
    print(f"  Person confirmed  (Factor 2 passed)   : {confirmed}")
    print(f"  Rejected as non-human (Factor 2 )     : {rejected}")
    print()
    print(f"  A motion-only system would have raised {motion} alerts.")
    print(f"  CAPHY raised {confirmed}.")
    print()
    r = pct(rejected, motion)
    print(f"  FALSE ALARM REDUCTION: {r:.1f}%")
    print(f"  [{bar(r)}]")
    print()
    print("  Report this as: 'Two-factor validation suppressed "
          f"{rejected} of {motion} motion events ({r:.1f}%)'.")


def accuracy(data):
    labelled = [r for r in data if r["ground_truth"] in ("person", "no_person")]
    print("\n" + "=" * 62)
    print("2. DETECTION ACCURACY  (needs labelled runs)")
    print("=" * 62)
    if not labelled:
        print("  No labelled data.")
        print("  To get this: set EVAL_GROUND_TRUTH in config.py before a run.")
        print('    "person"    - someone IS walking in frame')
        print('    "no_person" - only a pet, branch, shadow, curtain, etc.')
        return

    tp = sum(1 for r in labelled if r["ground_truth"] == "person" and r["person"])
    fn = sum(1 for r in labelled if r["ground_truth"] == "person" and not r["person"])
    fp = sum(1 for r in labelled if r["ground_truth"] == "no_person" and r["person"])
    tn = sum(1 for r in labelled if r["ground_truth"] == "no_person" and not r["person"])

    print(f"  Labelled events: {len(labelled)}\n")
    print("                        DETECTED")
    print("                     person   no person")
    print(f"  ACTUAL person       {tp:6}   {fn:9}   <- FN = missed intruder")
    print(f"         no person    {fp:6}   {tn:9}")
    print("                      ^ FP = false alarm\n")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    acc = (tp + tn) / len(labelled)

    print(f"  Precision : {precision:.3f}   of the alerts raised, this fraction were real")
    print(f"  Recall    : {recall:.3f}   of the real intruders, this fraction were caught")
    print(f"  F1 score  : {f1:.3f}")
    print(f"  Accuracy  : {acc:.3f}")
    if fn:
        print(f"\n  NOTE: {fn} missed detection(s). For a security system a missed")
        print("  intruder is worse than a false alarm - discuss this in Ch. 5.")


def tiers_and_speed(data):
    print("\n" + "=" * 62)
    print("3. TIER DISTRIBUTION AND PERFORMANCE")
    print("=" * 62)
    conf = [r for r in data if r["person"]]
    if not conf:
        print("  No confirmed detections yet.")
    else:
        print("  Tier assigned on confirmed detections:")
        for t in (1, 2, 3):
            n = sum(1 for r in conf if r["tier"] == t)
            print(f"    Tier {t}: {n:5}  ({pct(n,len(conf)):5.1f}%)  [{bar(pct(n,len(conf)),20)}]")
        d = [r["est_distance"] for r in conf if r["est_distance"]]
        if d:
            print(f"\n  Estimated distance: min {min(d):.1f} m | "
                  f"mean {sum(d)/len(d):.1f} m | max {max(d):.1f} m")
        c = [r["confidence"] for r in conf if r["confidence"]]
        if c:
            print(f"  YOLO confidence   : min {min(c):.2f} | "
                  f"mean {sum(c)/len(c):.2f} | max {max(c):.2f}")

    f = [r["fps"] for r in data if r["fps"]]
    if f:
        f_sorted = sorted(f)
        print(f"\n  FPS: min {min(f):.1f} | median {f_sorted[len(f)//2]:.1f} | "
              f"mean {sum(f)/len(f):.1f} | max {max(f):.1f}")
        print("  (report the median - the mean is skewed by startup frames)")


def sessions(conn):
    print("\nRecorded sessions:\n")
    q = ("SELECT session, ground_truth, COUNT(*) n, SUM(person) confirmed "
         "FROM detection_events GROUP BY session, ground_truth ORDER BY session")
    any_row = False
    for r in conn.execute(q):
        any_row = True
        name = r["session"] or "(unlabelled)"
        gt = r["ground_truth"] or "-"
        print(f"  {name:28} truth={gt:10} events={r['n']:5} confirmed={r['confirmed']}")
    if not any_row:
        print("  Nothing recorded yet. Set EVAL_LOGGING = True in config.py,")
        print("  run app.py, and walk in front of the camera.")


def export_csv(data, path):
    if not data:
        print("Nothing to export.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
        w.writeheader()
        w.writerows(data)
    print(f"Exported {len(data)} rows -> {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("SELECT 1 FROM detection_events LIMIT 1")
    except sqlite3.OperationalError:
        print("No detection_events table yet - run app.py once to create it.")
        return

    if args.reset:
        if input("Delete ALL recorded evaluation events? (yes/no) ").strip().lower() == "yes":
            conn.execute("DELETE FROM detection_events")
            conn.commit()
            print("Cleared.")
        else:
            print("Cancelled.")
        return

    if args.list:
        sessions(conn)
        return

    data = rows(conn, args.session)

    if args.csv:
        export_csv(data, args.csv)
        return

    title = f"session '{args.session}'" if args.session else "ALL sessions"
    print(f"\nCAPHY EVALUATION - {title}   ({len(data)} events)")

    false_alarm_reduction(data)
    accuracy(data)
    tiers_and_speed(data)
    print()


if __name__ == "__main__":
    main()
