"""CAPHY - full integrated system (redesigned tiers).

  Tier 1 (far)    -> snapshot + phone alert
  Tier 2 (medium) -> snapshot + record until person leaves + alert
  Tier 3 (close)  -> snapshot + siren + record until stopped + alert
  Highest-Security mode -> any confirmed person triggers the full Tier-3 response

Voice runs in-process: arm / disarm / stop siren / system status.

    python main.py            # webcam + detection (no voice - voice is in the app)
    python main.py --no-yolo  # motion only, skip person verification
    python main.py --no-yolo  # motion only

Keys:  q quit,  m motion mask,  n night vision,  h toggle Highest-Security
"""
import argparse
import threading
import time

import cv2

import config
from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine
from detection.night_vision import NightVision
from storage.database import Database
from storage.alerts import AlertManager
from storage.push import PushSender
from siren import Siren

GREEN = (80, 200, 120)
AMBER = (60, 160, 240)
RED = (60, 60, 230)
GREY = (150, 150, 150)
WHITE = (240, 240, 240)
CYAN = (220, 200, 60)
TIER_COLOR = {1: GREEN, 2: AMBER, 3: RED}


class SystemState:
    def __init__(self, armed=True, highest=False):
        self._lock = threading.Lock()
        self.armed = armed
        self.highest = highest
        self._stop = False
        # No siren/alert until this time - set whenever we arm, so arming while
        # you're still in front of the camera doesn't instantly blast the siren.
        self.arm_grace_until = 0.0

    def set_armed(self, v):
        with self._lock:
            self.armed = v
            if v:
                self.arm_grace_until = time.time() + getattr(config, "ARM_GRACE_SEC", 8)

    def in_grace(self):
        with self._lock:
            return self.armed and time.time() < self.arm_grace_until

    def request_stop(self):
        with self._lock:
            self._stop = True

    def take_stop(self):
        with self._lock:
            v = self._stop
            self._stop = False
            return v


def open_source(source):
    cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
    return cap


