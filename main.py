"""CAPHY - full integrated system (Phases 2,3,4,9 + 10 integration).

Camera -> two-factor detection -> tier -> (if ARMED) save alert + siren.
Voice runs in the same process: 'arm'/'disarm' change the state live,
'stop siren' silences the alarm, 'system status' reports.

    python main.py            # webcam + detection + voice
    python main.py --no-voice # detection only (no microphone)
    python main.py --no-yolo  # motion only

Keys:  q quit,  m motion mask,  n night vision
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
    """Shared between the detection loop and the voice thread."""
    def __init__(self, armed=True):
        self._lock = threading.Lock()
        self.armed = armed
        self._stop_siren = False

    def set_armed(self, v):
        with self._lock:
            self.armed = v

    def request_stop_siren(self):
        with self._lock:
            self._stop_siren = True

    def take_stop_siren(self):
        with self._lock:
            v = self._stop_siren
            self._stop_siren = False
            return v


def voice_worker(state):
    """Background thread: listen for commands and update the system state."""
    import os
    if not os.path.isdir(config.VOSK_MODEL_PATH):
        print("[CAPHY] Voice off (Vosk model not found).")
        return
    try:
        from voice.engine import VoskVoice
        from voice.commands import CommandInterpreter
    except Exception as e:
        print(f"[CAPHY] Voice off ({e}).")
        return
    voice = VoskVoice(config.VOSK_MODEL_PATH, config.VOICE_SAMPLE_RATE)
    ci = CommandInterpreter()
    print("[CAPHY] Voice active (arm / disarm / stop siren / system status).")
    for text in voice.listen():
        r = ci.interpret(text)
        if r is None:
            voice.say("Sorry, please repeat that.")
            continue
        if r.action == "status":
            voice.say(f"System is {'armed' if state.armed else 'disarmed'}.")
        else:
            voice.say(r.speak)
        if r.action == "arm":
            state.set_armed(True)
        elif r.action == "disarm":
            state.set_armed(False)
        elif r.action == "stop_siren":
            state.request_stop_siren()


def open_source(source):
    cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
    return cap


def draw_hud(frame, result, fps, nv_on, armed, siren_on):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 34), (25, 25, 25), -1)

    f1 = "MOTION" if result["motion"] else "no motion"
    cv2.putText(frame, "F1:", (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    cv2.putText(frame, f1, (45, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                AMBER if result["motion"] else GREY, 2)

    if result["ran_yolo"]:
        f2 = "PERSON" if result["persons"] else "no person"
        f2c = GREEN if result["persons"] else GREY
    else:
        f2, f2c = "idle", GREY
    cv2.putText(frame, "F2:", (170, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    cv2.putText(frame, f2, (205, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, f2c, 2)

    if result["tier"] > 0:
        cv2.putText(frame, f"TIER {result['tier']}", (340, 23),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, TIER_COLOR[result["tier"]], 2)
    if nv_on:
        cv2.putText(frame, "NV", (445, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, CYAN, 2)

    cv2.putText(frame, "ARMED" if armed else "DISARMED", (w - 250, 23),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN if armed else GREY, 2)
    if siren_on:
        cv2.putText(frame, "SIREN", (w - 110, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2)

    for p in result["persons"]:
        x1, y1, x2, y2 = p["box"]
        c = TIER_COLOR.get(p.get("tier", 2), GREEN)
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        label = f"{p.get('label','person')}  {p['conf']:.2f}  {p.get('distance_m','?')}m"
        cv2.rectangle(frame, (x1, y1 - 20), (x1 + 250, y1), c, -1)
        cv2.putText(frame, label, (x1 + 4, y1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)

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
    ap.add_argument("--no-voice", action="store_true")
    args = ap.parse_args()

    motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY,
                            config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
    tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST, config.TIER3_MAX_DIST)
    nv = NightVision(config.CLAHE_CLIP, config.CLAHE_TILE, config.NIGHT_LOW_LIGHT,
                     config.NIGHT_GAMMA, config.NIGHT_VISION_AUTO)

    person = None
    if not args.no_yolo:
        try:
            from detection.person_detector import PersonDetector
            person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
            print("[CAPHY] YOLO loaded - two-factor mode.")
        except Exception as e:
            print(f"[CAPHY] Could not load YOLO ({e}). Motion-only mode.")

    engine = TwoFactorDetector(motion, person, tier)

    db = Database(config.DB_PATH)
    alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                          config.VIDEO_SECONDS, config.VIDEO_FPS,
                          config.SNAPSHOT_TIERS, config.VIDEO_TIERS)
    row = db.conn.execute("SELECT armed FROM settings WHERE setting_id=1").fetchone()
    state = SystemState(armed=bool(row["armed"]) if row else True)
    siren = Siren()
    print(f"[CAPHY] Database ready. Alerts so far: {db.count_alerts()}. Armed: {state.armed}")
    push = PushSender(config.FIREBASE_KEY, config.PUSH_TOPIC)

    if not args.no_voice:
        threading.Thread(target=voice_worker, args=(state,), daemon=True).start()

    cap = open_source(args.source)
    if not cap.isOpened():
        print(f"[CAPHY] Could not open source '{args.source}'.")
        db.close()
        return

    show_mask = config.SHOW_MOTION_MASK
    prev = time.time()
    manual_stop = False
    print("[CAPHY] Running. Press 'q' to quit.")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[CAPHY] End of stream.")
            break

        frame, nv_on = nv.process(frame)
        result = engine.process(frame)

        now = time.time()
        fps = 1.0 / max(now - prev, 1e-6)
        prev = now
        armed = state.armed

        # ---- siren control (Phase 10) ----
        if state.take_stop_siren():
            manual_stop = True
        if not result["threat"]:
            manual_stop = False                 # reset once the threat clears
        siren_on = armed and result["threat"] and result["tier"] == 3 and not manual_stop
        if siren_on:
            siren.start()
        else:
            siren.stop()

        draw_hud(frame, result, fps, nv_on, armed, siren.active)

        # ---- only save alerts when ARMED ----
        if armed:
            alert_id = alerts.handle(frame, result)
            if alert_id is not None:
                p = max(result["persons"], key=lambda x: x["tier"])
                print(f"[CAPHY] SAVED alert #{alert_id}  Tier {result['tier']}  "
                      f"dist~{p['distance_m']}m  actions={p['actions']}")
                if result["tier"] >= config.PUSH_MIN_TIER:
                    push.send(result["tier"], p["distance_m"])

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

    siren.stop()
    cap.release()
    cv2.destroyAllWindows()
    db.close()
    print("[CAPHY] Stopped.")


if __name__ == "__main__":
    main()