def draw_hud(frame, result, fps, nv_on, armed, siren_on, highest):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 34), (25, 25, 25), -1)
    f1 = "MOTION" if result["motion"] else "no motion"
    cv2.putText(frame, "F1:", (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    cv2.putText(frame, f1, (45, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, AMBER if result["motion"] else GREY, 2)
    if result["ran_yolo"]:
        f2 = "PERSON" if result["persons"] else "no person"
        f2c = GREEN if result["persons"] else GREY
    else:
        f2, f2c = "idle", GREY
    cv2.putText(frame, "F2:", (165, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    cv2.putText(frame, f2, (200, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, f2c, 2)
    if result["tier"] > 0:
        cv2.putText(frame, f"TIER {result['tier']}", (330, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, TIER_COLOR[result["tier"]], 2)
    if nv_on:
        cv2.putText(frame, "NV", (435, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, CYAN, 2)
    cv2.putText(frame, ("MAX-SEC" if highest else ("ARMED" if armed else "DISARMED")),
                (w - 250, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                RED if highest else (GREEN if armed else GREY), 2)
    if siren_on:
        cv2.putText(frame, "SIREN", (w - 105, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2)
    for p in result["persons"]:
        x1, y1, x2, y2 = p["box"]
        c = TIER_COLOR.get(p.get("tier", 2), GREEN)
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        label = f"{p.get('label','person')}  {p['conf']:.2f}  {p.get('distance_m','?')}m"
        cv2.rectangle(frame, (x1, y1 - 20), (x1 + 250, y1), c, -1)
        cv2.putText(frame, label, (x1 + 4, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)
    if result["threat"] and armed:
        c = TIER_COLOR[result["tier"]] if result["tier"] > 0 else RED
        cv2.rectangle(frame, (0, h - 40), (w, h), c, -1)
        acts = result["persons"][0].get("actions", []) if result["persons"] else []
        cv2.putText(frame, f"THREAT  Tier {result['tier']}  ->  {', '.join(acts)}",
                    (12, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)
    elif not armed:
        cv2.rectangle(frame, (0, h - 40), (w, h), (40, 40, 40), -1)
        cv2.putText(frame, "DISARMED - monitoring only, no alerts",
                    (12, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREY, 2)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(config.CAMERA_INDEX))
    ap.add_argument("--no-yolo", action="store_true")
    args = ap.parse_args()

    motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY, config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
    tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST, config.TIER3_MAX_DIST,
                      getattr(config, "TIER_SMOOTHING", 0.35),
                      getattr(config, "TIER_HYSTERESIS", 0.12))
    nv = NightVision(config.CLAHE_CLIP, config.CLAHE_TILE, config.NIGHT_LOW_LIGHT, config.NIGHT_GAMMA, config.NIGHT_VISION_AUTO)
    person = None
    if not args.no_yolo:
        try:
            from detection.person_detector import PersonDetector
            person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
            print("[CAPHY] YOLO loaded - two-factor mode.")
        except Exception as e:
            print(f"[CAPHY] Could not load YOLO ({e}). Motion-only mode.")
    engine = TwoFactorDetector(motion, person, tier, config.PERSON_EVERY_N)

    db = Database(config.DB_PATH)
    alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                          config.SNAPSHOT_TIERS, config.RECORD_TIERS, config.PRESENCE_GRACE_SEC,
                          videos_dir=getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR))
    push = PushSender(config.FIREBASE_KEY, config.PUSH_TOPIC)
    # Start DISARMED so launching CAPHY never instantly fires the siren while
    # you're still sitting in front of the webcam. Arm it when ready (key,
    # phone, or voice); arming applies an 8s grace so you can step out of frame.
    db.conn.execute("UPDATE settings SET armed=0 WHERE setting_id=1")
    db.conn.commit()
    state = SystemState(armed=False, highest=config.HIGHEST_SECURITY)
    siren = Siren()
    print(f"[CAPHY] Ready. Alerts: {db.count_alerts()}. Armed: {state.armed}. Highest-Security: {state.highest}")

    cap = open_source(args.source)
    if not cap.isOpened():
        print(f"[CAPHY] Could not open source '{args.source}'."); db.close(); return

    show_mask = config.SHOW_MOTION_MASK
    prev = time.time()
    print("[CAPHY] Running. Press 'q' to quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[CAPHY] End of stream."); break
        frame, nv_on = nv.process(frame)
        result = engine.process(frame)

        # Highest-Security: treat any confirmed person as Tier 3
        if state.highest and result["threat"] and result["tier"] < 3:
            result["tier"] = 3
            for p in result["persons"]:
                p["tier"] = 3
                p["actions"] = ["snapshot", "record", "siren", "alert"]

        now = time.time()
        fps = 1.0 / max(now - prev, 1e-6)
        prev = now
        armed = state.armed

        # stop request from the phone app stops siren + recording
        stop_now = state.take_stop()
        if stop_now:
            alerts.request_stop()

        siren_on = (armed and result["threat"] and result["tier"] in config.SIREN_TIERS
                    and not stop_now and not state.in_grace())
        # keep siren on across frames while the Tier-3 threat is present
        if siren_on:
            siren.start()
        elif not (armed and result["threat"] and result["tier"] in config.SIREN_TIERS):
            siren.stop()

        draw_hud(frame, result, fps, nv_on, armed, siren.active, state.highest)

        if armed:
            alert_id = alerts.handle(frame, result, fps)
            if alert_id is not None:
                p = max(result["persons"], key=lambda x: x["tier"])
                print(f"[CAPHY] SAVED alert #{alert_id}  Tier {result['tier']}  "
                      f"dist~{p['distance_m']}m  actions={p['actions']}")
                if result["tier"] >= config.PUSH_MIN_TIER:
                    srow = db.conn.execute("SELECT snapshot_path FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
                    push.send(result["tier"], p["distance_m"], srow["snapshot_path"] if srow else None)

        cv2.imshow("CAPHY - Integrated System", frame)
        if show_mask and result["mask"] is not None:
            cv2.imshow("motion mask", result["mask"])
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("m"):
            show_mask = not show_mask
            if not show_mask:
                cv2.destroyWindow("motion mask")
        if key == ord("n"):
            nv.enabled = not nv.enabled
            print(f"[CAPHY] Night vision {'ON' if nv.enabled else 'OFF'}")
        if key == ord("h"):
            state.highest = not state.highest
            print(f"[CAPHY] Highest-Security {'ON' if state.highest else 'OFF'}")

    siren.shutdown(); cap.release(); cv2.destroyAllWindows(); db.close()
    print("[CAPHY] Stopped.")


if __name__ == "__main__":
    main()