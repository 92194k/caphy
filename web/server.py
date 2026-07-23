"""CAPHY Desktop Security Console (Flask web dashboard).

Six screens matching the mockups, wired to real data:
  /login    password-protected entry (session)
  /         Dashboard  - KPIs, live feed, recent alerts, 24h threat timeline
  /live     Live Camera - MJPEG + Factor 1/2 overlay, distance, camera switcher, 2-factor log
  /history  Alert History - filter/search/export, snapshot thumbnails
  /logs     System Logs - live log tail, health metrics, per-module status
  /settings Settings - sensitivities, tier thresholds, night vision, auto-arm, highest-security

Runs the detection engine in a background thread and saves alerts, so the
dashboard IS a full running system. Requires the new config/alerts/tier_engine.
"""
import io
import os
import csv
import json
import queue
import socket
import secrets
import hashlib
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import cv2
import numpy as np
from flask import (Flask, Response, request, redirect, url_for, session,
                   render_template, render_template_string, send_file, abort, jsonify,
                   stream_with_context)

import sys
import config
from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine
from detection.night_vision import NightVision
from storage.database import Database
from storage.alerts import AlertManager
from siren import Siren

try:
    import psutil
except Exception:
    psutil = None


def _resource_dir(*parts):
    """Resolve a bundled resource folder both when running from source AND
    when frozen into a PyInstaller .exe. Frozen builds unpack data files to
    sys._MEIPASS; from source they sit next to the project. This is what lets
    Flask find web/templates and web/static inside the packaged app."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, *parts)
    # from source: this file is web/server.py -> project root is one up
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


app = Flask(__name__,
            template_folder=_resource_dir("web", "templates"),
            static_folder=_resource_dir("web", "static"))
app.secret_key = "caphy-local-console-secret"
app.permanent_session_lifetime = timedelta(days=30)   # "remember this device"

TIER_BGR = {1: (80, 200, 120), 2: (60, 160, 240), 3: (60, 60, 230)}

# ==================== persistent log file (caphy.log) ====================
# The System Logs page shows real totals ("18,742 events today", "last
# event", "alerts (24h)") - those need to survive a restart and cover the
# whole day, not just whatever's still in the last-200 in-memory buffer per
# camera. Every _log() call also appends one line here.
LOG_FILE_PATH = getattr(config, "LOG_FILE_PATH", "caphy.log")
_log_file_lock = threading.Lock()


def _append_log_file(level, mod, msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{level}\t{mod}\t{msg}\n"
    try:
        with _log_file_lock:
            with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass   # a logging failure should never take detection down


def _log_file_stats():
    """(events_today, last_event_hhmmss, alerts_last_24h) from caphy.log.
    Cheap line scan - fine at thesis/demo scale; never raises."""
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.now() - timedelta(hours=24)
    events_today = 0
    alerts_24h = 0
    last_event = "-"
    try:
        with _log_file_lock:
            if not os.path.exists(LOG_FILE_PATH):
                return 0, "-", 0
            with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t", 3)
                    if len(parts) < 3:
                        continue
                    ts, level = parts[0], parts[1]
                    if ts.startswith(today):
                        events_today += 1
                        last_event = ts[11:19]
                    if level == "ALERT":
                        try:
                            if datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") >= cutoff:
                                alerts_24h += 1
                        except ValueError:
                            pass
    except Exception:
        return 0, "-", 0
    return events_today, last_event, alerts_24h


# ==================== camera / detection worker ====================
class Worker(threading.Thread):
    def __init__(self, source, cam_id=0):
        super().__init__(daemon=True)
        self.source = source
        self.cam_id = cam_id
        names = getattr(config, "CAMERA_NAMES", [])
        self.name = names[cam_id] if cam_id < len(names) else f"Cam {cam_id}"
        self.running = True
        self.paused = False             # camera OFF: release the device, stop detecting
        self.lock = threading.Lock()
        self.jpeg = None
        self.raw_frame = None    # last raw BGR frame, for the WebRTC track
        self.manual_record = False      # toggled by the phone Live tab
        self._mrec = None
        self._mrec_path = None
        self.last_record = None         # basename of the last finished recording
        self.stats = {"online": False, "motion": False, "person": False,
                      "tier": 0, "distance": "-", "conf": "-", "fps": 0.0,
                      "armed": True, "camera_on": True}
        self.logs = deque(maxlen=200)

        self.motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY,
                                     config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
        self.tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST,
                               config.TIER3_MAX_DIST,
                               getattr(config, "TIER_SMOOTHING", 0.35),
                               getattr(config, "TIER_HYSTERESIS", 0.12))
        self.nv = NightVision(config.CLAHE_CLIP, config.CLAHE_TILE, config.NIGHT_LOW_LIGHT,
                              config.NIGHT_GAMMA, config.NIGHT_VISION_AUTO)
        self.person = None
        self.yolo_ok = False
        try:
            from detection.person_detector import PersonDetector
            self.person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID,
                                         config.PERSON_CONF, getattr(config, "PERSON_IMGSZ", 640))
            self.yolo_ok = True
        except Exception as e:
            self._log("WARN", "yolo", f"not loaded ({e})")
        self.engine = TwoFactorDetector(self.motion, self.person, self.tier, config.PERSON_EVERY_N)
        self.highest = config.HIGHEST_SECURITY

    def _log(self, level, mod, msg):
        self.logs.appendleft({"t": datetime.now().strftime("%H:%M:%S"), "cam": self.cam_id,
                              "level": level, "mod": mod, "msg": msg})
        _append_log_file(level, f"{mod}({self.name})", msg)

    def update_settings(self, sensitivity, person_conf, t1, t3, night, highest):
        self.motion.min_area = float(sensitivity)
        if self.person is not None:
            self.person.conf = float(person_conf)
        self.tier.tier1_min = float(t1)
        self.tier.tier3_max = float(t3)
        self.nv.enabled = bool(night)
        self.highest = bool(highest)
        self._log("INFO", "settings", "updated from dashboard")

    def _annotate(self, frame, result):
        for p in result.get("persons", []):
            x1, y1, x2, y2 = p["box"]
            c = TIER_BGR.get(p.get("tier", 2), (80, 200, 120))
            cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
            lbl = f"{p.get('label','person')} {p['conf']:.2f} {p.get('distance_m','?')}m"
            cv2.rectangle(frame, (x1, y1 - 18), (x1 + 230, y1), c, -1)
            cv2.putText(frame, lbl, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)

        # ---- CCTV-style caption bar: camera name + date/time, burned into
        # every frame so it's ALSO in every snapshot/recording taken from it,
        # on both the PC and the phone (they both read this same frame). ----
        caption = f"{self.name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        (tw, th), _ = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(frame, (0, 0), (tw + 20, th + 18), (0, 0, 0), -1)
        cv2.putText(frame, caption, (10, th + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)

        if result.get("threat"):
            c = TIER_BGR[result["tier"]]
            cv2.rectangle(frame, (0, frame.shape[0] - 28), (frame.shape[1], frame.shape[0]), c, -1)
            cv2.putText(frame, f"THREAT - Tier {result['tier']}", (8, frame.shape[0] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        return frame

    def run(self):
        db = Database(config.DB_PATH)
        alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                              config.SNAPSHOT_TIERS, config.RECORD_TIERS, config.PRESENCE_GRACE_SEC,
                              self.name,
                              videos_dir=getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR))
        cap = self._open_capture()
        prev = time.time()
        while self.running:
            # ---- camera OFF: release the device entirely, run no detection ----
            if self.paused:
                if cap is not None:
                    cap.release()
                    cap = None
                    self._log("INFO", "camera", f"{self.name}: camera OFF (device released)")
                self._store_placeholder("Camera off")
                _update_siren(self.cam_id, False)
                time.sleep(0.3)
                continue
            if cap is None:                      # coming back from OFF
                cap = self._open_capture()
                prev = time.time()

            ok, frame = cap.read()
            if not ok:
                self._store_placeholder()
                _update_siren(self.cam_id, False)
                time.sleep(0.4)
                continue
            frame, nv_on = self.nv.process(frame)
            result = self.engine.process(frame)

            if self.highest and result["threat"] and result["tier"] < 3:
                result["tier"] = 3
                for p in result["persons"]:
                    p["tier"] = 3
                    p["actions"] = ["snapshot", "record", "siren", "alert"]

            now = time.time()
            fps = 1.0 / max(now - prev, 1e-6)
            prev = now

            row = db.conn.execute("SELECT armed FROM settings WHERE setting_id=1").fetchone()
            armed = bool(row["armed"]) if row else True

            if result["motion"] and result["ran_yolo"]:
                if result["threat"]:
                    self._log("DETECT", "yolo", f"person conf={result['persons'][0]['conf']:.2f} tier={result['tier']}")
                else:
                    self._log("DETECT", "yolo", "motion, no person - ignored")

                # ---- evaluation data (thesis Chapter 4) ----
                # Logged only when EVAL_LOGGING is on, so normal runs don't
                # fill the database. Records rejected motion too - that is the
                # evidence that Factor 2 prevents false alarms.
                if getattr(config, "EVAL_LOGGING", False):
                    try:
                        pp = result["persons"][0] if result["persons"] else None
                        db.log_detection_event(
                            session=getattr(config, "EVAL_SESSION", ""),
                            ground_truth=getattr(config, "EVAL_GROUND_TRUTH", ""),
                            camera=self.name,
                            motion=True,
                            ran_yolo=True,
                            person=bool(result["persons"]),
                            persons_n=len(result["persons"]),
                            motion_area=result.get("motion_area", 0.0),
                            bbox_height=(pp["box"][3] - pp["box"][1]) if pp else 0,
                            est_distance=(pp.get("distance_m") if pp else None),
                            tier=result["tier"],
                            confidence=(pp["conf"] if pp else None),
                            fps=round(fps, 1))
                    except Exception as e:
                        self._log("WARN", "eval", f"could not log event ({e})")

            # Siren only after the just-armed grace window - so arming (or
            # starting the system) while you're standing right in front of the
            # webcam does NOT instantly blast the siren at Tier 3.
            siren_on = (armed and result["threat"]
                        and result["tier"] in config.SIREN_TIERS
                        and time.time() >= _arm_grace_until)
            _update_siren(self.cam_id, siren_on)

            self._annotate(frame, result)

            # manual recording toggled from the phone (Live tab)
            if self.manual_record:
                if self._mrec is None:
                    self._start_manual_record(frame, fps)
                if self._mrec is not None:
                    self._mrec.write(frame)
            elif self._mrec is not None:
                self._mrec.release()
                self._mrec = None
                # remember the finished file so the phone can download it
                if self._mrec_path:
                    self.last_record = os.path.basename(self._mrec_path)
                    self._log("INFO", "record", f"saved {os.path.abspath(self._mrec_path)}")

            # Alert-fatigue policy:
            #   - DETECT & SAVE all tiers ALWAYS (even disarmed), each with its
            #     snapshot, so the alert history is complete and Tier 1/2 are
            #     there to review - they just don't buzz your phone.
            #   - PUSH a phone notification only for Tier 3 when disarmed; when
            #     ARMED, push every tier (you're actively guarding, you want to
            #     know about everything).
            # The just-armed grace window still suppresses alerts right after
            # arming (so walking away from the laptop doesn't alert on you).
            in_arm_grace = armed and time.time() < _arm_grace_until
            if not in_arm_grace:
                aid = alerts.handle(frame, result, fps)
                if aid is not None:
                    p = max(result["persons"], key=lambda x: x["tier"])
                    self._log("ALERT", "db", f"alert #{aid} Tier {result['tier']} saved")
                    # push the new alert to every connected /alerts page or
                    # phone app the instant it's saved - no polling delay.
                    try:
                        arow = db.conn.execute(
                            "SELECT * FROM alerts WHERE alert_id=?", (aid,)).fetchone()
                        if arow:
                            ajson = _alert_json(arow)
                            _broadcast_alert(ajson)
                            # Publish to the cloud so the phone can see this
                            # alert from ANYWHERE (mobile data), not just on
                            # the laptop's LAN. Best-effort; never blocks
                            # detection if offline.
                            try:
                                owner = _current_device_owner_uid()
                                if owner:
                                    from identity import get_device_identity
                                    from storage import cloud_alerts
                                    snap_path = arow["snapshot_path"] if "snapshot_path" in arow.keys() else None
                                    cloud_alerts.publish_alert(
                                        owner, get_device_identity()["device_id"],
                                        ajson, snap_path)
                            except Exception as ce:
                                self._log("WARN", "alerts", f"cloud publish failed: {ce}")
                    except Exception as e:
                        self._log("WARN", "alerts", f"broadcast failed: {e}")
                    # Push gate (alert fatigue): armed -> notify every tier;
                    # disarmed -> notify Tier 3 only. Either way the alert was
                    # already saved above, so nothing is lost - Tier 1/2 just
                    # stay silent in the list until you open the app.
                    should_push = armed or (result["tier"] >= 3)
                    if should_push:
                        srow = db.conn.execute("SELECT snapshot_path FROM alerts WHERE alert_id=?", (aid,)).fetchone()
                        # Scoped push only - NEVER the old shared "caphy_alerts"
                        # topic (that sent every laptop's alerts to every
                        # subscribed phone, account boundaries or not). This
                        # laptop may not have an owner yet (nobody signed in /
                        # paired), which is fine: no owner means no phone could
                        # have legitimately subscribed to its topic either, so
                        # skipping the push here is the correct no-op, not a bug.
                        try:
                            from identity import get_device_identity
                            owner_uid = _current_device_owner_uid()
                            if owner_uid:
                                from storage.firebase_push import get_push_sender_for_alert
                                sender = get_push_sender_for_alert(owner_uid, get_device_identity()["device_id"])
                                if sender:
                                    sender.send_alert(
                                        title=f"CAPHY Alert - Tier {result['tier']}",
                                        body=f"{self.name}: person detected ({p['distance_m']}m)",
                                        data={"alert_id": str(aid), "camera": self.name},
                                        tier=result["tier"],
                                    )
                                    self._log("ALERT", "fcm", f"push Tier {result['tier']} sent (owner-scoped)")
                        except Exception as e:
                            self._log("WARN", "fcm", f"scoped push failed: {e}")

            p0 = result["persons"][0] if result["persons"] else None
            self._store(frame, {"online": True, "motion": result["motion"],
                                "person": bool(result["persons"]), "tier": result["tier"],
                                "distance": (p0["distance_m"] if p0 else "-"),
                                "conf": (round(p0["conf"], 2) if p0 else "-"),
                                "fps": round(fps, 1), "armed": armed,
                                "camera_on": True})
        if cap is not None:
            cap.release()
        db.close()

    def _start_manual_record(self, frame, fps):
        """Open a VideoWriter, trying codecs until one actually opens.
        mp4v often fails silently on Windows OpenCV, so we fall back to MJPG
        (.avi), which is available almost everywhere."""
        vdir = getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR)
        os.makedirs(vdir, exist_ok=True)
        h, w = frame.shape[:2]
        wfps = max(min(fps, 30.0), 5.0)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cam_label = "".join(c for c in self.name if c.isalnum()) or f"Cam{self.cam_id}"
        # (fourcc, extension) candidates, in order of preference
        for fourcc, ext in (("mp4v", "mp4"), ("avc1", "mp4"), ("MJPG", "avi"), ("XVID", "avi")):
            path = os.path.join(vdir, f"CAPHY_{cam_label}_manual_{stamp}.{ext}")
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), wfps, (w, h))
            if writer.isOpened():
                self._mrec = writer
                self._mrec_path = path
                self._log("INFO", "record", f"recording -> {os.path.abspath(path)} ({fourcc})")
                return
            writer.release()
        self._mrec = None
        self._log("WARN", "record", "could not open any video codec - recording disabled")

    def _open_capture(self):
        """Open the video source. Returns a VideoCapture (possibly not opened)."""
        src = self.source
        if isinstance(src, str) and src.isdigit():
            src = int(src)
        if isinstance(src, int) and os.name == "nt":
            cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)   # DirectShow = reliable on Windows
        else:
            cap = cv2.VideoCapture(src)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, getattr(config, "CAP_BUFFERSIZE", 1))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
        except Exception:
            pass
        if not cap.isOpened():
            self._log("WARN", "camera", f"{self.name}: source {self.source} did NOT open "
                      f"(camera unplugged, wrong RTSP URL/password, or in use by another app?)")
        else:
            self._log("INFO", "camera", f"{self.name}: opened source {self.source}")
        return cap

    def _store(self, frame, stats):
        q = getattr(config, "JPEG_QUALITY", 80)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                # Raw BGR frame, kept alongside the JPEG so the WebRTC track
                # (web/webrtc_stream.py) can feed aiortc directly instead of
                # decoding JPEG back to raw every frame - same annotated
                # frame MJPEG and WebRTC viewers both end up seeing, just
                # two different encodings of one camera read.
                self.raw_frame = frame
                self.stats = stats

    def _store_placeholder(self, text="Camera offline"):
        img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
        cv2.putText(img, text, (40, config.FRAME_HEIGHT // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (110, 110, 110), 2)
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.raw_frame = img
                self.stats = {**self.stats, "online": False, "motion": False,
                              "person": False, "tier": 0,
                              "camera_on": not self.paused}

    def get_jpeg(self):
        with self.lock:
            return self.jpeg

    def get_frame(self):
        """Latest raw BGR frame (numpy array), or None before the first
        camera read. Used by the WebRTC track - see web/webrtc_stream.py."""
        with self.lock:
            return None if self.raw_frame is None else self.raw_frame.copy()

    def get_stats(self):
        with self.lock:
            return dict(self.stats)

    def get_logs(self):
        with self.lock:
            return list(self.logs)


workers = []

# ---- real-time alert push (Server-Sent Events) ----
# Every connected /alerts page or phone app registers a Queue here. The
# instant a new alert is confirmed in a Worker's detection loop, we push it
# into every queue - no polling delay, no manual refresh needed.
_alert_subs = []
_alert_subs_lock = threading.Lock()


def _alert_subscribe():
    q = queue.Queue()
    with _alert_subs_lock:
        _alert_subs.append(q)
    return q


def _alert_unsubscribe(q):
    with _alert_subs_lock:
        if q in _alert_subs:
            _alert_subs.remove(q)


def _broadcast_alert(payload):
    with _alert_subs_lock:
        subs = list(_alert_subs)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass


_owner_uid_cache = {"uid": None, "checked_at": 0.0}
_OWNER_UID_CACHE_TTL_SEC = 30


def _current_device_owner_uid():
    """
    Who does THIS laptop currently belong to, per the Firestore device
    registry (devices/{device_id}.owner_uid) - the same field
    confirm_pairing() and register_device(owner_uid=...) write.

    Used by the detection loop (a background thread with no Flask session)
    to decide whether an alert is allowed to push at all: no owner means
    nobody has ever signed in or paired this install, so there is no
    legitimate phone subscription to send to - the correct behavior is to
    stay silent, not to fall back to some other topic.

    Cached briefly (30s) so a burst of detections doesn't turn into a
    Firestore read per frame; owner_uid changes at most once in the
    lifetime of a normal session (pairing / first login), so a short TTL
    is plenty responsive without adding read cost.
    """
    now = time.time()
    if now - _owner_uid_cache["checked_at"] < _OWNER_UID_CACHE_TTL_SEC:
        return _owner_uid_cache["uid"]

    uid = None
    try:
        from identity import get_device_identity
        from storage import device_registry
        device_id = get_device_identity()["device_id"]
        device = device_registry.get_device(device_id)
        if device:
            uid = device.get("owner_uid")
    except Exception:
        uid = None

    _owner_uid_cache["uid"] = uid
    _owner_uid_cache["checked_at"] = now
    return uid


# ---- lightweight UI preferences (camera names, etc.) persisted to a JSON file ----
UI_PREFS_PATH = "ui_prefs.json"


def load_prefs():
    try:
        with open(UI_PREFS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(update):
    p = load_prefs()
    p.update(update)
    try:
        with open(UI_PREFS_PATH, "w") as f:
            json.dump(p, f, indent=2)
    except Exception:
        pass

# ---- shared siren across cameras (on if ANY camera is at a siren tier) ----
_siren = Siren()
_siren_states = {}
_siren_manual = False          # manual "Trigger Siren" override from the Live page
_siren_lock = threading.Lock()


def _recompute_siren():
    if any(_siren_states.values()) or _siren_manual:
        _siren.start()
    else:
        _siren.stop()


def _update_siren(cam_id, wants):
    with _siren_lock:
        _siren_states[cam_id] = wants
        _recompute_siren()


def scan_cameras(max_index=6):
    """Probe device indices; return the ones that actually open + read a frame."""
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
        ok = cap.isOpened()
        if ok:
            ok, _ = cap.read()
        cap.release()
        if ok:
            found.append(i)
    return found


def camera_label(source):
    """Friendly name for a camera source. Index 0 is (by convention on
    laptops) the built-in webcam; higher indices are USB/external; strings
    are network/IP camera URLs."""
    if isinstance(source, str):
        return "Network Camera"
    if source == 0:
        return "Built-in (Laptop) Camera"
    return f"USB / External Camera {source}"


def available_cameras():
    """
    Every camera the system can currently offer to choose from = the ones
    ALREADY in use by a running worker (a busy device can't be re-probed, so
    we must include them explicitly) PLUS any free device indices a fresh
    scan can open right now. Returns a list of
    {source, label, active} dicts, active=True if a worker is using it.
    """
    active_sources = [w.source for w in workers]
    out = []
    seen = set()
    for s in active_sources:
        out.append({"source": s, "label": camera_label(s), "active": True})
        seen.add(s)
    for s in scan_cameras():
        if s not in seen:
            out.append({"source": s, "label": camera_label(s), "active": False})
            seen.add(s)
    # network cameras from config that may not be reachable to a probe
    for s in getattr(config, "EXTRA_CAMERAS", []):
        if isinstance(s, str) and s not in seen:
            out.append({"source": s, "label": camera_label(s), "active": False})
            seen.add(s)
    return out


# Client-side camera picker (kept as a plain string so its JS braces don't
# collide with the f-string that builds the Cameras settings section).
_CAMERA_PICKER_JS = """
<script>
let camAvail = [], camSel = new Set(), camMax = 2;
async function loadCameras(){
  const st = document.getElementById('camPickStatus');
  if(!st) return;
  st.textContent = 'Detecting cameras\\u2026';
  document.getElementById('camPickList').innerHTML = '';
  document.getElementById('camApplyBtn').disabled = true;
  try{
    const r = await fetch('/api/cameras/available');
    const d = await r.json();
    camAvail = d.cameras || [];
    camMax = d.max || 2;
    const mx = document.getElementById('camMax'); if(mx) mx.textContent = camMax;
    camSel = new Set(camAvail.filter(c => c.active).map(c => String(c.source)));
    renderCameras();
    st.textContent = camAvail.length ? '' : 'No cameras detected. Plug one in and press Rescan.';
  }catch(e){ st.textContent = 'Could not detect cameras.'; }
}
function renderCameras(){
  const list = document.getElementById('camPickList');
  list.innerHTML = '';
  camAvail.forEach(function(c){
    const id = String(c.source);
    const sel = camSel.has(id);
    const row = document.createElement('label');
    row.className = 'campick' + (sel ? ' on' : '');
    row.innerHTML = '<input type="checkbox" ' + (sel ? 'checked' : '') +
      '><span class="cn">' + c.label + '</span><span class="ci">index ' + id + '</span>' +
      (c.active ? '<span class="cactive">in use</span>' : '');
    const cb = row.querySelector('input');
    cb.onchange = function(){
      if(cb.checked){
        if(camSel.size >= camMax){ cb.checked = false; return; }
        camSel.add(id);
      } else { camSel.delete(id); }
      row.classList.toggle('on', cb.checked);
      document.getElementById('camApplyBtn').disabled = camSel.size === 0;
    };
    list.appendChild(row);
  });
  document.getElementById('camApplyBtn').disabled = camSel.size === 0;
}
async function applyCameras(){
  const btn = document.getElementById('camApplyBtn'), msg = document.getElementById('camApplyMsg');
  const sources = [...camSel].map(s => /^\\d+$/.test(s) ? parseInt(s) : s);
  btn.disabled = true; msg.style.color = 'var(--muted)'; msg.textContent = 'Switching cameras\\u2026';
  try{
    const r = await fetch('/api/cameras/select', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({sources: sources})});
    const d = await r.json();
    if(d.ok){ msg.style.color = 'var(--green)'; msg.textContent = 'Cameras updated. Reloading\\u2026';
      setTimeout(function(){ location.reload(); }, 1600); }
    else { msg.style.color = 'var(--red)'; msg.textContent = d.error || 'Failed.'; btn.disabled = false; }
  }catch(e){ msg.style.color = 'var(--red)'; msg.textContent = 'Could not reach server.'; btn.disabled = false; }
}
if(document.getElementById('camPickList')) loadCameras();
</script>
"""


def resolve_cameras():
    """Which cameras to start. If the user has picked a specific set in
    Settings → Cameras (saved as the 'camera_selection' pref), honor that.
    Otherwise auto-scan connected cameras (+ any network URLs) or fall back
    to the manual config list."""
    saved = load_prefs().get("camera_selection")
    if isinstance(saved, list) and saved:
        print(f"[CAPHY] Using saved camera selection: {saved}")
        return saved

    if getattr(config, "AUTO_SCAN_CAMERAS", False):
        cams = scan_cameras()
        cams += list(getattr(config, "EXTRA_CAMERAS", []))
        cams = cams[:getattr(config, "MAX_CAMERAS", 2)]
        if not cams:
            cams = [0]
        print(f"[CAPHY] Auto-scan cameras: {cams}")
        return cams
    return config.CAMERAS


def restart_workers(sources):
    """Stop the running camera workers and start fresh ones on `sources`.
    Used when the user changes which cameras to use in Settings, so the
    change takes effect without restarting the whole app. Releases the old
    devices first (so a newly-selected camera that was busy becomes
    available) then rebuilds."""
    global workers
    old = list(workers)
    for w in old:
        w.running = False
    for w in old:
        try:
            w.join(timeout=3)
        except Exception:
            pass
    workers.clear()
    start_workers(sources)
    print(f"[CAPHY] Cameras restarted on {sources}")


def start_workers(sources):
    # Start DISARMED so launching CAPHY never instantly fires the siren while
    # you're sitting right in front of the webcam. The user arms it when ready
    # (dashboard, phone, or voice); arming then applies the grace window too.
    _set_armed(False)

    # Bring the cloud services (registration, heartbeat, remote commands,
    # WebRTC live streaming) up FIRST, before the slow camera/model init
    # below. Camera startup on Windows can be slow or noisy (DSHOW warnings)
    # and shouldn't hold back the laptop becoming reachable from the phone.
    # WebRTC's CallListener reads the `workers` list lazily (via a lambda),
    # so it's fine that they aren't created yet at this point.
    start_cloud_services()
    # split CPU cores across the cameras so two YOLO models don't oversubscribe
    try:
        import torch, os as _os
        n = max(1, (_os.cpu_count() or 2) // max(1, len(sources)))
        torch.set_num_threads(n)
        print(f"[CAPHY] torch threads/camera: {n}")
    except Exception:
        pass
    for i, src in enumerate(sources):
        workers.append(Worker(src, i))
    # apply any saved custom camera names before the threads (and alert managers) start
    prefs = load_prefs()
    saved = prefs.get("camera_names") or []
    for w in workers:
        if w.cam_id < len(saved) and saved[w.cam_id]:
            w.name = saved[w.cam_id]
    # apply saved detection settings so Settings changes survive a restart
    try:
        db = Database(config.DB_PATH)
        s = db.conn.execute("SELECT * FROM settings WHERE setting_id=1").fetchone()
        db.close()
        t1 = prefs.get("tier1", config.TIER1_MIN_DIST)
        t3 = prefs.get("tier3", config.TIER3_MAX_DIST)
        highest = prefs.get("highest", config.HIGHEST_SECURITY)
        if s:
            for w in workers:
                w.update_settings(s["sensitivity"], s["person_conf"], t1, t3, s["night_vision"], highest)
        print("[CAPHY] applied saved detection settings")
    except Exception as e:
        print(f"[CAPHY] could not apply saved settings ({e})")
    for w in workers:
        w.start()
    print(f"[CAPHY] {len(workers)} camera(s) started.")
    # Cloud services were already started at the top of this function (before
    # camera init); start_cloud_services() is idempotent so we don't call it
    # again here.


_cloud_services_started = False


def start_cloud_services():
    """Idempotently start the background threads that connect this laptop to
    the CAPHY cloud (Firestore): device registration + heartbeat, the remote
    command / QR-pairing listener, and the WebRTC call listener. Safe to
    call more than once (guarded by _cloud_services_started); safe to call
    with no internet (each thread try/excepts and simply keeps retrying)."""
    global _cloud_services_started
    if _cloud_services_started:
        return
    _cloud_services_started = True

    print("[CAPHY] Starting cloud services (registration, heartbeat, remote "
          "commands, WebRTC live streaming)...")
    import threading as _threading
    _threading.Thread(target=_run_cloud_registration_and_heartbeat,
                      daemon=True).start()
    _threading.Thread(target=_run_remote_command_listener,
                      daemon=True).start()
    try:
        from identity import get_device_identity
        from web.webrtc_stream import CallListener, AIORTC_AVAILABLE, AIORTC_IMPORT_ERROR
        if not AIORTC_AVAILABLE:
            print("[CAPHY] WARNING: WebRTC live streaming is OFF - the phone "
                  "won't get live video off your Wi-Fi.")
            print(f"[CAPHY]   Reason: {AIORTC_IMPORT_ERROR}")
            print("[CAPHY]   This usually means aiortc/av didn't install into "
                  "THIS Python. In the SAME venv that runs CAPHY, run:")
            print("[CAPHY]       python -c \"import aiortc, av; print('ok')\"")
            print("[CAPHY]   If that errors, run:  pip install aiortc av")
        else:
            print("[CAPHY] WebRTC live streaming ready (aiortc + av OK).")
        device = get_device_identity()
        CallListener(device["device_id"], lambda: workers).start()
    except Exception as e:
        print(f"[CAPHY] WebRTC live streaming unavailable: {e}")


def _run_cloud_registration_and_heartbeat():
    """Registers this desktop in the Firestore device registry and then
    heartbeats every ~20s with its current LAN IP so a paired phone can (a)
    find the fast local path when on the same WiFi and (b) tell "my laptop
    is online right now" from anywhere over the internet. Additive: if
    Firestore is unreachable this silently no-ops and CAPHY keeps running
    as a pure LAN system."""
    from identity import get_device_identity
    device = get_device_identity()
    device_id = device["device_id"]

    try:
        from storage import device_registry
        device_registry.register_device(device_id, device["device_secret"],
                                        device["hostname"])
        print(f"[CAPHY] Registered in cloud device registry as {device_id}")
    except Exception as e:
        print(f"[CAPHY] Cloud registration skipped (offline?): {e}")

    try:
        from web.webrtc_stream import AIORTC_AVAILABLE as _webrtc_ok
    except Exception:
        _webrtc_ok = False

    first_ok = True
    while True:
        try:
            from storage import device_registry
            # Publish a small live-state snapshot alongside the heartbeat so
            # a phone off the LAN sees the REAL armed/camera/emergency status
            # over the internet, not a guess (see device_registry.heartbeat
            # and the phone's _stateFromCloud).
            try:
                snapshot = {
                    "armed": _is_armed(),
                    "camera_on": any(not w.paused for w in workers) if workers else False,
                    "emergency": bool(_emergency_on),
                    "siren": bool(_siren_manual),
                    "webrtc_available": bool(_webrtc_ok),
                    # Camera list so the phone can show the camera selector +
                    # per-camera online/on state over the internet (mobile
                    # data), not only on the LAN.
                    "cameras": [
                        {"cam": w.cam_id, "name": w.name,
                         "on": not w.paused,
                         "online": bool(w.get_stats().get("online", False))}
                        for w in workers
                    ],
                }
            except Exception:
                snapshot = None
            # Publish current TURN creds (cached ~30 min) so the phone can
            # gather relay candidates from the start - the fix for live
            # video not connecting on mobile data.
            turn_servers = None
            try:
                from web.webrtc_stream import get_ice_servers_cached
                turn_servers, _turn_ok = get_ice_servers_cached()
            except Exception:
                turn_servers = None
            device_registry.heartbeat(device_id, lan_ip=_local_ip(),
                                      lan_port=5000, state=snapshot,
                                      turn_servers=turn_servers)
            if first_ok:
                print("[CAPHY] Cloud heartbeat OK - this laptop is now "
                      "reachable from the phone over the internet.")
                first_ok = False
        except Exception as e:
            first_ok = True   # so recovery is logged too
            print(f"[CAPHY] Heartbeat skipped (offline?): {e}")
        time.sleep(20)


def _run_remote_command_listener():
    """Polls Firestore for commands enqueued by a phone that can't reach the
    laptop's LAN directly (different network / traveling), and for pending
    QR-pairing confirmation requests - this is what lets both remote control
    AND pairing itself work off the laptop's WiFi. Local calls never come
    through here; they hit Flask directly and are much faster."""
    from identity import get_device_identity
    device_id = get_device_identity()["device_id"]
    time.sleep(20)   # let registration land first
    while True:
        try:
            from storage import device_registry
            for cmd in device_registry.pending_commands(device_id):
                _execute_remote_command(device_id, cmd)
            for req in device_registry.pending_pairing_requests_for_device(device_id):
                _execute_confirm_pairing(req)
        except Exception as e:
            print(f"[CAPHY] Remote command poll skipped (offline?): {e}")
        time.sleep(4)


def _execute_remote_command(device_id, cmd):
    """Runs one queued remote command against THIS laptop's own local Flask
    app, so remote and local control share one source of truth."""
    from storage import device_registry
    import requests as _requests

    cmd_id = cmd["id"]
    cmd_type = cmd.get("type")
    try:
        route_map = {
            "arm": ("POST", "/api/arm", {"on": True}),
            "disarm": ("POST", "/api/arm", {"on": False}),
            "camera_on": ("POST", "/api/camera/power", {"on": True}),
            "camera_off": ("POST", "/api/camera/power", {"on": False}),
            "emergency_on": ("POST", "/api/emergency", {"on": True}),
            "emergency_off": ("POST", "/api/emergency", {"on": False}),
            "snapshot": ("POST", "/api/snapshot/0", {}),
            "siren": ("POST", "/api/siren", {}),
        }
        if cmd_type not in route_map:
            device_registry.complete_command(device_id, cmd_id,
                                             {"error": "unknown command"}, ok=False)
            return
        method, path, body = route_map[cmd_type]
        r = _requests.request(method, f"http://127.0.0.1:5000{path}",
                              json=body, timeout=10,
                              headers={"X-CAPHY-Internal": _INTERNAL_TOKEN})
        device_registry.complete_command(
            device_id, cmd_id,
            {"status_code": r.status_code, "body": r.text[:500]},
            ok=r.status_code == 200)
    except Exception as e:
        try:
            device_registry.complete_command(device_id, cmd_id,
                                             {"error": str(e)}, ok=False)
        except Exception:
            pass


def _execute_confirm_pairing(req):
    """Confirms QR pairing WITHOUT the phone reaching this laptop's LAN - the
    phone wrote a request to Firestore, this laptop picked up only the ones
    meant for it, and calls the same confirm_pairing() the LAN-only route
    used."""
    from storage import device_registry
    req_id = req["id"]
    code = req.get("code", "")
    phone_uid = req.get("requested_by", "")
    if not code or not phone_uid:
        device_registry.complete_pairing_request(
            req_id, {"error": "Missing code or requester"}, ok=False)
        return
    try:
        result = device_registry.confirm_pairing(code, phone_uid)
        device_registry.complete_pairing_request(
            req_id, result, ok=result.get("success", False))
    except Exception as e:
        device_registry.complete_pairing_request(req_id, {"error": str(e)}, ok=False)


def all_logs(limit=200):
    merged = []
    for w in workers:
        merged.extend(w.get_logs())
    merged.sort(key=lambda l: l["t"], reverse=True)
    return merged[:limit]


# ==================== auth (web session + mobile token) ====================
# Bearer tokens issued to the mobile app. Maps token -> user_uid, so a phone
# request carrying "Authorization: Bearer <token>" resolves to the SAME
# account identity the web session uses (session["user_uid"]). Before this,
# API_TOKENS was just a set - it proved *a* phone was logged in, but not
# *whose* phone, which meant every phone effectively shared one identity.
#
# Since B3's rework, the phone authenticates DIRECTLY against Firebase
# (see caphy_app/lib/api.dart login()/signup()/loginWithGoogleToken()) and
# sends its raw Firebase ID token as the Bearer header - it is never
# issued a laptop-specific token by /api/login anymore (that route still
# exists for the web session, which stays cookie-based). So the bearer
# path here MUST verify a real Firebase ID token, not just look it up in
# a table this laptop invented - a laptop-local token would only ever be
# known to whichever phone called /api/login, which nothing calls now.
#
# API_TOKENS is kept as a short-lived verify-result CACHE ONLY (token ->
# (uid, verified_at)) so a phone hammering the live-view endpoint every
# second doesn't re-verify the JWT signature on every single request -
# it is never itself a source of truth for identity.
API_TOKENS = {}   # token -> (user_uid, verified_at)
_TOKEN_CACHE_TTL = 300  # re-verify at most every 5 min


def _token_from_request():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.args.get("token", "")   # allow ?token= for image/stream URLs


def _verify_bearer_uid(token):
    """Resolve a phone's bearer token to a Firebase UID, verifying the
    token's signature/expiry via the Admin SDK. This is the ONLY place a
    phone's claimed identity is trusted - everything downstream (pairing,
    devices, alerts, FCM registration) depends on this being a real,
    cryptographically-verified Firebase ID token, not a client-supplied
    value of any kind."""
    if not token:
        return None

    cached = API_TOKENS.get(token)
    if cached and (time.time() - cached[1]) < _TOKEN_CACHE_TTL:
        return cached[0]

    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        decoded = fb_auth.verify_id_token(token)
        uid = decoded.get("uid")
        if uid:
            API_TOKENS[token] = (uid, time.time())
        return uid
    except Exception:
        API_TOKENS.pop(token, None)
        return None


def _authed():
    return bool(session.get("user_uid")) or _verify_bearer_uid(_token_from_request()) is not None


def _current_uid():
    """
    Resolve the logged-in account's Firebase UID from EITHER a web session
    cookie OR a phone's bearer token - lets a single route (like
    /api/fcm/register) work for both without knowing which one is calling.
    Returns None if neither is present/valid.
    """
    uid = session.get("user_uid")
    if uid:
        return uid
    return _verify_bearer_uid(_token_from_request())


# paths the mobile app reaches with a token instead of a web session
# ("/video" covers both /video_feed (live stream) and /video/<file> (recordings))
_TOKEN_PATHS = ("/api/", "/video", "/snapshot")

# Process-local secret so the laptop's OWN remote-command executor (which
# runs in this same Flask process and calls the laptop's own /api routes
# over loopback to execute a command a phone enqueued in Firestore) can
# authenticate to itself WITHOUT a user session or bearer token. It's
# random per launch, never written to disk, and only ever accepted on a
# loopback (127.0.0.1) connection - so it can't be used to reach the API
# from anywhere but this machine. The real authorization already happened
# at the Firestore layer: the phone proved (via the rules + confirm_pairing
# ownership check) that it owns this device before its command was ever
# picked up.
_INTERNAL_TOKEN = secrets.token_urlsafe(32)


@app.before_request
def guard():
    # Whitelist public endpoints
    if request.endpoint in ("login", "static"):
        return

    # The laptop's own remote-command executor calling itself over loopback.
    if (request.headers.get("X-CAPHY-Internal") == _INTERNAL_TOKEN
            and request.remote_addr in ("127.0.0.1", "::1")):
        return

    # Auth endpoints don't require authentication
    if request.path in ("/api/auth/signup", "/api/auth/google", "/logout",
                        "/auth/google/start", "/auth/google/callback",
                        "/api/auth/google/status"):
        return

    # Token-based API access (for mobile app)
    if request.path.startswith(_TOKEN_PATHS):
        if _authed():
            return
        return jsonify({"error": "unauthorized"}), 401

    # Web dashboard - require session
    if not session.get("user_uid"):
        return redirect(url_for("login"))


# ==================== HTML / CSS ====================
NAV = [("/", "Dashboard"), ("/live", "Live Camera"), ("/alerts", "Alerts"),
       ("/history", "Alert History"), ("/logs", "System Logs"), ("/settings", "Settings")]


def page(title, href, body, subtitle=""):
    st = workers[0].get_stats() if workers else {"armed": True, "online": False}
    online = sum(1 for w in workers if w.get_stats().get("online"))
    cam = f"{online} of {len(workers)} cameras online" if workers else "no cameras"
    try:
        db = Database(config.DB_PATH)
        unread = db.count_alerts(include_dismissed=False)
        db.close()
    except Exception:
        unread = 0
    return render_template("base.html", title=title, page=href, nav=NAV, body=body,
                           subtitle=subtitle, ncam=online, user=session.get("email", "admin"),
                           armed=st.get("armed", True), cam=cam, nalerts=unread)


def tier_pill(t):
    return f'<span class="pill p{t}">Tier {t}</span>'


def ago(ts):
    """Human 'x min ago' from a 'YYYY-MM-DD HH:MM:SS' timestamp string."""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts[-8:]
    s = (datetime.now() - dt).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d hr ago" % (s // 3600)
    return "%d d ago" % (s // 86400)


# ==================== routes ====================
#
# NOTE - unified-account rework:
# Everything below used to validate against a local SQLite password hash
# and invent its own "firebase_uid" strings (f"local_{token}",
# f"google_{sub}") that were never real Firebase UIDs. That was the actual
# root cause of "an account made on desktop can't log into the phone" -
# the phone's FirebaseAuth.instance had never heard of that user, because
# it was never created in Firebase Auth at all, only in this laptop's own
# caphy.db.
#
# Now the web dashboard is a Firebase Auth CLIENT too, exactly like the
# phone: it uses the Firebase Admin SDK (via firebase_auth.py) to create/
# verify users against the SAME Firebase project the phone signs into.
# There is exactly one identity provider - Firebase Auth - for both
# platforms, which is what "centralized backend" in the account/auth
# architecture requires. The local `users` table still exists, but now
# only mirrors Firebase users (display name/email caching) - it is never
# the source of truth for "does this password match this account."


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page - Firebase Auth (email+password + Google Sign-In)."""
    if request.method == "GET":
        google_client_id = getattr(config, "GOOGLE_CLIENT_ID", "")
        return render_template("login_dual_auth.html", google_client_id=google_client_id)

    # POST: email+password login. The web dashboard has no Firebase JS SDK
    # wired in (it's server-rendered), so it verifies credentials via the
    # Firebase Auth REST API's signInWithPassword endpoint using the same
    # Web API key the Android app's google-services.json carries - this
    # calls the exact same Firebase Auth backend the phone's client SDK
    # calls, so "same email+password works on both" is structurally true,
    # not just intended.
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    remember = request.form.get("remember") == "on"

    if not email or not password:
        google_client_id = getattr(config, "GOOGLE_CLIENT_ID", "")
        return render_template("login_dual_auth.html",
                             google_client_id=google_client_id,
                             error="Email and password required"), 400

    result = _firebase_rest_sign_in(email, password)
    if result.get("error"):
        google_client_id = getattr(config, "GOOGLE_CLIENT_ID", "")
        return render_template("login_dual_auth.html",
                             google_client_id=google_client_id,
                             error=result["error"]), 401

    session["user_uid"] = result["uid"]
    session["email"] = email
    session.permanent = remember
    if session.permanent:
        app.permanent_session_lifetime = timedelta(days=30)
    _mirror_local_user(result["uid"], email)
    return redirect(url_for("dashboard"))


def _firebase_web_api_key():
    """The Web API key for this Firebase project - lives in
    android/app/google-services.json (client[0].api_key[0].current_key),
    same project the phone uses. Cached in config for convenience; falls
    back to reading google-services.json directly if not set there."""
    key = getattr(config, "FIREBASE_WEB_API_KEY", "")
    if key:
        return key
    try:
        gs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "caphy_app", "android", "app", "google-services.json")
        with open(gs_path, "r", encoding="utf-8") as f:
            gs = json.load(f)
        return gs["client"][0]["api_key"][0]["current_key"]
    except Exception:
        return ""


def _firebase_rest_sign_in(email, password):
    """Signs in against Firebase Auth's REST API (signInWithPassword) -
    the same backend api.dart's signInWithEmailAndPassword() call hits,
    just reached over plain HTTPS instead of the Dart SDK since this is a
    server-rendered page, not a Firebase client app. Returns {'uid':...}
    on success or {'error': 'human message'} on failure."""
    import urllib.request
    import urllib.error

    api_key = _firebase_web_api_key()
    if not api_key:
        return {"error": "Server misconfigured (no Firebase Web API key)"}

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    body = json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        return {"uid": payload["localId"], "id_token": payload.get("idToken")}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            msg = err.get("error", {}).get("message", "")
        except Exception:
            msg = ""
        friendly = {
            "EMAIL_NOT_FOUND": "No account found for that email.",
            "INVALID_PASSWORD": "Incorrect email or password.",
            "INVALID_LOGIN_CREDENTIALS": "Incorrect email or password.",
            "USER_DISABLED": "This account has been disabled.",
        }.get(msg, "Invalid email or password")
        return {"error": friendly}
    except Exception as e:
        return {"error": f"Could not reach Firebase: {e}"}


def _mirror_local_user(uid, email):
    """Keeps a lightweight local mirror row for display purposes only
    (e.g. the Settings > Users tab). Firebase Auth remains the sole
    source of truth for whether credentials are valid - this table is
    never consulted for that anymore."""
    try:
        db = Database(config.DB_PATH)
        existing = db.conn.execute("SELECT 1 FROM users WHERE firebase_uid=?", (uid,)).fetchone()
        if existing:
            db.conn.execute("UPDATE users SET email=? WHERE firebase_uid=?", (email, uid))
        else:
            db.conn.execute(
                "INSERT INTO users (firebase_uid, email, created_at) VALUES (?, ?, ?)",
                (uid, email, datetime.now().isoformat(timespec="seconds")))
        db.conn.commit()
        db.close()
    except Exception:
        pass


@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    """
    Create a new account (email+password) - via the Firebase Admin SDK,
    the exact same identity store the phone's createUserWithEmailAndPassword()
    writes into. This is what makes "create on desktop, log in on phone"
    actually work: the account did not exist anywhere until this call,
    and after this call it exists in Firebase Auth, reachable from either
    platform.
    """
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"success": False, "error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters"}), 400

    try:
        from firebase_auth import FirebaseAuthManager
        mgr = FirebaseAuthManager()
        uid = mgr.create_user_email_password(email, password)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Signup failed: {e}"}), 500

    session["user_uid"] = uid
    session["email"] = email
    session.permanent = True
    _mirror_local_user(uid, email)

    return jsonify({"success": True, "uid": uid})


@app.route("/api/auth/google", methods=["POST"])
def api_google_auth():
    """
    Handle Google Sign-In from the web dashboard. Verifies the Google ID
    token, then finds-or-creates the matching Firebase Auth user via the
    Admin SDK using Firebase's own account-linking behavior: Firebase
    treats "sign in with Google for email X" as the SAME account as
    "sign in with email+password for email X" whenever the emails match,
    exactly like the phone's loginWithGoogleToken() does via
    signInWithCredential(). One email -> one Firebase account, regardless
    of which method reaches it, on either platform.
    """
    data = request.get_json() or {}
    id_token = data.get("id_token", "")

    if not id_token:
        return jsonify({"success": False, "error": "No token provided"}), 400

    try:
        import urllib.request
        import json as _json

        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
        with urllib.request.urlopen(verify_url, timeout=8) as resp:
            payload = _json.loads(resp.read().decode())

        expected_aud = getattr(config, "GOOGLE_CLIENT_ID", "")
        if expected_aud and payload.get("aud") != expected_aud:
            return jsonify({"success": False, "error": "Token was not issued for this app"}), 401

        email = payload.get("email", "")
        email_verified = payload.get("email_verified") == "true"
        if not email:
            return jsonify({"success": False, "error": "Invalid Google token payload"}), 401

    except Exception as e:
        return jsonify({"success": False, "error": f"Token verification failed: {e}"}), 401

    # Find-or-create the Firebase Auth user for this email, and attach a
    # Google provider link to it via the Admin SDK - this is the server-
    # side equivalent of what signInWithCredential() does on the phone.
    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()

        try:
            user = fb_auth.get_user_by_email(email)
            needs_password = not any(p.provider_id == "password" for p in user.provider_data)
            uid = user.uid
        except fb_auth.UserNotFoundError:
            user = fb_auth.create_user(email=email, email_verified=email_verified)
            uid = user.uid
            needs_password = True

    except Exception as e:
        return jsonify({"success": False, "error": f"Google sign-in failed: {e}"}), 500

    session["user_uid"] = uid
    session["email"] = email
    session.permanent = True
    _mirror_local_user(uid, email)

    next_url = url_for("set_password") if needs_password else url_for("dashboard")
    return jsonify({"success": True, "uid": uid, "email": email, "next": next_url})


# In-memory pending-login state for the desktop's browser-based Google
# Sign-In flow. code_verifier -> {"done": bool, "uid": str|None,
# "email": str|None, "error": str|None}. This is intentionally NOT the
# Flask session - the browser tab that completes the OAuth flow is a
# DIFFERENT process/cookie jar than the pywebview window, so there is no
# shared session between them. The pywebview window instead polls
# /api/auth/google/status?state=... to notice when the browser tab finished.
_PENDING_GOOGLE_LOGINS = {}
_GOOGLE_LOGIN_TTL_SEC = 600


def _prune_pending_google_logins():
    now = time.time()
    dead = [s for s, v in _PENDING_GOOGLE_LOGINS.items()
            if now - v.get("created", 0) > _GOOGLE_LOGIN_TTL_SEC]
    for s in dead:
        _PENDING_GOOGLE_LOGINS.pop(s, None)


def _google_redirect_uri() -> str:
    """
    Hardcoded rather than url_for(_external=True) on purpose: Flask derives
    the external host from the incoming request's Host header, and inside
    pywebview / behind different launch conditions that can silently differ
    (127.0.0.1 vs localhost, trailing slash, etc.) - any mismatch between
    what THIS sends to Google and what's registered in Cloud Console as an
    Authorized redirect URI produces redirect_uri_mismatch even when the
    Console entry "looks right" at a glance. One literal string, used by
    both the auth start and the token exchange, removes that whole class
    of bug. Must match EXACTLY (scheme, host, port, path, no trailing
    slash) an entry in Google Cloud Console → Credentials → this OAuth
    Web client → Authorized redirect URIs.
    """
    return "http://127.0.0.1:5000/auth/google/callback"


@app.route("/auth/google/start")
def auth_google_start():
    """
    Desktop's "Sign in with Google" button hits this - it does NOT try to
    run Google's Sign-In widget inside the pywebview window (that widget
    actively refuses to work properly inside embedded/native-app browser
    views, which is exactly the stuck/duplicate-popup symptom this
    replaces). Instead it opens the user's REAL default system browser to
    Google's OAuth consent screen - the standard, Google-endorsed pattern
    for desktop apps (same approach Slack/Discord/Spotify use).
    """
    _prune_pending_google_logins()
    client_id = getattr(config, "GOOGLE_CLIENT_ID", "")
    if not client_id:
        return "Google Sign-In is not configured (missing GOOGLE_CLIENT_ID).", 500

    state = secrets.token_urlsafe(24)
    _PENDING_GOOGLE_LOGINS[state] = {"done": False, "uid": None, "email": None,
                                      "error": None, "created": time.time()}

    redirect_uri = _google_redirect_uri()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    from urllib.parse import urlencode
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

    import webbrowser
    webbrowser.open(auth_url)

    # The pywebview window shows a "waiting for browser" page and polls
    # /api/auth/google/status - it can't just block here, since opening
    # the browser doesn't block, and this request needs to return so the
    # UI can show that waiting state.
    return render_template("google_waiting.html", state=state)


@app.route("/auth/google/callback")
def auth_google_callback():
    """
    Google redirects here (in the user's real browser, NOT the pywebview
    window) after they approve/deny access. Exchanges the authorization
    code for tokens server-side (using the Web client's secret - never
    exposed to the browser), verifies the identity, and completes the
    exact same find-or-create-Firebase-user logic /api/auth/google uses
    for the phone/embedded-JS path - one shared outcome, three different
    ways of getting there.
    """
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    error = request.args.get("error", "")

    pending = _PENDING_GOOGLE_LOGINS.get(state)
    if pending is None:
        return render_template("google_done.html",
                               ok=False, message="This sign-in link expired or was already used. "
                                                  "Close this tab and try again from the CAPHY app."), 400

    if error:
        pending["done"] = True
        pending["error"] = "Google sign-in was cancelled."
        return render_template("google_done.html", ok=False, message=pending["error"])

    if not code:
        pending["done"] = True
        pending["error"] = "No authorization code received from Google."
        return render_template("google_done.html", ok=False, message=pending["error"])

    try:
        import secrets_config
        import urllib.request
        import urllib.parse

        client_secret = secrets_config.get_google_oauth_client_secret()
        if not client_secret:
            raise RuntimeError("Server is missing the Google OAuth client secret "
                                "(see secrets_config.get_google_oauth_client_secret)")

        token_body = urllib.parse.urlencode({
            "code": code,
            "client_id": getattr(config, "GOOGLE_CLIENT_ID", ""),
            "client_secret": client_secret,
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        }).encode()
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            tokens = json.loads(resp.read().decode())

        id_token = tokens.get("id_token", "")
        if not id_token:
            raise RuntimeError("Google did not return an id_token")

        # Verify + decode the id_token the same way /api/auth/google does
        # for the embedded-JS path (tokeninfo endpoint - simple, no extra
        # deps, and this is a one-time server-side call, not a hot path).
        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
        with urllib.request.urlopen(verify_url, timeout=8) as resp:
            payload = json.loads(resp.read().decode())

        expected_aud = getattr(config, "GOOGLE_CLIENT_ID", "")
        if expected_aud and payload.get("aud") != expected_aud:
            raise RuntimeError("Token was not issued for this app")

        email = payload.get("email", "")
        email_verified = payload.get("email_verified") == "true"
        if not email:
            raise RuntimeError("Invalid Google token payload")

        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()

        try:
            user = fb_auth.get_user_by_email(email)
            uid = user.uid
        except fb_auth.UserNotFoundError:
            user = fb_auth.create_user(email=email, email_verified=email_verified)
            uid = user.uid

        pending["done"] = True
        pending["uid"] = uid
        pending["email"] = email
        _mirror_local_user(uid, email)

        return render_template("google_done.html", ok=True,
                               message=f"Signed in as {email}. You can close this tab and "
                                       f"return to CAPHY.")

    except Exception as e:
        pending["done"] = True
        pending["error"] = f"Google sign-in failed: {e}"
        return render_template("google_done.html", ok=False, message=pending["error"]), 500


@app.route("/api/auth/google/status")
def api_auth_google_status():
    """
    Polled by the pywebview window's waiting page (google_waiting.html)
    every ~1.5s to find out when the browser-tab sign-in finished. Once
    done, this sets the SAME session cookie the rest of the app checks
    (session["user_uid"]) - the poll request comes from the pywebview
    window itself, so its response's Set-Cookie lands in the right place.
    """
    state = request.args.get("state", "")
    pending = _PENDING_GOOGLE_LOGINS.get(state)
    if pending is None:
        return jsonify({"done": True, "error": "Sign-in session expired."})

    if not pending["done"]:
        return jsonify({"done": False})

    _PENDING_GOOGLE_LOGINS.pop(state, None)

    if pending["error"]:
        return jsonify({"done": True, "error": pending["error"]})

    session["user_uid"] = pending["uid"]
    session["email"] = pending["email"]
    session.permanent = True
    return jsonify({"done": True, "next": url_for("dashboard")})


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    """
    Ask a Google-signed-in user to add a password, so email+password also
    works later - sets the password directly on the Firebase Auth user
    via the Admin SDK, so it works identically from the phone afterward.
    """
    if not session.get("user_uid"):
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("set_password.html", email=session.get("email", ""), error="")

    password = request.form.get("password", "")
    password_confirm = request.form.get("password_confirm", "")

    if not password or len(password) < 6:
        return render_template("set_password.html", email=session.get("email", ""),
                             error="Password must be at least 6 characters")
    if password != password_confirm:
        return render_template("set_password.html", email=session.get("email", ""),
                             error="Passwords do not match")

    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        fb_auth.update_user(session["user_uid"], password=password)
    except Exception as e:
        return render_template("set_password.html", email=session.get("email", ""),
                             error=f"Could not set password: {e}")

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/console")
def console():
    """Optional single-screen console (not the default). The default home is
    the classic multi-page dashboard at '/'. Kept here in case it's wanted
    later; History / Full Logs / Settings / Live open as popups."""
    cams = [{"cam": w.cam_id, "name": w.name} for w in workers] or [{"cam": 0, "name": "Camera 0"}]
    return render_template("console.html", user=session.get("email", "admin"),
                           cams_json=json.dumps(cams))


@app.route("/api/dashboard")
def api_dashboard():
    """Top-bar stats + 24h threat chart for the one-screen console."""
    db = Database(config.DB_PATH)
    total = db.count_alerts()
    today = db.conn.execute("SELECT COUNT(*) c FROM alerts "
                            "WHERE date(timestamp)=date('now','localtime')").fetchone()["c"]
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    hours = {int(r["h"]): r["c"] for r in db.conn.execute(
        "SELECT strftime('%H', timestamp) h, COUNT(*) c FROM alerts "
        "WHERE timestamp >= datetime('now','-1 day') GROUP BY h")}
    db.close()
    cur = max([w.get_stats().get("tier", 0) for w in workers] or [0])
    online = sum(1 for w in workers if w.get_stats().get("online"))
    ncam = len(workers)
    cur_dist = "-"
    for w in workers:
        s = w.get_stats()
        if s.get("tier", 0) == cur and cur:
            cur_dist = s.get("distance", "-")
            break
    return jsonify({
        "total": total, "today": today, "pending": pending,
        "online": online, "ncam": ncam,
        "threat": ("Tier " + str(cur)) if cur else "Clear",
        "threat_foot": (f"person · {cur_dist} m") if cur else "no active threat",
        "hours": hours,
    })


@app.route("/api/health")
def api_health():
    """CPU / mem / disk / fps + module status for the console health panel."""
    if psutil:
        cpu = round(psutil.cpu_percent())
        vm = psutil.virtual_memory()
        mem = round(vm.percent)
        mem_txt = f"{vm.used/1e9:.1f} / {vm.total/1e9:.0f} GB"
        disk = round(psutil.disk_usage("/").percent)
    else:
        cpu = mem = disk = 0
        mem_txt = "n/a"
    st = workers[0].get_stats() if workers else {}
    fps = st.get("fps", 0) or 0
    db = Database(config.DB_PATH)
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    db.close()
    yolo_ok = any(w.yolo_ok for w in workers)
    nv_on = any(getattr(w, "nv", None) and w.nv.enabled for w in workers)
    fcm_on = os.path.exists(config.FIREBASE_KEY)
    G, T, M, R = "#3fb98a", "#5b8dff", "#8d9bb5", "#e5544e"
    modules = [
        {"name": "OpenCV capture", "status": "running", "color": G},
        {"name": "YOLOv8-nano", "status": "running" if yolo_ok else "off", "color": G if yolo_ok else R},
        {"name": "Tier engine", "status": "running", "color": G},
        {"name": "Night vision (CLAHE)", "status": "engaged" if nv_on else "standby", "color": T if nv_on else M},
        {"name": "Flask API / MJPEG", "status": "running", "color": G},
        {"name": "SQLite", "status": "ok", "color": T},
        {"name": "Cloud sync", "status": (f"offline · queue {pending}" if pending else "synced"), "color": R if pending else G},
        {"name": "Firebase FCM", "status": "connected" if fcm_on else "off", "color": T if fcm_on else M},
    ]
    return jsonify({"cpu": cpu, "mem": mem, "mem_txt": mem_txt, "disk": disk,
                    "fps": fps, "modules": modules})


@app.route("/")
def dashboard():
    db = Database(config.DB_PATH)
    total = db.count_alerts()
    today = db.conn.execute("SELECT COUNT(*) c FROM alerts "
                            "WHERE date(timestamp)=date('now','localtime')").fetchone()["c"]
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    recent = db.recent_alerts(5)
    hours = {int(r["h"]): r["c"] for r in db.conn.execute(
        "SELECT strftime('%H', timestamp) h, COUNT(*) c FROM alerts "
        "WHERE timestamp >= datetime('now','-1 day') GROUP BY h")}
    db.close()

    cur = max([w.get_stats().get("tier", 0) for w in workers] or [0])
    online = sum(1 for w in workers if w.get_stats().get("online"))
    ncam = len(workers)
    # distance of the highest-tier active camera (for the Current Threat card sub-line)
    cur_dist = "-"
    for w in workers:
        s = w.get_stats()
        if s.get("tier", 0) == cur and cur:
            cur_dist = s.get("distance", "-")
            break

    # ---- KPI cards ----
    threat_txt = ("Tier " + str(cur)) if cur else "Clear"
    threat_foot = ("person &middot; %s m" % cur_dist) if cur else "no active threat"
    cam_foot = "all online" if online == ncam and ncam else ("%d online" % online)
    cards = f"""
    <div class="cards">
      <div class="card a1"><div class="k">Total Alerts</div><div class="v">{total}</div>
        <div class="foot"><span class="d"></span>+{today} today</div></div>
      <div class="card a4"><div class="k">Active Cameras</div><div class="v">{online} / {ncam}</div>
        <div class="foot"><span class="d"></span>{cam_foot}</div></div>
      <div class="card {'a2' if cur else 'a1'}"><div class="k">Current Threat</div><div class="v">{threat_txt}</div>
        <div class="foot"><span class="d"></span>{threat_foot}</div></div>
      <div class="card a3"><div class="k">Pending Sync</div><div class="v">{pending}</div>
        <div class="foot"><span class="d"></span>offline queue</div></div>
    </div>"""

    # ---- live feed panel (all active cameras, compact) ----
    if workers:
        tiles = ""
        for w in workers:
            tiles += (f'<div class="feedtile"><div class="feedwrap">'
                      f'<img class="feed" src="/video_feed/{w.cam_id}">'
                      f'<span class="pill p3 rec">&#9679; REC</span></div>'
                      f'<div class="feedcap">{w.name}</div></div>')
        feed = f'<div class="feedgrid">{tiles}</div>'
        feed_title = " &mdash; " + workers[0].name
    else:
        feed = '<div style="color:var(--dim)">No cameras running</div>'
        feed_title = ""

    # ---- recent alerts list ----
    if recent:
        alerts_html = '<div class="alist">'
        for a in recent:
            t = a["tier"] or 1
            event = "Person confirmed" if a["tier"] else "Movement (no person)"
            alerts_html += (
                f'<div class="arow t{t}">'
                f'<div class="av"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/>'
                f'<path d="M4 21v-1a6 6 0 0 1 12 0v1"/></svg></div>'
                f'<div class="txt"><div class="tt">{tier_pill(t)}</div>'
                f'<div class="ss">{event} &middot; {a["distance_m"]} m</div></div>'
                f'<div class="tm">{ago(a["timestamp"])}</div></div>')
        alerts_html += "</div>"
    else:
        alerts_html = '<div style="color:var(--dim);font-size:13px">No alerts yet</div>'

    # ---- 24h threat chart (hover a bar to see the hour + alert count) ----
    maxh = max(hours.values()) if hours else 1
    bars = ""
    for h in range(24):
        c = hours.get(h, 0)
        cls = "bar hot" if (c and c >= maxh) else ("bar warm" if c >= maxh * 0.5 and c else "bar")
        pct = int(100 * c / maxh) if maxh else 0
        plural = "" if c == 1 else "s"
        bars += (f'<div class="{cls}" style="--h:{max(pct,4)}%">'
                 f'<span class="bartip"><b>{h:02d}:00</b> &middot; {c} alert{plural}</span></div>')

    body = f"""
    {cards}
    <div class="grid2">
      <div class="panel">
        <div class="ph"><h2>Live Feed{feed_title}</h2><a class="link" href="/live">Open Live Camera &rarr;</a></div>
        {feed}
      </div>
      <div class="panel">
        <div class="ph"><h2>Recent Alerts</h2><a class="link" href="/history">View All</a></div>
        {alerts_html}
      </div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="ph"><h2>Threat Activity &mdash; last 24h</h2></div>
      <div class="chart">{bars}</div>
      <div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    </div>"""
    return page("Dashboard", "/", body,
                subtitle="Real-time overview &middot; updated just now")


@app.route("/live")
def live():
    main_id = workers[0].cam_id if workers else 0
    main_name = workers[0].name if workers else "No camera"
    cam_items = ""
    for w in workers:
        active = " active" if w.cam_id == main_id else ""
        cam_items += (
            f'<div class="camitem{active}" id="ci{w.cam_id}" onclick="selectCam({w.cam_id})">'
            f'<img class="camthumb" src="/video_feed/{w.cam_id}">'
            f'<div class="cimeta"><div><div class="cn">{w.name}</div>'
            f'<div class="cs" id="cs{w.cam_id}">Online</div></div>'
            f'<span class="dot" id="cd{w.cam_id}" style="background:var(--green)"></span></div></div>')
    if not cam_items:
        cam_items = '<div style="color:var(--dim);font-size:13px">No cameras running</div>'

    body = """
    <div class="livewrap">
      <div class="panel maincam">
        <div class="ph"><h2 id="mainName">__MAIN_NAME__</h2><span class="pill p3 rec">&#9679; REC</span></div>
        <div class="feedwrap live" id="mainwrap">
          <img class="feed" id="mainFeed" src="/video_feed/__MAIN_ID__">
          <div class="detbox">
            <div class="dh">DETECTION</div>
            <div class="dr"><span>Factor 1 &middot; Motion</span><span class="dot" id="dm" style="background:var(--dim)"></span></div>
            <div class="dr"><span>Factor 2 &middot; Person</span><span class="dot" id="dp" style="background:var(--dim)"></span></div>
            <div class="dd">Est. distance <span id="ddist">-</span> m</div>
          </div>
          <!-- fullscreen: just the video. Only an Exit button, which auto-hides. -->
          <button class="fsexit" id="fsexit" onclick="fsMain()">&#10005;&nbsp;Exit</button>
        </div>

        <div class="statebar" id="statebar">
          <span class="sb" id="sbArm">System &mdash;</span>
          <span class="sb" id="sbCam">Camera &mdash;</span>
          <span class="sb" id="sbNv">Night vision &mdash;</span>
          <span class="sb" id="sbSiren">Siren &mdash;</span>
          <span class="sb" id="sbEmg">Emergency &mdash;</span>
        </div>

        <div class="ctrlhead">
          <span>Controls</span>
          <button class="collapse" id="collapseBtn" onclick="toggleControls()">Hide &#9650;</button>
        </div>
        <div class="controls" id="controls">
          <button class="ctrlbtn primary" id="armBtn" onclick="toggleArm()">
            <svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg>
            <span id="armLabel">Arm</span></button>
          <button class="ctrlbtn icon" id="camBtn" onclick="toggleCam()" title="Camera">
            <svg viewBox="0 0 24 24"><path d="M16 16v1a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2m5.66 0H14a2 2 0 0 1 2 2v3.34l1 1L23 7v10"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          </button>
          <button class="ctrlbtn icon" onclick="snap()" title="Snapshot">
            <svg viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
          </button>
          <button class="ctrlbtn icon" id="recBtn" onclick="toggleRec()" title="Record">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/></svg>
          </button>
          <button class="ctrlbtn icon" id="nvBtn" onclick="toggleNV()" title="Night vision">
            <svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
          </button>
          <button class="ctrlbtn icon" onclick="fsMain()" title="Fullscreen">
            <svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
          </button>
          <div class="ctrlspacer"></div>
          <button class="ctrlbtn icon emg" id="emgBtn" onclick="toggleEmg()" title="Emergency">
            <svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          </button>
          <button class="ctrlbtn siren" id="sirenBtn" onclick="siren()">Trigger Siren</button>
        </div>
      </div>
      <div class="rightcol">
        <div class="panel">
          <h2 style="margin-bottom:14px">Cameras</h2>
          <div class="camlist">__CAM_ITEMS__</div>
        </div>
        <div class="panel">
          <h2 style="margin-bottom:14px">Two-Factor Log</h2>
          <div class="tflog" id="tflog"></div>
        </div>
      </div>
    </div>
    <script>
    let sel = __MAIN_ID__;
    function selectCam(id){
      sel = id;
      document.getElementById('mainFeed').src = '/video_feed/'+id;
      document.getElementById('mainName').textContent = document.querySelector('#ci'+id+' .cn').textContent;
      document.querySelectorAll('.camitem').forEach(function(e){ e.classList.remove('active'); });
      const it = document.getElementById('ci'+id); if(it) it.classList.add('active');
    }
    function fsMain(){
      const el = document.getElementById('mainwrap');
      if(document.fullscreenElement){ document.exitFullscreen(); }
      else if(el.requestFullscreen){ el.requestFullscreen(); }
      else if(el.webkitRequestFullscreen){ el.webkitRequestFullscreen(); }
    }
    async function snap(){
      try{ const r=await fetch('/api/snapshot/'+sel,{method:'POST'}); const j=await r.json();
        if(j.ok){ toast('Snapshot saved'); window.open(j.url,'_blank'); }
        else { toast('Snapshot failed - is the camera on?'); }
      }catch(e){ toast('Snapshot failed'); }
    }
    async function toggleNV(){
      try{ const r=await fetch('/api/nightvision/'+sel,{method:'POST'}); const j=await r.json();
        document.getElementById('nvBtn').classList.toggle('active', j.on); }catch(e){}
    }
    async function siren(){
      try{ const r=await fetch('/api/siren',{method:'POST'}); const j=await r.json();
        document.getElementById('sirenBtn').textContent = j.on ? 'Stop Siren' : 'Trigger Siren'; }catch(e){}
    }
    let recOn=false;
    async function toggleRec(){
      try{
        const r=await fetch('/api/record/'+sel,{method:'POST'}); const j=await r.json();
        recOn = j.recording;
        const b=document.getElementById('recBtn');
        b.classList.toggle('active', recOn);
        b.title = recOn ? 'Stop Recording' : 'Record';
        toast(recOn ? 'Recording started' : 'Recording saved to Videos/CAPHY');
      }catch(e){}
    }
    function toast(msg){
      let t=document.getElementById('webtoast');
      if(!t){ t=document.createElement('div'); t.id='webtoast'; document.body.appendChild(t); }
      t.textContent=msg; t.className='show';
      clearTimeout(window._tt); window._tt=setTimeout(function(){ t.className=''; },2200);
    }

    // ---- state-aware controls ----
    let ST = {};
    function setPill(el, label, on, onText, offText){
      el.textContent = label + ' ' + (on ? onText : offText);
      el.classList.toggle('on', !!on);
    }
    async function refreshState(){
      try{
        const r = await fetch('/api/state'); ST = await r.json();
        setPill(document.getElementById('sbArm'),   'System',       ST.armed,        'ARMED','DISARMED');
        setPill(document.getElementById('sbCam'),   'Camera',       ST.camera_on,    'ON','OFF');
        setPill(document.getElementById('sbNv'),    'Night vision', ST.night_vision, 'ON','OFF');
        setPill(document.getElementById('sbSiren'), 'Siren',        ST.siren,        'ON','OFF');
        setPill(document.getElementById('sbEmg'),   'Emergency',    ST.emergency,    'ACTIVE','OFF');

        const armBtn=document.getElementById('armBtn');
        document.getElementById('armLabel').textContent = ST.armed ? 'Disarm' : 'Arm';
        armBtn.classList.toggle('active', ST.armed);

        const camBtn=document.getElementById('camBtn');
        camBtn.title = ST.camera_on ? 'Camera Off' : 'Camera On';
        camBtn.classList.toggle('active', !ST.camera_on);

        document.getElementById('nvBtn').classList.toggle('active', ST.night_vision);
        document.getElementById('sirenBtn').textContent = ST.siren ? 'Stop Siren' : 'Trigger Siren';
        document.getElementById('sirenBtn').classList.toggle('active', ST.siren);

        const emgBtn=document.getElementById('emgBtn');
        emgBtn.title = ST.emergency ? 'Cancel Emergency' : 'Emergency';
        emgBtn.classList.toggle('active', ST.emergency);
      }catch(e){}
    }
    async function toggleArm(){
      try{ await fetch('/api/arm',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: !ST.armed})}); }catch(e){}
      refreshState();
    }
    async function toggleCam(){
      try{ await fetch('/api/camera/power',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: !ST.camera_on})}); }catch(e){}
      refreshState();
    }
    async function toggleEmg(){
      if(!ST.emergency){
        const ok = await openModal({
          title: 'Activate Emergency Mode?',
          message: 'This forces the camera on, arms the system, sounds the siren, and sends a push alert immediately to everyone on the account.',
          confirmText: 'Activate Emergency',
          danger: true,
          icon: '<svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
        });
        if(!ok) return;
      }
      try{ await fetch('/api/emergency',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: !ST.emergency})}); }catch(e){}
      await refreshState();
      toast(ST.emergency ? 'Emergency mode activated' : 'Emergency mode cancelled');
    }

    async function poll(){
      try{
        const r=await fetch('/api/stats'); const data=await r.json();
        data.forEach(function(s){
          const camOn = (s.camera_on !== false);
          const cd=document.getElementById('cd'+s.cam);
          if(cd) cd.style.background = !camOn ? 'var(--orange)' : (s.online?'var(--green)':'var(--dim)');
          const cs=document.getElementById('cs'+s.cam);
          // "Camera Off" and "Offline" are different problems - say which
          if(cs) cs.textContent = !camOn ? 'Camera Off' : (s.online?'Online':'Offline');
          if(s.cam===sel){
            document.getElementById('dm').style.background = s.motion?'var(--green)':'var(--dim)';
            document.getElementById('dp').style.background = s.person?'var(--green)':'var(--dim)';
            document.getElementById('ddist').textContent = s.distance;
          }
        });
        const lr=await fetch('/api/logs'); const logs=await lr.json();
        const box=document.getElementById('tflog');
        box.innerHTML = logs.map(function(l){
          const col = l.level==='ALERT' ? 'var(--orange)' : (l.level==='DETECT' ? 'var(--teal2)' : 'var(--muted)');
          return '<div class="lg"><span class="tt">'+l.t+'</span><span style="color:'+col+'">'+l.msg+'</span></div>';
        }).join('');
      }catch(e){}
    }
    // ---- collapse the control bar ----
    function toggleControls(){
      const c=document.getElementById('controls'); const b=document.getElementById('collapseBtn');
      const hidden = c.classList.toggle('hidden');
      b.innerHTML = hidden ? 'Show &#9660;' : 'Hide &#9650;';
    }
    // ---- fullscreen: only an Exit button that auto-hides; tap video to reveal ----
    let fsHideTimer=null;
    function showFsExit(){
      const b=document.getElementById('fsexit'); b.classList.remove('gone');
      clearTimeout(fsHideTimer);
      if(document.fullscreenElement) fsHideTimer=setTimeout(function(){ b.classList.add('gone'); },3000);
    }
    document.getElementById('mainwrap').addEventListener('click', function(e){
      if(document.fullscreenElement && e.target.id==='mainFeed') showFsExit();
    });
    document.addEventListener('fullscreenchange', function(){
      const wrap=document.getElementById('mainwrap');
      wrap.classList.toggle('isfs', !!document.fullscreenElement);
      if(document.fullscreenElement){ showFsExit(); }
      else { clearTimeout(fsHideTimer); document.getElementById('fsexit').classList.remove('gone'); }
    });

    setInterval(poll,1500); poll();
    setInterval(refreshState,2000); refreshState();
    </script>
    <style>
      .statebar{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 6px}
      .statebar .sb{font-size:11px;letter-spacing:.4px;padding:5px 11px;border-radius:20px;
        border:1px solid var(--line);color:var(--dim);background:var(--panel);font-weight:600}
      .statebar .sb.on{color:var(--teal2);border-color:var(--teal2);background:rgba(63,215,196,.08)}
      #sbEmg.on{color:var(--red);border-color:var(--red);background:rgba(229,72,77,.1)}
      #sbCam:not(.on){color:var(--orange);border-color:var(--orange)}

      .ctrlhead{display:flex;justify-content:space-between;align-items:center;margin:8px 2px 8px}
      .ctrlhead span{font-size:11px;letter-spacing:1.5px;color:var(--dim);text-transform:uppercase}
      .collapse{background:none;border:none;color:var(--muted);font-size:11.5px;cursor:pointer}
      .collapse:hover{color:var(--teal2)}

      .controls{display:flex;flex-wrap:wrap;align-items:center;gap:9px;
        overflow:hidden;transition:max-height .25s ease,opacity .2s;max-height:400px}
      .controls.hidden{max-height:0;opacity:0;margin:0}
      .ctrlspacer{flex:1 1 auto;min-width:8px}
      .ctrlbtn{display:flex;align-items:center;justify-content:center;gap:8px;
        background:var(--panel);border:1px solid var(--line);color:var(--text);
        border-radius:10px;padding:12px 16px;font-size:13px;font-weight:600;
        letter-spacing:.2px;cursor:pointer;transition:.15s;text-align:center}
      .ctrlbtn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;flex-shrink:0}
      .ctrlbtn.icon{padding:12px;width:46px;height:46px}
      .ctrlbtn:hover{border-color:var(--teal2);color:var(--teal2)}
      .ctrlbtn.active{background:var(--teal);color:#04110e;border-color:var(--teal)}
      .ctrlbtn.primary{border-color:var(--teal2);color:var(--teal2)}
      .ctrlbtn.emg{border-color:var(--red);color:var(--red)}
      .ctrlbtn.siren{background:var(--red);color:#fff;border-color:var(--red)}
      .ctrlbtn.siren:hover{filter:brightness(1.1);color:#fff}
      .ctrlbtn.siren.active,.ctrlbtn.emg.active{background:var(--red);color:#fff;border-color:var(--red)}

      /* fullscreen: only an Exit button, top-right, auto-hiding */
      .fsexit{position:fixed;top:18px;right:18px;display:none;align-items:center;
        gap:6px;padding:10px 16px;border-radius:24px;background:rgba(10,16,22,.78);
        border:1px solid var(--line);color:#fff;cursor:pointer;font-size:13px;
        font-weight:600;z-index:2147483647;transition:opacity .25s}
      .fsexit.gone{opacity:0;pointer-events:none}
      .fsexit:hover{background:var(--red);border-color:var(--red)}
      .feedwrap.isfs .fsexit{display:inline-flex}
      .feedwrap.isfs{background:#000}
      .feedwrap.isfs .feed{object-fit:contain;height:100vh;width:100vw}

      #webtoast{position:fixed;top:20px;left:50%;transform:translateX(-50%) translateY(-20px);
        background:var(--panel);border:1px solid var(--teal2);color:var(--text);
        padding:11px 18px;border-radius:11px;font-size:13px;font-weight:600;z-index:9999;
        opacity:0;transition:.25s;pointer-events:none;box-shadow:0 6px 18px rgba(0,0,0,.4)}
      #webtoast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    </style>
    """.replace("__CAM_ITEMS__", cam_items).replace("__MAIN_NAME__", main_name).replace("__MAIN_ID__", str(main_id))
    return page("Live Camera", "/live", body,
                subtitle="Two-factor validation active")


@app.route("/api/stats")
def api_stats():
    out = []
    for w in workers:
        st = w.get_stats()
        out.append({"cam": w.cam_id, "name": w.name, "online": st.get("online", False),
                    "motion": st.get("motion", False), "person": st.get("person", False),
                    "tier": st.get("tier", 0), "distance": st.get("distance", "-"),
                    "fps": st.get("fps", "-")})
    return jsonify(out)


@app.route("/api/logs")
def api_logs():
    try:
        n = min(int(request.args.get("n", 12)), 200)
    except ValueError:
        n = 12
    return jsonify([{"t": l["t"], "level": l["level"], "mod": l["mod"], "msg": l["msg"]}
                    for l in all_logs(n)])


def _save_snapshot(cam):
    """Save the current frame of a camera into Pictures/CAPHY.
    Returns the filename on success, else None. Logs the full path so the
    file is easy to find (and failures are visible in the console)."""
    if not (0 <= cam < len(workers)):
        return None
    w = workers[cam]
    jpg = w.get_jpeg()
    if not jpg:
        w._log("WARN", "snapshot", "no frame available yet - camera starting or off")
        return None
    try:
        os.makedirs(config.CAPTURES_DIR, exist_ok=True)
        name = f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}_cam{cam}.jpg"
        full = os.path.abspath(os.path.join(config.CAPTURES_DIR, name))
        with open(full, "wb") as f:
            f.write(jpg)
        w._log("INFO", "snapshot", f"saved {full}")
        return name
    except Exception as e:
        w._log("WARN", "snapshot", f"save failed: {e}")
        return None


@app.route("/api/snapshot/<int:cam>", methods=["POST"])
def api_snapshot(cam):
    name = _save_snapshot(cam)
    return jsonify({"ok": bool(name), "name": name,
                    "url": (f"/snapshot/{name}" if name else None)})


@app.route("/api/nightvision/<int:cam>", methods=["POST"])
def api_nightvision(cam):
    if 0 <= cam < len(workers):
        w = workers[cam]
        w.nv.enabled = not w.nv.enabled
        w._log("INFO", "night", "CLAHE " + ("ON" if w.nv.enabled else "OFF") + " (manual)")
        return jsonify({"on": w.nv.enabled})
    return jsonify({"on": False})


@app.route("/api/siren", methods=["POST"])
def api_siren():
    global _siren_manual
    _siren_manual = not _siren_manual
    with _siren_lock:
        _recompute_siren()
    return jsonify({"on": _siren_manual})


@app.route("/api/frame/<int:cam>")
def api_frame(cam):
    """Latest single JPEG frame - the phone polls this for a live view
    (simpler and more reliable over Wi-Fi than an MJPEG stream)."""
    if 0 <= cam < len(workers):
        jpg = workers[cam].get_jpeg()
        if jpg:
            return Response(jpg, mimetype="image/jpeg")
    return Response(status=404)


@app.route("/api/record/<int:cam>", methods=["POST"])
def api_record(cam):
    if 0 <= cam < len(workers):
        w = workers[cam]
        was_recording = w.manual_record
        w.manual_record = not w.manual_record
        w._log("INFO", "record", "manual recording " + ("started" if w.manual_record else "stopped"))
        resp = {"recording": w.manual_record}
        # when STOPPING, return the finished file so the phone can save it to
        # its gallery. The writer closes on the next frame, so poll briefly.
        if was_recording and not w.manual_record:
            for _ in range(20):                 # up to ~2s
                if w.last_record:
                    break
                time.sleep(0.1)
            if w.last_record:
                resp["video"] = w.last_record
                resp["video_url"] = f"/video/{w.last_record}"
                w.last_record = None
        return jsonify(resp)
    return jsonify({"recording": False})


# ==================== mobile app JSON API ====================
def _alert_json(a):
    has_person = a["confidence"] and a["confidence"] > 0
    snap = a["snapshot_path"]
    return {
        "id": a["alert_id"], "tier": a["tier"] or 1,
        "event": "Person detected" if has_person else "Movement (no person)",
        "distance_m": a["distance_m"],
        "confidence": round(a["confidence"], 2) if has_person else None,
        "camera": (a["camera"] if "camera" in a.keys() and a["camera"] else None),
        "timestamp": a["timestamp"],
        "snapshot": ("/snapshot/" + os.path.basename(snap)) if snap else None,
        "has_video": bool(a["video_path"]),
    }


# NOTE: the old /api/login (local password-hash check) and
# /api/auth/mobile-google (manual Google tokeninfo check + locally-invented
# "google_<sub>" uid) routes were removed here. They predate the unified-
# account rework: the phone now authenticates DIRECTLY against Firebase
# (see caphy_app/lib/api.dart), the same identity provider the web
# dashboard uses, so there is exactly ONE place an account is created or
# verified - Firebase Auth - instead of two different code paths that
# could (and did) disagree about what uid an email maps to. Any client
# still calling these paths should switch to signing in with the Firebase
# SDK directly and sending the resulting ID token as a Bearer header.


@app.route("/api/fcm/register", methods=["POST"])
def api_fcm_register():
    """
    Phone calls this after login (and again whenever its FCM token refreshes)
    to subscribe itself to push alerts from THIS laptop only. This is the
    step that makes B5 real - without it, send_alert() has no phone to
    reach because nothing has subscribed to the topic yet.

    Body: {"fcm_token": "..."}
    Auth: requires a logged-in session/token (see the `guard()` before_request) -
    the account is read from that, never trusted from the request body, so a
    phone can only subscribe itself to ITS OWN account's alerts.
    """
    data = request.get_json() or {}
    fcm_token = data.get("fcm_token", "")

    if not fcm_token:
        return jsonify({"success": False, "error": "fcm_token required"}), 400

    user_uid = _current_uid()
    if not user_uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from identity import get_device_identity
        from storage.firebase_push import FirebasePushSender

        device_id = get_device_identity()["device_id"]
        sender = FirebasePushSender(user_uid, device_id)
        ok = sender.subscribe_device_to_topic(fcm_token)

        return jsonify({"success": ok, "device_id": device_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/fcm/unregister", methods=["POST"])
def api_fcm_unregister():
    """Phone calls this on logout so it stops receiving this laptop's alerts."""
    data = request.get_json() or {}
    fcm_token = data.get("fcm_token", "")

    if not fcm_token:
        return jsonify({"success": False, "error": "fcm_token required"}), 400

    user_uid = _current_uid()
    if not user_uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from identity import get_device_identity
        from storage.firebase_push import FirebasePushSender

        device_id = get_device_identity()["device_id"]
        sender = FirebasePushSender(user_uid, device_id)
        ok = sender.unsubscribe_device_from_topic(fcm_token)

        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _local_ip():
    """Best-effort LAN IP of this laptop (the address the phone must reach
    over WiFi to talk to the local Flask server for camera/live/arm-disarm).
    Doesn't actually send anything - opening a UDP socket to a public IP is
    just a trick to make the OS pick the right outbound interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.route("/api/pairing/generate")
def api_pairing_generate():
    """
    Web dashboard (Settings > Connect Device) calls this to get a fresh QR
    payload. Requires an active web session - only someone already signed
    in on THIS laptop can generate a code for it, which is what stops a
    stranger from pairing their phone to your house.

    The pairing code now lives in Firestore (storage/device_registry.py),
    not just this process's memory - this laptop's own account is also
    registered as this device's owner_uid at this point (register_device),
    so "who owns this desktop" is a cloud fact, not something only this
    Flask process remembers.

    Returns the laptop's local IP/port (fast-path hint for when the phone
    is on the same WiFi) + device_id + a one-time pairing code the phone
    confirms through ITS OWN backend call to /api/pairing/confirm (which
    may be this same laptop, if reachable, or - since that route only
    touches Firestore - honestly any CAPHY backend process reachable to
    it; in practice the phone calls the address in this QR).
    """
    user_uid = session.get("user_uid")
    if not user_uid:
        return jsonify({"error": "unauthorized"}), 401

    from identity import get_device_identity
    from storage import device_registry
    ident = get_device_identity()
    device_id = ident["device_id"]

    # Make sure this desktop is registered + claimed by whoever is signed
    # in right now, every time a QR is generated (cheap upsert).
    device_registry.register_device(device_id, ident["device_secret"], ident["hostname"], owner_uid=user_uid)

    pairing = device_registry.create_pairing_code(device_id)

    port = request.environ.get("SERVER_PORT") or getattr(config, "PORT", 5000)
    payload = {
        "v": 1,
        "code": pairing["code"],
        "device_id": device_id,
        "ip": _local_ip(),
        "port": int(port),
    }
    return jsonify({"success": True, "payload": payload, "expires_in": pairing["expires_in"]})


@app.route("/api/pairing/link-token")
def api_pairing_link_token():
    """
    Scan-to-connect: the desktop is already signed into an account, so it can
    hand the phone a one-time way to sign in AS that account just by scanning
    a QR - no email/password/Google screen on the phone at all.

    Requires an active web session (only someone signed in on THIS laptop can
    mint a link for it). Steps:
      1. Claim/refresh this desktop's device doc for the signed-in account.
      2. Mint a Firebase CUSTOM TOKEN for that account (Admin SDK).
      3. Stash it in Firestore under a random nonce (see device_registry.
         store_link_token), which self-expires in a few minutes.
      4. Return just the nonce + device_id - the QR never contains the token
         itself, only the nonce that points at it.

    The phone reads the token by nonce, calls signInWithCustomToken(), and is
    authenticated as this account with zero typing.
    """
    user_uid = session.get("user_uid")
    if not user_uid:
        return jsonify({"error": "unauthorized"}), 401

    from identity import get_device_identity
    from storage import device_registry
    ident = get_device_identity()
    device_id = ident["device_id"]

    # Make sure Firebase Admin is initialized, then mint the custom token.
    try:
        from firebase_admin import auth as _fb_auth
        try:
            import firebase_admin
            firebase_admin.get_app()
        except ValueError:
            from firebase_auth import init_firebase
            init_firebase()
        custom_token = _fb_auth.create_custom_token(user_uid)
        if isinstance(custom_token, (bytes, bytearray)):
            custom_token = custom_token.decode("utf-8")
    except Exception as e:
        return jsonify({"success": False,
                        "error": f"Could not create sign-in token: {e}"}), 500

    # Claim this desktop for the signed-in account so the phone (once signed
    # in as the same account) can immediately see and control it. force-claim
    # (overwrite) - scan-to-connect means "this account owns this laptop from
    # now on", which also heals a device left owned by an earlier test
    # account (that stale owner is exactly what makes the phone's live-video
    # request get PERMISSION_DENIED).
    device_registry.register_device(device_id, ident["device_secret"],
                                    ident["hostname"], owner_uid=user_uid)
    device_registry.claim_device(device_id, user_uid)

    nonce = secrets.token_urlsafe(32)
    device_registry.store_link_token(nonce, custom_token, device_id, user_uid)

    payload = {"v": 2, "nonce": nonce, "device_id": device_id,
               "ip": _local_ip(), "port": int(request.environ.get("SERVER_PORT")
                                               or getattr(config, "PORT", 5000))}
    return jsonify({"success": True, "payload": payload,
                    "expires_in": device_registry.LINK_TOKEN_TTL_SEC})


@app.route("/api/pairing/confirm", methods=["POST"])
def api_pairing_confirm():
    """
    Phone calls this immediately after scanning the QR - ideally directly
    against the laptop's LAN address decoded from the QR (fast, works
    offline-from-internet too, since this route only needs Firestore which
    IS reachable if the phone has internet - but if the laptop happens to
    be unreachable on that LAN address for some reason, pairing still
    works because the actual ownership write happens in Firestore, not on
    this laptop's local state).

    Body: {"code": "..."}. Auth: phone's own bearer token (its
    Firebase-issued identity) - separate from the pairing code, so the
    code alone can never impersonate an account.
    """
    data = request.get_json() or {}
    code = data.get("code", "")
    if not code:
        return jsonify({"success": False, "error": "Missing code"}), 400

    phone_uid = _current_uid()
    if not phone_uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    from storage import device_registry
    result = device_registry.confirm_pairing(code, phone_uid)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/api/devices")
def api_devices():
    """
    Phone (and web) call this to list every CAPHY desktop this account
    owns, with online/offline + last known LAN address for each - this is
    what powers the onboarding screen ("no CAPHY system connected yet")
    and the dashboard's device picker for accounts with more than one
    desktop.
    """
    user_uid = _current_uid()
    if not user_uid:
        return jsonify({"error": "unauthorized"}), 401

    from storage import device_registry
    return jsonify({"success": True, "devices": device_registry.devices_for_owner(user_uid)})


@app.route("/api/alerts")
def api_alerts():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        limit = 50
    db = Database(config.DB_PATH)
    rows = db.conn.execute("SELECT * FROM alerts ORDER BY alert_id DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return jsonify([_alert_json(a) for a in rows])


@app.route("/api/alert/<int:aid>")
def api_alert(aid):
    db = Database(config.DB_PATH)
    a = db.conn.execute("SELECT * FROM alerts WHERE alert_id=?", (aid,)).fetchone()
    db.close()
    if not a:
        return jsonify({"error": "not found"}), 404
    d = _alert_json(a)
    d["video"] = ("/video/" + os.path.basename(a["video_path"])) if a["video_path"] else None
    return jsonify(d)


@app.route("/api/cameras")
def api_cameras():
    return jsonify([{"cam": w.cam_id, "name": w.name,
                     "online": w.get_stats().get("online", False)} for w in workers])


@app.route("/api/cameras/available")
def api_cameras_available():
    """Every camera the user can choose from - active ones plus any free
    device the system can detect right now (see available_cameras())."""
    return jsonify({"cameras": available_cameras(),
                    "max": getattr(config, "MAX_CAMERAS", 2)})


@app.route("/api/cameras/select", methods=["POST"])
def api_cameras_select():
    """Apply a new camera selection: save it and restart the workers on it,
    so the change takes effect immediately (no app restart). Body:
    {"sources": [0, 1]} - integers for device indices, strings for network
    camera URLs."""
    data = request.get_json(silent=True) or {}
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        return jsonify({"ok": False, "error": "Pick at least one camera."}), 400
    maxc = getattr(config, "MAX_CAMERAS", 2)
    if len(sources) > maxc:
        return jsonify({"ok": False,
                        "error": f"You can use at most {maxc} cameras at once."}), 400
    # normalize: keep ints as ints, strings as-is
    norm = []
    for s in sources:
        if isinstance(s, bool):
            continue
        if isinstance(s, int):
            norm.append(s)
        elif isinstance(s, str) and s.strip().isdigit():
            norm.append(int(s.strip()))
        elif isinstance(s, str) and s.strip():
            norm.append(s.strip())
    save_prefs({"camera_selection": norm})
    try:
        restart_workers(norm)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not switch cameras: {e}"}), 500
    return jsonify({"ok": True, "sources": norm})


@app.route("/api/camera/<int:cam>/name", methods=["POST"])
def api_camera_name(cam):
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    if 0 <= cam < len(workers) and name:
        workers[cam].name = name
        save_prefs({"camera_names": [w.name for w in workers]})
        return jsonify({"ok": True, "name": name})
    return jsonify({"ok": False}), 400


_emergency_on = False        # panic override; remembers the state it interrupted
_emergency_prev = None


_arm_grace_until = 0.0     # no alerts until this time (set when arming)


def _set_armed(value):
    global _arm_grace_until
    if value:
        # Arming grace: give the user a few seconds to leave the frame, so
        # arming while standing in view does not instantly fire an alert.
        _arm_grace_until = time.time() + getattr(config, "ARM_GRACE_SEC", 8)
    db = Database(config.DB_PATH)
    try:
        db.conn.execute("UPDATE settings SET armed=? WHERE setting_id=1", (1 if value else 0,))
        db.conn.commit()
    finally:
        db.close()


def _is_armed():
    db = Database(config.DB_PATH)
    try:
        row = db.conn.execute("SELECT armed FROM settings WHERE setting_id=1").fetchone()
        return bool(row["armed"]) if row else True
    finally:
        db.close()


def _set_siren(on):
    global _siren_manual
    _siren_manual = bool(on)
    with _siren_lock:
        _recompute_siren()


def _set_camera(on):
    for w in workers:
        w.paused = not on


def _set_night_vision(on):
    for w in workers:
        if getattr(w, "nv", None):
            w.nv.enabled = bool(on)


def _system_snapshot():
    """Current state, for the reporting intents."""
    st = workers[0].get_stats() if workers else {}
    return {"armed": _is_armed(),
            "camera_on": any(not w.paused for w in workers) if workers else False,
            "night_vision": any(getattr(w, "nv", None) and w.nv.enabled for w in workers),
            "threat_level": (f"Tier {st['tier']}" if st.get("tier") else None)}


def _do_action(action):
    """Perform one intent. Returns a data dict for reporting intents, else {}."""
    global _emergency_on, _emergency_prev

    if action == "arm_system":        _set_armed(True)
    elif action == "disarm_system":   _set_armed(False)
    elif action == "camera_on":       _set_camera(True)
    elif action == "camera_off":      _set_camera(False)
    elif action == "siren_on":        _set_siren(True)
    elif action == "siren_off":       _set_siren(False)
    elif action == "night_vision_on":  _set_night_vision(True)
    elif action == "night_vision_off": _set_night_vision(False)
    elif action == "take_snapshot":   _save_snapshot(0)

    elif action == "emergency_on":
        # remember what we interrupted so emergency_off can restore it
        _emergency_prev = {"armed": _is_armed(),
                           "camera_on": any(not w.paused for w in workers) if workers else True}
        _emergency_on = True
        _set_camera(True)             # overrides camera OFF
        _set_armed(True)
        _set_siren(True)
        for w in workers:
            w.highest = True          # every confirmed person reports as Tier 3
        try:
            owner_uid = _current_device_owner_uid()
            if owner_uid:
                from identity import get_device_identity
                from storage.firebase_push import get_push_sender_for_alert
                sender = get_push_sender_for_alert(owner_uid, get_device_identity()["device_id"])
                if sender:
                    sender.send_alert(title="CAPHY EMERGENCY", body="Emergency mode activated",
                                       data={}, tier=3)
        except Exception as e:
            print(f"[CAPHY] emergency push failed: {e}")

    elif action == "emergency_off":
        _emergency_on = False
        _set_siren(False)
        for w in workers:
            w.highest = config.HIGHEST_SECURITY
        if _emergency_prev:
            _set_armed(_emergency_prev["armed"])
            _set_camera(_emergency_prev["camera_on"])
            _emergency_prev = None

    elif action == "clear_alerts":
        # Dismiss, never delete. The rows stay in the database as evidence and
        # as thesis data - "clear" only means "stop showing me these".
        db = Database(config.DB_PATH)
        try:
            db.dismiss_all_alerts()
        finally:
            db.close()

    elif action == "check_status":
        return _system_snapshot()

    elif action == "threat_level":
        st = workers[0].get_stats() if workers else {}
        return {"tier": (f"Tier {st['tier']}" if st.get("tier") else "none")}

    elif action == "alert_status":
        db = Database(config.DB_PATH)
        try:
            rows = db.conn.execute(
                "SELECT timestamp FROM alerts ORDER BY alert_id DESC LIMIT 10").fetchall()
            return {"alerts": [{"timestamp": r["timestamp"]} for r in rows]}
        finally:
            db.close()

    return {}


# One interpreter for the web/phone path. main.py keeps its own so the two
# confirmation dialogs don't interfere with each other.
_web_interpreter = None


def _run_voice_command(cmd, lang="en"):
    """Interpret a command sent from the phone app, perform it, and return the
    reply as TEXT. action=None means nothing matched (the caller may then hand
    it is reported as "not understood").

    The PC does not listen and does not speak - the phone app does both. It
    runs speech-to-text on the device, POSTs the words here, and speaks the
    reply itself. Phrases come from voice/intents.json.
    """
    global _web_interpreter
    from voice.commands import CommandInterpreter, response_text

    if _web_interpreter is None:
        _web_interpreter = CommandInterpreter()

    r = _web_interpreter.interpret(cmd)
    if r is None:
        # Not a command. There is no chat fallback any more, so say so out
        # loud rather than going silent - otherwise the user cannot tell the
        # difference between "not understood" and "app is broken".
        from voice.commands import config as intents_config
        lang = lang if lang in ("en", "tl") else "en"
        reply = intents_config()["fallback"]["unrecognized"][lang]
        return {"ok": False, "action": None, "reply": reply,
                "message": reply, "lang": lang}

    # the caller may force a language (phone UI toggle); otherwise use detected
    lang = lang if lang in ("en", "tl") else r.lang

    if r.action is None:                       # "Are you sure?" / "Cancelled."
        reply = r.speak
        return {"ok": True, "action": None, "awaiting": r.awaiting,
                "reply": reply, "message": reply, "lang": lang}

    data = _do_action(r.action)
    reply = response_text(r.intent, lang, data) if r.intent else r.speak
    return {"ok": True, "action": r.action, "message": reply, "reply": reply, "lang": lang}



def start_auto_arm_scheduler():
    """Arm at night, disarm in the morning, if 'Auto-arm at night' is on.

    Runs on a background thread and checks once a minute. It only acts on the
    TRANSITION into arm/disarm time, so the user can still manually disarm at
    23:00 and it will not immediately re-arm them.
    """
    def loop():
        last_state = None
        while True:
            try:
                if load_prefs().get("autoarm"):
                    h = datetime.now().hour
                    start = getattr(config, "AUTO_ARM_START_HOUR", 22)
                    end = getattr(config, "AUTO_ARM_END_HOUR", 6)
                    # night window wraps past midnight (22:00 -> 06:00)
                    night = (h >= start or h < end) if start > end else (start <= h < end)
                    if last_state is None:
                        last_state = night          # don't act on first tick
                    elif night != last_state:
                        _set_armed(night)
                        print(f"[CAPHY] Auto-arm: {'ARMED' if night else 'DISARMED'} "
                              f"({datetime.now().strftime('%H:%M')})")
                        last_state = night
                else:
                    last_state = None               # switch off - forget state
            except Exception as e:
                print(f"[CAPHY] Auto-arm error: {e}")
            time.sleep(60)

    t = threading.Thread(target=loop, name="caphy-autoarm", daemon=True)
    t.start()
    return t


@app.route("/api/state")
def api_state():
    """Everything the UIs need to show what is currently ON or OFF."""
    return jsonify({
        "armed": _is_armed(),
        "camera_on": any(not w.paused for w in workers) if workers else False,
        "cameras": [{"cam": w.cam_id, "name": w.name, "on": not w.paused,
                     "online": w.get_stats().get("online", False)} for w in workers],
        "night_vision": any(getattr(w, "nv", None) and w.nv.enabled for w in workers),
        "siren": bool(_siren_manual),
        "emergency": bool(_emergency_on),
        "auto_arm": bool(load_prefs().get("autoarm", False)),
    })


@app.route("/api/arm", methods=["POST"])
def api_arm():
    """Arm or disarm. Send {"on": true/false}, or omit to toggle."""
    data = request.get_json(silent=True) or request.form
    on = data.get("on")
    on = (not _is_armed()) if on is None else (str(on).lower() in ("1", "true", "yes"))
    _set_armed(on)
    return jsonify({"ok": True, "armed": on})


@app.route("/api/camera/power", methods=["POST"])
def api_camera_power():
    """Turn the camera device on or off. Off releases it and stops detection."""
    data = request.get_json(silent=True) or request.form
    on = data.get("on")
    current = any(not w.paused for w in workers) if workers else False
    on = (not current) if on is None else (str(on).lower() in ("1", "true", "yes"))
    _set_camera(on)
    return jsonify({"ok": True, "camera_on": on})


@app.route("/api/emergency", methods=["POST"])
def api_emergency():
    """Panic override on/off."""
    data = request.get_json(silent=True) or request.form
    on = data.get("on")
    on = (not _emergency_on) if on is None else (str(on).lower() in ("1", "true", "yes"))
    _do_action("emergency_on" if on else "emergency_off")
    return jsonify({"ok": True, "emergency": on})


@app.route("/api/alert/<int:aid>/dismiss", methods=["POST"])
def api_dismiss_alert(aid):
    """Acknowledge one alert - hides it, keeps the row in the database."""
    db = Database(config.DB_PATH)
    try:
        db.dismiss_alert(aid)
        return jsonify({"ok": True, "alert_id": aid, "dismissed": True})
    finally:
        db.close()


@app.route("/api/alert/<int:aid>/restore", methods=["POST"])
def api_restore_alert(aid):
    db = Database(config.DB_PATH)
    try:
        db.restore_alert(aid)
        return jsonify({"ok": True, "alert_id": aid, "dismissed": False})
    finally:
        db.close()


@app.route("/api/alerts/dismiss_all", methods=["POST"])
def api_dismiss_all():
    """'Clear alerts' - dismisses every visible alert. Deletes nothing."""
    db = Database(config.DB_PATH)
    try:
        n = db.dismiss_all_alerts()
        return jsonify({"ok": True, "dismissed": n})
    finally:
        db.close()


@app.route("/api/intents")
def api_intents():
    """The command list, straight from voice/intents.json.

    The phone app and the dashboard both render this, so the list a user sees
    can never drift from what CAPHY actually understands.
    """
    from voice.commands import config as intents_config
    cfg = intents_config()
    labels = {
        "arm_system": ("Arm system", "I-arm ang sistema"),
        "disarm_system": ("Disarm system", "I-disarm ang sistema"),
        "camera_on": ("Turn on camera", "Buksan ang camera"),
        "camera_off": ("Turn off camera", "Patayin ang camera"),
        "siren_on": ("Sound the siren", "Patunugin ang sirena"),
        "siren_off": ("Silence the siren", "Patayin ang sirena"),
        "emergency_on": ("Emergency mode", "Emergency mode"),
        "emergency_off": ("Cancel emergency", "Kanselahin ang emergency"),
        "night_vision_on": ("Night vision on", "Buksan ang night vision"),
        "night_vision_off": ("Night vision off", "Patayin ang night vision"),
        "take_snapshot": ("Take a snapshot", "Kumuha ng larawan"),
        "check_status": ("System status", "Status ng sistema"),
        "alert_status": ("Recent alerts", "Mga alert"),
        "clear_alerts": ("Clear alerts", "Burahin ang alerts"),
        "threat_level": ("Threat level", "Antas ng banta"),
        "greeting": ("Say hello", "Batiin si CAPHY"),
    }
    groups = {
        "arm_system": "Security", "disarm_system": "Security",
        "camera_on": "Camera", "camera_off": "Camera",
        "take_snapshot": "Camera",
        "night_vision_on": "Camera", "night_vision_off": "Camera",
        "siren_on": "Alarm", "siren_off": "Alarm",
        "emergency_on": "Alarm", "emergency_off": "Alarm",
        "check_status": "Info", "alert_status": "Info",
        "threat_level": "Info", "clear_alerts": "Info",
        "greeting": "Setup",
    }
    out = []
    for intent in cfg["intents"]:
        iid = intent["id"]
        en_label, tl_label = labels.get(iid, (iid.replace("_", " ").title(),) * 2)
        out.append({
            "id": iid,
            "group": groups.get(iid, "Other"),
            "label": {"en": en_label, "tl": tl_label},
            # what the user should actually SAY
            "say": {"en": intent["phrases"].get("en", []),
                    "tl": intent["phrases"].get("tl", [])},
            "sensitive": bool(intent.get("sensitive")),
        })
    return jsonify({"version": cfg.get("version", 1), "commands": out})


@app.route("/api/voice", methods=["POST"])
def api_voice():
    """The only voice endpoint. Fixed commands, no conversation.

    CAPHY does not chat. Anything that isn't a known command gets the
    "I did not understand" reply from voice/intents.json. That keeps the whole
    system offline - no cloud LLM, no internet needed to control the house.
    """
    data = request.get_json(silent=True) or request.form
    lang = (data.get("lang") or "en").lower()
    cmd = (data.get("command") or data.get("text") or "").strip()
    return jsonify(_run_voice_command(cmd, lang))


@app.route("/video/<name>")
def video_file(name):
    name = os.path.basename(name)
    # recordings live in VIDEOS_DIR now, older ones in CAPTURES_DIR - check both
    for d in (getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR), config.CAPTURES_DIR):
        path = os.path.abspath(os.path.join(d, name))
        if os.path.exists(path):
            return send_file(path)
    abort(404)


@app.route("/api/media/delete", methods=["POST"])
def api_media_delete():
    """Delete a snapshot/recording that a client (the phone app) has already
    saved locally. Used so a recording started from the phone ends up living
    ONLY on the phone, instead of also sitting in Videos/CAPHY on the PC -
    each device keeps what it actually captured, not a duplicate copy."""
    data = request.get_json(silent=True) or {}
    name = os.path.basename((data.get("name") or "").strip())
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    removed = False
    for d in (getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR), config.CAPTURES_DIR):
        path = os.path.abspath(os.path.join(d, name))
        if os.path.exists(path):
            try:
                os.remove(path)
                removed = True
            except Exception:
                pass
    return jsonify({"ok": removed})


@app.route("/api/alerts/feed")
def api_alerts_feed():
    """Visible (unacknowledged) alerts as JSON. Used for the first load and
    as a slow fallback poll - real-time updates come from /api/alerts/stream."""
    db = Database(config.DB_PATH)
    try:
        rows = db.recent_alerts(30)
        out = [{"id": a["alert_id"], "tier": a["tier"] or 0,
                "distance_m": a["distance_m"], "camera": a.get("camera"),
                "timestamp": a["timestamp"]} for a in rows]
        return jsonify({"alerts": out})
    finally:
        db.close()


@app.route("/api/alerts/stream")
def api_alerts_stream():
    """Server-Sent Events: the instant a new alert is confirmed in a Worker's
    detection loop, it's pushed here to every connected /alerts page or phone
    app. No polling delay, no manual refresh needed."""
    q = _alert_subscribe()

    def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    payload = q.get(timeout=20)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # keep the connection alive through proxies
        finally:
            _alert_unsubscribe(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/alerts")
def alerts_page():
    body = """
    <div class="panel">
      <div class="ph"><h2>Recent Alerts</h2>
        <div class="phactions">
          <span class="livedot" id="liveDot"></span>
          <span class="livetxt" id="liveTxt">live</span>
          <button class="btn btn-ghost" id="ackAllBtn" onclick="ackAll()">Acknowledge All</button>
          <a class="btn btn-ghost" href="/history">History &rarr;</a>
        </div>
      </div>
      <div id="alist" class="alist"></div>
      <div id="empty" class="emptystate" style="display:none">
        <svg viewBox="0 0 24 24" width="42" height="42"><path d="M20 6L9 17l-5-5"/></svg>
        <div>No new alerts</div>
      </div>
    </div>
    <script>
    let known = new Set();
    function tpill(t){
      const cls = t>=3?'p3':(t===2?'p2':'p1');
      const lbl = t? ('TIER '+t) : 'MOTION';
      return '<span class="pill '+cls+'">'+lbl+'</span>';
    }
    function rowHtml(a){
      const event = a.tier ? 'Person confirmed' : 'Movement (no person)';
      const cam = a.camera || 'Unknown';
      return '<div class="arow" id="ar'+a.id+'">'+
        '<div class="av"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/>'+
        '<path d="M4 21v-1a6 6 0 0 1 12 0v1"/></svg></div>'+
        '<div class="txt"><div class="tt">'+event+' '+tpill(a.tier)+'</div>'+
        '<div class="ss">'+cam+' &middot; est. distance '+(a.distance_m||'-')+' m</div></div>'+
        '<div class="tm">'+a.timestamp.replace('T',' ').slice(5,16)+'</div>'+
        '<button class="ackbtn" onclick="ack('+a.id+')">Acknowledge</button></div>';
    }
    async function refresh(){
      try{
        const r = await fetch('/api/alerts/feed'); const j = await r.json();
        const dot=document.getElementById('liveDot'); dot.style.background='var(--green)';
        const list=document.getElementById('alist'); const empty=document.getElementById('empty');
        const ackAll=document.getElementById('ackAllBtn');
        if(!j.alerts.length){ list.innerHTML=''; empty.style.display='flex'; ackAll.style.display='none'; return; }
        empty.style.display='none'; ackAll.style.display='inline-flex';
        list.innerHTML = j.alerts.map(rowHtml).join('');
        // subtle highlight for alerts we haven't seen before
        j.alerts.forEach(function(a){
          if(!known.has(a.id)){ known.add(a.id);
            const el=document.getElementById('ar'+a.id); if(el) el.classList.add('fresh'); }
        });
      }catch(e){
        const dot=document.getElementById('liveDot'); if(dot) dot.style.background='var(--red)';
      }
    }
    async function ack(id){
      const el=document.getElementById('ar'+id);
      if(el){ el.style.transition='opacity .2s,transform .2s'; el.style.opacity=0; el.style.transform='translateX(20px)'; }
      try{ await fetch('/api/alert/'+id+'/dismiss',{method:'POST'}); }catch(e){}
      setTimeout(refresh, 220);
    }
    async function ackAll(){
      const ok = await openModal({
        title: 'Acknowledge All Alerts?',
        message: 'This clears every alert from this list. Records stay saved in Alert History.',
        confirmText: 'Acknowledge All',
        danger: false,
        icon: '<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>'
      });
      if(!ok) return;
      try{ await fetch('/api/alerts/dismiss_all',{method:'POST'}); }catch(e){}
      refresh();
    }
    refresh();
    // ---- real-time: push, not poll. A new alert triggers refresh() the
    // instant it's confirmed, with no delay. A slow fallback poll below
    // covers the rare case the stream connection drops silently. ----
    let es;
    function connectStream(){
      es = new EventSource('/api/alerts/stream');
      es.onmessage = function(){ refresh(); };
      es.onopen = function(){
        const dot=document.getElementById('liveDot'); if(dot) dot.style.background='var(--green)';
      };
      es.onerror = function(){
        const dot=document.getElementById('liveDot'); if(dot) dot.style.background='var(--red)';
      };
    }
    connectStream();
    setInterval(refresh, 20000);
    </script>
    <style>
      .phactions{display:flex;align-items:center;gap:10px}
      .livedot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;
        box-shadow:0 0 0 3px rgba(63,185,80,.12)}
      .livetxt{font-size:11px;color:var(--muted);letter-spacing:.5px;margin-right:6px}
      .btn{border-radius:9px;padding:7px 14px;font-size:12.5px;font-weight:600;cursor:pointer;
        text-decoration:none;display:inline-flex;align-items:center;border:1px solid var(--line);transition:.15s}
      .btn-ghost{background:var(--panel);color:var(--muted)}
      .btn-ghost:hover{color:var(--teal2);border-color:var(--teal2)}
      .arow{display:flex;align-items:center;gap:12px;padding:12px 14px;margin-bottom:9px;
        background:var(--panel);border:1px solid var(--line);border-radius:12px}
      .arow.fresh{animation:flashin .8s ease}
      @keyframes flashin{from{background:rgba(63,215,196,.18)}to{background:var(--panel)}}
      .arow .av{width:40px;height:40px;border-radius:8px;background:rgba(63,215,196,.15);
        flex-shrink:0;display:flex;align-items:center;justify-content:center}
      .arow .av svg{width:20px;height:20px;stroke:var(--teal2);fill:none;stroke-width:2}
      .arow .txt{flex:1}
      .arow .tt{font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px}
      .arow .tt .pill{padding:2px 8px;font-size:10px;border-radius:4px;font-weight:700;letter-spacing:.3px}
      .arow .ss{color:var(--muted);font-size:12.5px;margin-top:4px}
      .arow .tm{color:var(--dim);font-size:11.5px;white-space:nowrap}
      .ackbtn{background:transparent;border:1px solid var(--line);color:var(--text);
        border-radius:7px;padding:8px 16px;font-size:12px;font-weight:600;cursor:pointer;transition:.15s;white-space:nowrap}
      .ackbtn:hover{color:var(--teal2);border-color:var(--teal2);background:rgba(63,215,196,.08)}
      .emptystate{flex-direction:column;align-items:center;gap:10px;color:var(--dim);padding:50px 0}
      .emptystate svg{stroke:var(--dim);fill:none;stroke-width:2}
    </style>"""
    return page("Alerts", "/alerts", body,
                subtitle="Live &middot; updates automatically")


PAGE_SIZE = 12


@app.route("/history")
def history():
    import os
    from urllib.parse import urlencode
    tier = request.args.get("tier", "all")
    rng = request.args.get("range", "")
    q = request.args.get("q", "").strip()
    try:
        page_no = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page_no = 1

    show = request.args.get("show", "")     # "all" also lists acknowledged ones

    where = "WHERE (?='all' OR tier=?) AND (?='' OR timestamp LIKE ?)"
    params = [tier, tier, q, "%" + q + "%"]
    if rng == "24h":
        where += " AND timestamp >= datetime('now','-1 day')"
    if show != "all":
        where += " AND COALESCE(dismissed,0)=0"

    db = Database(config.DB_PATH)
    total = db.conn.execute("SELECT COUNT(*) c FROM alerts " + where, params).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_no = min(page_no, pages)
    offset = (page_no - 1) * PAGE_SIZE
    rows = db.conn.execute(
        "SELECT * FROM alerts " + where + " ORDER BY alert_id DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, offset]).fetchall()
    db.close()

    # ---- filter pills ----
    def qs(**kw):
        d = {"tier": tier, "range": rng, "q": q, "show": show}
        d.update(kw)
        d = {k: v for k, v in d.items() if v}
        return "/history?" + urlencode(d)

    def pill(label, active, href):
        return f'<a class="fpill{" active" if active else ""}" href="{href}">{label}</a>'

    pills = (
        pill("All Tiers", tier == "all", qs(tier="all", page=1))
        + pill("Tier 3", tier == "3", qs(tier="3", page=1))
        + pill("Tier 2", tier == "2", qs(tier="2", page=1))
        + pill("Tier 1", tier == "1", qs(tier="1", page=1))
        + pill("Last 24h", rng == "24h", qs(range="" if rng == "24h" else "24h", page=1))
        + pill("Include acknowledged", show == "all",
               qs(show="" if show == "all" else "all", page=1)))

    # ---- table rows ----
    import html as _html
    trows = ""
    for a in rows:
        snap = a["snapshot_path"]
        has_person = a["confidence"] and a["confidence"] > 0
        event = "Person confirmed" if has_person else "Movement (no person)"
        sub = "two-factor &middot; motion + person" if has_person else "motion only &middot; no person confirmed"
        cam = (a["camera"] if "camera" in a.keys() and a["camera"] else "&mdash;")
        ts = a["timestamp"][:19].replace("T", " ")
        if snap:
            view_url = "/snapshot/%s" % os.path.basename(snap)
            title = _html.escape(f"{event} · {cam} · {ts}", quote=True)
            thumb = "<img class='av thumb' src='%s'>" % view_url
            view_btn = (f"<button class='viewbtn' onclick=\"openSnap('{view_url}','{title}')\">View</button>")
        else:
            thumb = ("<div class='av'><svg viewBox='0 0 24 24'><circle cx='12' cy='8' r='4'/>"
                     "<path d='M4 21v-1a6 6 0 0 1 12 0v1'/></svg></div>")
            view_btn = "<span class='viewbtn disabled'>View</span>"
        trows += (
            "<tr><td><div class='evrow'>%s"
            "<div class='evtxt'><div class='evname'>%s</div><div class='evsub'>%s</div></div></div></td>"
            "<td>%s</td><td>%s m</td><td style='color:var(--muted)'>%s</td>"
            "<td>%s</td></tr>"
            % (thumb, event, sub, cam, a["distance_m"], ts, view_btn))
    if not trows:
        trows = "<tr><td colspan='5' style='color:var(--dim);padding:22px'>No alerts match this filter.</td></tr>"

    # ---- pagination ----
    lo = offset + 1 if total else 0
    hi = offset + len(rows)

    def pgbtn(n, active=False, label=None, href=None):
        cls = "pg-btn active" if active else "pg-btn"
        href = href if href is not None else qs(page=n)
        return f'<a class="{cls}" href="{href}">{label or n}</a>'

    nums = ""
    if pages <= 7:
        nums = "".join(pgbtn(n, n == page_no) for n in range(1, pages + 1))
    else:
        show = {1, 2, page_no - 1, page_no, page_no + 1, pages - 1, pages}
        last = 0
        for n in range(1, pages + 1):
            if n in show and 1 <= n <= pages:
                if last and n - last > 1:
                    nums += "<span class='muted'>&hellip;</span>"
                nums += pgbtn(n, n == page_no)
                last = n
    prev_b = pgbtn(page_no - 1, label="&lsaquo;", href=qs(page=max(1, page_no - 1)))
    next_b = pgbtn(page_no + 1, label="&rsaquo;", href=qs(page=min(pages, page_no + 1)))

    body = f"""
    <div class="panel">
      <div class="ph"><h2>Alert History</h2>
        <div class="phactions">
          <a class="btn ghost" href="/export">Export CSV</a>
          <button class="btn ghost danger" onclick="clearHist()">Clear History</button>
        </div>
      </div>
    </div>
    <div class="panel toolbar">
      <form class="searchbox" method="get">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
        <input name="q" value="{q}" placeholder="Search by date, e.g. 2026-07-14">
        <input type="hidden" name="tier" value="{tier}">
        <input type="hidden" name="range" value="{rng}">
      </form>
      <div class="fpills">{pills}</div>
    </div>
    <div class="panel" style="padding:0;overflow:hidden">
      <table class="histtable">
        <tr><th>Event</th><th>Camera</th><th>Distance</th><th>Time</th><th>Snapshot</th></tr>
        {trows}
      </table>
    </div>
    <div class="pager">
      <span class="muted">Showing {lo}&ndash;{hi} of {total}</span>
      <div class="pnums">{prev_b}{nums}{next_b}</div>
    </div>

    <div class="snapmodal" id="snapModal">
      <div class="snapbar">
        <div class="snaptitle" id="snapTitle"></div>
        <div class="snapctrls">
          <button class="snapbtn" onclick="snapZoom(-1)">&minus;</button>
          <button class="snapbtn snappct" id="snapPct" onclick="snapZoomReset()">100%</button>
          <button class="snapbtn" onclick="snapZoom(1)">+</button>
          <button class="snapbtn snapexit" onclick="closeSnap()">&#10005; Exit</button>
        </div>
      </div>
      <div class="snapstage" id="snapStage">
        <img id="snapImg" src="" draggable="false">
      </div>
      <div class="snaphint">scroll to zoom &middot; drag to pan &middot; Esc to exit</div>
    </div>

    <script>
    async function clearHist(){{
      const ok = await openModal({{
        title: 'Clear History?',
        message: 'Acknowledge and clear all history from this view.<br><br>Records stay saved for export &mdash; this only hides them here.',
        confirmText: 'Clear History',
        danger: true,
        icon: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
      }});
      if(!ok) return;
      try{{ await fetch('/api/alerts/dismiss_all',{{method:'POST'}}); }}catch(e){{}}
      location.reload();
    }}

    // ---- snapshot viewer: zoom + pan + esc-to-close ----
    let snapScale = 1, snapX = 0, snapY = 0, snapDragging = false, snapDX = 0, snapDY = 0;
    function snapApply(){{
      document.getElementById('snapImg').style.transform =
        'translate('+snapX+'px,'+snapY+'px) scale('+snapScale+')';
      document.getElementById('snapPct').textContent = Math.round(snapScale*100)+'%';
    }}
    function openSnap(url, title){{
      document.getElementById('snapImg').src = url;
      document.getElementById('snapTitle').textContent = title;
      snapScale = 1; snapX = 0; snapY = 0; snapApply();
      document.getElementById('snapModal').classList.add('show');
      document.body.style.overflow = 'hidden';
    }}
    function closeSnap(){{
      document.getElementById('snapModal').classList.remove('show');
      document.body.style.overflow = '';
    }}
    function snapZoom(dir){{
      snapScale = Math.min(6, Math.max(1, snapScale + dir*0.25));
      if(snapScale === 1){{ snapX = 0; snapY = 0; }}
      snapApply();
    }}
    function snapZoomReset(){{ snapScale = 1; snapX = 0; snapY = 0; snapApply(); }}
    document.addEventListener('keydown', function(e){{
      if(e.key === 'Escape') closeSnap();
    }});
    document.getElementById('snapStage').addEventListener('wheel', function(e){{
      e.preventDefault();
      snapZoom(e.deltaY < 0 ? 1 : -1);
    }}, {{passive:false}});
    document.getElementById('snapStage').addEventListener('mousedown', function(e){{
      if(snapScale === 1) return;
      snapDragging = true; snapDX = e.clientX - snapX; snapDY = e.clientY - snapY;
    }});
    window.addEventListener('mousemove', function(e){{
      if(!snapDragging) return;
      snapX = e.clientX - snapDX; snapY = e.clientY - snapDY;
      snapApply();
    }});
    window.addEventListener('mouseup', function(){{ snapDragging = false; }});
    document.getElementById('snapModal').addEventListener('click', function(e){{
      if(e.target.id === 'snapModal' || e.target.id === 'snapStage') closeSnap();
    }});
    </script>
    <style>
      .phactions{{display:flex;align-items:center;gap:10px}}
      .btn.ghost.danger{{color:var(--red);border-color:rgba(229,72,77,.4)}}
      .btn.ghost.danger:hover{{background:rgba(229,72,77,.1);border-color:var(--red)}}
      .histtable{{width:100%;border-collapse:collapse}}
      .histtable th{{text-align:left;padding:12px 14px;font-size:11px;letter-spacing:.6px;
        text-transform:uppercase;color:var(--teal2);border-bottom:1px solid var(--line2)}}
      .histtable td{{padding:12px 14px;border-top:1px solid var(--line);font-size:13px;vertical-align:middle}}
      .evrow{{display:flex;align-items:center;gap:12px}}
      .evrow .av{{width:44px;height:32px;border-radius:6px;background:rgba(63,215,196,.15);
        flex-shrink:0;display:flex;align-items:center;justify-content:center}}
      .evrow .av svg{{width:18px;height:18px;stroke:var(--teal2);fill:none;stroke-width:2}}
      .evrow img.av.thumb{{object-fit:cover;border:1px solid var(--line2);background:var(--bg)}}
      .evname{{font-weight:600;color:var(--text)}}
      .evsub{{font-size:11.5px;color:var(--muted);margin-top:2px}}
      .viewbtn{{display:inline-flex;align-items:center;justify-content:center;
        border:1px solid var(--teal2);color:var(--teal2);border-radius:7px;
        padding:6px 16px;font-size:12px;font-weight:600;cursor:pointer;transition:.15s;
        text-decoration:none;white-space:nowrap;background:transparent}}
      .viewbtn:hover{{background:rgba(63,215,196,.1)}}
      .viewbtn.disabled{{color:var(--dim);border-color:var(--line);cursor:default;pointer-events:none}}

      /* fullscreen snapshot viewer */
      .snapmodal{{display:none;position:fixed;inset:0;z-index:9999;background:rgba(4,8,11,.92);
        flex-direction:column}}
      .snapmodal.show{{display:flex}}
      .snapbar{{display:flex;justify-content:space-between;align-items:center;
        padding:18px 26px;flex-shrink:0}}
      .snaptitle{{font-size:14px;font-weight:600;color:var(--text)}}
      .snapctrls{{display:flex;align-items:center;gap:8px}}
      .snapbtn{{background:var(--panel2);border:1px solid var(--line2);color:var(--text);
        border-radius:8px;height:38px;min-width:38px;padding:0 12px;font-size:15px;
        font-weight:600;cursor:pointer;transition:.15s}}
      .snapbtn:hover{{border-color:var(--teal2);color:var(--teal2)}}
      .snappct{{min-width:56px;font-size:12.5px}}
      .snapexit{{background:var(--red);border-color:var(--red);color:#fff;font-size:13px}}
      .snapexit:hover{{filter:brightness(1.1);color:#fff}}
      .snapstage{{flex:1;display:flex;align-items:center;justify-content:center;
        overflow:hidden;cursor:grab;position:relative}}
      .snapstage:active{{cursor:grabbing}}
      #snapImg{{max-width:90%;max-height:82vh;border-radius:6px;
        box-shadow:0 20px 60px rgba(0,0,0,.5);transition:transform .05s linear;user-select:none}}
      .snaphint{{text-align:center;padding:16px;color:var(--muted);font-size:12.5px;flex-shrink:0}}
    </style>"""
    return page("Alert History", "/history", body,
                subtitle=f"{total} events &middot; filterable log of confirmed threats")


@app.route("/snapshot/<name>")
def snapshot(name):
    import os
    path = os.path.abspath(os.path.join(config.CAPTURES_DIR, os.path.basename(name)))
    if os.path.exists(path):
        return send_file(path)
    abort(404)


@app.route("/export")
def export():
    db = Database(config.DB_PATH)
    rows = db.conn.execute("SELECT * FROM alerts ORDER BY alert_id DESC").fetchall()
    db.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "tier", "distance_m", "confidence", "camera", "snapshot", "video", "synced", "timestamp"])
    for r in rows:
        cam = r["camera"] if "camera" in r.keys() else ""
        w.writerow([r["alert_id"], r["tier"], r["distance_m"], r["confidence"], cam,
                    r["snapshot_path"], r["video_path"], r["synced"], r["timestamp"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=caphy_alerts.csv"})


LVL_COLOR = {"INFO": "var(--muted)", "DETECT": "var(--teal2)", "TIER": "var(--teal2)",
             "ALERT": "var(--green)", "DB": "var(--muted)", "SYNC": "var(--teal2)",
             "WARN": "var(--orange)"}


def _log_line_html(l):
    col = LVL_COLOR.get(l["level"], "var(--muted)")
    msg_col = col if l["level"] in ("DETECT", "ALERT", "WARN", "SYNC", "TIER") else "var(--text)"
    return (f'<div class="logline"><span class="lt">{l["t"]}</span>'
            f'<span class="lvl" style="background:color-mix(in srgb,{col} 20%,transparent);color:{col}">{l["level"]}</span>'
            f'<span class="lm">{l["mod"]}</span>'
            f'<span style="color:{msg_col}">{l["msg"]}</span></div>')


@app.route("/logs")
def logs():
    initial = "".join(_log_line_html(l) for l in all_logs(40))
    if not initial:
        initial = '<div style="color:var(--dim)">waiting for log events...</div>'

    events_today, last_event, alerts_24h = _log_file_stats()

    if psutil:
        cpu = psutil.cpu_percent()
        vm = psutil.virtual_memory()
        mem = vm.percent
        mem_txt = f"{vm.used/1e9:.1f} / {vm.total/1e9:.0f} GB"
        disk = psutil.disk_usage("/").percent
    else:
        cpu = mem = disk = 0
        mem_txt = "install psutil"
    st = workers[0].get_stats() if workers else {}
    fpsval = float(st.get("fps", 0) or 0)

    db = Database(config.DB_PATH)
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    db.close()

    def metric(name, val, frac, color="var(--teal)"):
        return (f'<div class="metric"><div class="mrow"><span>{name}</span><b>{val}</b></div>'
                f'<div class="slider"><div class="fill" style="width:{min(frac,100)}%;background:{color}"></div></div></div>')
    health = (metric("CPU", f"{cpu:.0f}%", cpu)
              + metric("Memory", mem_txt, mem)
              + metric("Disk (media)", f"{disk:.0f}%", disk, "var(--orange)")
              + metric("Model FPS", f"{st.get('fps','-')} fps", min(fpsval * 4, 100), "var(--green)"))

    yolo_ok = any(w.yolo_ok for w in workers)
    nv_on = any(getattr(w, "nv", None) and w.nv.enabled for w in workers)
    fcm_on = os.path.exists(config.FIREBASE_KEY)
    G, T, M, R, O = "var(--green)", "var(--teal2)", "var(--muted)", "var(--red)", "var(--orange)"
    mods = [("OpenCV capture", "running", G),
            ("YOLOv8-nano", "running" if yolo_ok else "off", G if yolo_ok else R),
            ("Tier engine", "running", G),
            ("Night vision (CLAHE)", "engaged" if nv_on else "standby", T if nv_on else M),
            ("Flask API / MJPEG", "running", G),
            ("SQLite", "ok", T),
            ("Cloud sync", f"offline &middot; queue {pending}" if pending else "synced", R if pending else G),
            ("Firebase FCM", "connected" if fcm_on else "off", T if fcm_on else M)]
    modrows = "".join(
        f'<div class="modrow"><span><span class="dot" style="background:{c}"></span>{n}</span>'
        f'<span class="modtag" style="color:{c};border-color:{c}">{s}</span></div>' for n, s, c in mods)

    alert_color = "var(--red)" if alerts_24h else "var(--muted)"
    body = f"""
    <div class="grid2">
      <div class="panel logpanel">
        <div class="ph">
          <h2>caphy.log <span class="livetag">live tail</span></h2>
          <div class="logfilters">
            <button class="fbtn active" onclick="setFilter('ALL',this)">ALL</button>
            <button class="fbtn" onclick="setFilter('DETECT',this)">DETECT</button>
            <button class="fbtn" onclick="setFilter('ALERT',this)">ALERT</button>
            <button class="fbtn" onclick="setFilter('SYNC',this)">SYNC</button>
          </div>
        </div>
        <div class="logstats">
          <span><b>{events_today:,}</b> events today</span>
          <span class="sep">&middot;</span>
          <span><b>{last_event}</b> last event</span>
          <span class="sep">&middot;</span>
          <span><b style="color:{alert_color}">{alerts_24h}</b> alerts (24h)</span>
        </div>
        <div class="log" id="logtail">{initial}</div>
      </div>
      <div class="rightcol">
        <div class="panel"><h2 style="margin-bottom:16px">System Health</h2>{health}</div>
        <div class="panel"><h2 style="margin-bottom:6px">Module Status</h2>{modrows}</div>
      </div>
    </div>
    <script>
    const LC={{INFO:'var(--muted)',DETECT:'var(--teal2)',TIER:'var(--teal2)',ALERT:'var(--green)',
              DB:'var(--muted)',SYNC:'var(--teal2)',WARN:'var(--orange)'}};
    let filterLevel='ALL', expandAll=false;
    function setFilter(lvl, btn){{
      filterLevel = lvl; expandAll = false;
      document.querySelectorAll('.fbtn').forEach(function(b){{ b.classList.remove('active'); }});
      btn.classList.add('active');
      tail();
    }}
    function toggleExpand(){{ expandAll = true; tail(); }}
    function renderLine(l){{
      const c=LC[l.level]||'var(--muted)';
      const mc=(['DETECT','ALERT','WARN','SYNC','TIER'].indexOf(l.level)>=0)?c:'var(--text)';
      return '<div class="logline"><span class="lt">'+l.t+'</span>'+
        '<span class="lvl" style="background:color-mix(in srgb,'+c+' 20%,transparent);color:'+c+'">'+l.level+'</span>'+
        '<span class="lm">'+l.mod+'</span><span style="color:'+mc+'">'+l.msg+'</span></div>';
    }}
    async function tail(){{
      try{{
        const r=await fetch('/api/logs?n=150'); let logs=await r.json();
        if(filterLevel!=='ALL') logs = logs.filter(function(l){{ return l.level===filterLevel; }});
        const box=document.getElementById('logtail');
        if(!logs.length){{ box.innerHTML='<div style="color:var(--dim)">No matching events</div>'; return; }}
        const RAW=15;
        let html = logs.slice(0,RAW).map(renderLine).join('');
        const rest = logs.slice(RAW);
        if(rest.length && !expandAll){{
          // collapse long runs of an identical repeated line - "96 identical
          // DETECT/yolo lines collapsed above - Show all" instead of a huge
          // wall of the same "motion, no person" line over and over
          let i=0;
          while(i<rest.length){{
            let j=i;
            while(j<rest.length && rest[j].level===rest[i].level &&
                  rest[j].mod===rest[i].mod && rest[j].msg===rest[i].msg) j++;
            const n=j-i;
            if(n>=3){{
              html += '<div class="logcollapsed">&#8635; '+n+' identical '+rest[i].level+'/'+rest[i].mod+
                ' lines collapsed above &mdash; <a href="#" onclick="toggleExpand();return false;">Show all</a></div>';
            }} else {{
              for(let k=i;k<j;k++) html += renderLine(rest[k]);
            }}
            i=j;
          }}
        }} else if(rest.length){{
          html += rest.map(renderLine).join('');
        }}
        box.innerHTML = html;
      }}catch(e){{}}
    }}
    setInterval(tail,1500); tail();
    </script>
    <style>
      .ph{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px}}
      .livetag{{color:var(--muted);font-weight:400;font-size:11.5px;margin-left:8px}}
      .logfilters{{display:flex;gap:8px}}
      .fbtn{{background:var(--panel);border:1px solid var(--line2);color:var(--muted);
        border-radius:8px;padding:6px 13px;font-size:11.5px;font-weight:700;letter-spacing:.4px;
        cursor:pointer;transition:.15s}}
      .fbtn:hover{{color:var(--text);border-color:var(--teal2)}}
      .fbtn.active{{color:var(--teal2);border-color:var(--teal2);background:rgba(63,215,196,.08)}}
      .logstats{{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12.5px;
        margin:12px 0 14px;padding:10px 14px;background:var(--panel);border:1px solid var(--line);
        border-radius:10px}}
      .logstats b{{color:var(--text);font-weight:700}}
      .logstats .sep{{color:var(--line2)}}
      .logcollapsed{{color:var(--muted);font-size:12px;padding:8px 4px;font-style:italic}}
      .logcollapsed a{{color:var(--teal2);font-style:normal;cursor:pointer}}
      .logcollapsed a:hover{{text-decoration:underline}}
      .modtag{{font-size:11px;font-weight:700;padding:3px 10px;border-radius:7px;border:1px solid}}
    </style>"""
    return page("System Logs", "/logs", body,
                subtitle="Diagnostics &middot; detection pipeline &amp; sync events")


SETTINGS_TABS = [("detection", "Detection"), ("cameras", "Cameras"), ("alerts", "Alerts"),
                 ("storage", "Storage &amp; Sync"), ("voice", "Voice"), ("device", "Connect Phone"),
                 ("offline", "Offline Mode"), ("users", "Users"), ("about", "About")]


def _sw(name, on):
    return (f'<label class="sw"><input type="checkbox" name="{name}" {"checked" if on else ""}>'
            f'<span class="track"></span></label>')


def _irow(label, value):
    return f'<div class="irow"><span>{label}</span><b>{value}</b></div>'


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = Database(config.DB_PATH)
    if request.method == "POST":
        section = request.form.get("section", "detection")
        if section == "detection":
            if request.form.get("action") == "reset":
                sens, conf, t1, t3, night, auto, highest = (1500, 0.5, config.TIER1_MIN_DIST,
                                                            config.TIER3_MAX_DIST, 0, 0, 0)
            else:
                try:
                    pct = float(request.form.get("motion", 60))
                except ValueError:
                    pct = 60
                sens = round(4000 - pct / 100 * 3900)         # % -> min pixel-change area
                conf = request.form.get("person_conf", 0.5)
                t1 = request.form.get("tier1", config.TIER1_MIN_DIST)
                t3 = request.form.get("tier3", config.TIER3_MAX_DIST)
                night = 1 if request.form.get("night") == "on" else 0
                auto = 1 if request.form.get("autoarm") == "on" else 0
                highest = 1 if request.form.get("highest") == "on" else 0
            db.conn.execute("UPDATE settings SET sensitivity=?, person_conf=?, armed=?, night_vision=? WHERE setting_id=1",
                            (sens, conf, auto, night))
            db.conn.commit()
            # tier distances + highest-security have no DB column, so persist them to prefs
            save_prefs({"tier1": float(t1), "tier3": float(t3), "highest": bool(highest)})
            for w in workers:
                w.update_settings(sens, conf, t1, t3, night, highest)
            db.close()
            return redirect(url_for("settings"))
        if section == "cameras":
            names = []
            for w in workers:
                nm = request.form.get(f"cam_name_{w.cam_id}", "").strip()
                if nm:
                    w.name = nm
                names.append(w.name)
            if names:
                save_prefs({"camera_names": names})
            db.close()
            return redirect(url_for("settings", tab="cameras"))
        if section == "users":
            # Verifies the CURRENT password against Firebase Auth (via the
            # same REST sign-in helper /login uses) before letting the new
            # one through - this used to check a "username" column that no
            # longer exists in this schema (email/firebase_uid are the
            # real keys now), so it silently could never succeed. Now it
            # both verifies AND updates the actual Firebase Auth account,
            # matching whatever the phone will authenticate against next.
            cur_pw = request.form.get("cur_pw", "")
            new_pw = request.form.get("new_pw", "")
            email = session.get("email", "")
            uid = session.get("user_uid")
            ok = False
            if email and uid and new_pw and len(new_pw) >= 6:
                verify = _firebase_rest_sign_in(email, cur_pw)
                if verify.get("uid") == uid:
                    try:
                        from firebase_admin import auth as fb_auth
                        from firebase_auth import init_firebase
                        init_firebase()
                        fb_auth.update_user(uid, password=new_pw)
                        ok = True
                    except Exception:
                        ok = False
            db.close()
            return redirect(url_for("settings", tab="users", pw=("ok" if ok else "err")))
        db.close()
        return redirect(url_for("settings"))

    s = db.conn.execute("SELECT * FROM settings WHERE setting_id=1").fetchone()
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    total = db.conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
    db.close()

    active = request.args.get("tab", "detection")
    min_area = float(s["sensitivity"] or 1500)
    mot_pct = max(0, min(100, round((4000 - min_area) / 3900 * 100)))
    pconf = float(s["person_conf"] or 0.5)
    prefs = load_prefs()
    t1 = prefs.get("tier1", config.TIER1_MIN_DIST)
    t3 = prefs.get("tier3", config.TIER3_MAX_DIST)
    night_on, auto_on = bool(s["night_vision"]), bool(s["armed"])
    highest_on = prefs.get("highest", workers[0].highest if workers else False)

    # ---- Detection ----
    sec_detection = f"""
      <form method="post">
        <input type="hidden" name="section" value="detection">
        <div class="sechead">Detection Settings</div><div class="subd">Tune the two-factor validation pipeline</div>
        <div class="field">
          <div class="flabel"><b>Motion Sensitivity (Factor 1)</b><span class="val" id="motVal">{mot_pct}%</span></div>
          <div class="fdesc">Higher = more triggers &middot; pixel-change threshold</div>
          <input type="range" id="mot" name="motion" min="0" max="100" value="{mot_pct}"
                 oninput="document.getElementById('motVal').textContent=this.value+'%'">
        </div>
        <div class="field">
          <div class="flabel"><b>Person Confidence (Factor 2)</b><span class="val" id="pcVal">{pconf:.2f}</span></div>
          <div class="fdesc">Minimum YOLOv8 confidence to confirm a person</div>
          <input type="range" id="pc" name="person_conf" min="0.3" max="0.95" step="0.01" value="{pconf}"
                 oninput="document.getElementById('pcVal').textContent=(+this.value).toFixed(2)">
        </div>
        <div class="secheadsmall">THREE-TIER DISTANCE THRESHOLDS</div>
        <div class="tiercards">
          <div class="tiercard t1"><div class="tl">Tier 1 &middot; Far</div>
            <div class="tv">&gt; <input class="tin" name="tier1" value="{t1}"> m</div><div class="tsub">Log only</div></div>
          <div class="tiercard t2"><div class="tl">Tier 2 &middot; Medium</div>
            <div class="tv">{t3}&ndash;{t1} m</div><div class="tsub">Snapshot + alert</div></div>
          <div class="tiercard t3"><div class="tl">Tier 3 &middot; Close</div>
            <div class="tv">&lt; <input class="tin" name="tier3" value="{t3}"> m</div><div class="tsub">Video + siren</div></div>
        </div>
        <div class="togglerow"><div><b>Software Night Vision (CLAHE)</b>
          <div class="fdesc" style="margin:2px 0 0">Auto-enhance low-light frames</div></div>{_sw("night", night_on)}</div>
        <div class="togglerow"><div><b>Auto-Arm at Night</b>
          <div class="fdesc" style="margin:2px 0 0">Arm system on schedule &middot; {getattr(config,'AUTO_ARM_START_HOUR',22):02d}:00&ndash;{getattr(config,'AUTO_ARM_END_HOUR',6):02d}:00</div></div>{_sw("autoarm", auto_on)}</div>
        <div class="togglerow"><div><b>Highest Security</b>
          <div class="fdesc" style="margin:2px 0 0">Any confirmed person triggers a full Tier-3 response</div></div>{_sw("highest", highest_on)}</div>
        <div class="setfoot">
          <button class="btn ghost" name="action" value="reset">Reset Defaults</button>
          <button class="btn" name="action" value="save">Save Changes</button>
        </div>
      </form>"""

    # ---- Cameras ----
    cam_list = [(w.cam_id, w.name) for w in workers] or list(enumerate(getattr(config, "CAMERA_NAMES", ["Cam 0", "Cam 1"])))
    cam_inputs = "".join(
        f'<div class="field"><div class="flabel"><b>Camera {i}</b></div>'
        f'<div class="fdesc">Display name shown across the console &amp; saved with each alert</div>'
        f'<input name="cam_name_{i}" value="{nm}" style="width:100%"></div>' for i, nm in cam_list)
    extra = getattr(config, "EXTRA_CAMERAS", [])
    extra_txt = "<br>".join(str(e) for e in extra if isinstance(e, str)) or "none"
    sec_cameras = f"""
      <div class="sechead">Cameras</div><div class="subd">Choose which cameras CAPHY uses, then name them</div>

      <div class="secheadsmall">DETECTED CAMERAS</div>
      <div id="camPickStatus" style="color:var(--muted);font-size:13px;margin-bottom:10px">Detecting cameras&hellip;</div>
      <div id="camPickList"></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap">
        <button class="btn" id="camApplyBtn" onclick="applyCameras()" disabled>Apply</button>
        <button class="btn ghost" onclick="loadCameras()">Rescan</button>
        <span id="camApplyMsg" style="font-size:12px;color:var(--muted)"></span>
      </div>
      <div style="color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6">
        You can use up to <span id="camMax">{getattr(config,'MAX_CAMERAS',2)}</span> cameras at once.
        Index 0 is usually your laptop's built-in webcam; plug in a USB camera and press
        <b>Rescan</b> to see it, then tick the ones you want and press <b>Apply</b>.
      </div>

      <form method="post">
        <input type="hidden" name="section" value="cameras">
        <div class="secheadsmall" style="margin-top:22px">CAMERA NAMES</div>
        {cam_inputs}
        <div class="secheadsmall">CAPTURE (edit config.py to change)</div>
        {_irow("Resolution", f"{config.FRAME_WIDTH} &times; {config.FRAME_HEIGHT}")}
        {_irow("Stream quality", f"{getattr(config,'JPEG_QUALITY',70)} / 100")}
        {_irow("Network cameras", extra_txt)}
        <div class="setfoot"><button class="btn" name="action" value="save">Save Names</button></div>
      </form>
    """ + _CAMERA_PICKER_JS

    # ---- Alerts (reflects config) ----
    fmt_t = lambda xs: ", ".join("Tier %s" % t for t in xs) or "none"
    sec_alerts = f"""
      <div class="sechead">Alerts &amp; Response</div><div class="subd">What each threat tier does (edit config.py to change)</div>
      {_irow("Save snapshot on", fmt_t(getattr(config,'SNAPSHOT_TIERS',[])))}
      {_irow("Record video on", fmt_t(getattr(config,'RECORD_TIERS',[])))}
      {_irow("Sound siren on", fmt_t(getattr(config,'SIREN_TIERS',[])))}
      {_irow("Push to phone from", "Tier %s up" % getattr(config,'PUSH_MIN_TIER',1))}
      {_irow("Alert cooldown", f"{getattr(config,'ALERT_COOLDOWN_SEC',5)} s")}
      {_irow("Recording grace", f"{getattr(config,'PRESENCE_GRACE_SEC',1.5)} s after person leaves")}
      {_irow("Highest Security", "on" if highest_on else "off")}"""

    # ---- Storage & Sync ----
    sec_storage = f"""
      <div class="sechead">Storage &amp; Sync</div><div class="subd">Local capture files and cloud upload</div>
      {_irow("Captures folder", getattr(config,'CAPTURES_DIR','captures'))}
      {_irow("Compressed folder", getattr(config,'COMPRESSED_DIR','captures_compressed'))}
      {_irow("Upload image quality", f"{getattr(config,'IMAGE_QUALITY',60)} / 100")}
      {_irow("Sync interval", f"{getattr(config,'SYNC_INTERVAL_SEC',15)} s")}
      {_irow("Firebase bucket", getattr(config,'FIREBASE_BUCKET','not set'))}
      {_irow("Alerts stored", total)}
      {_irow("Pending upload", f"{pending} queued" if pending else "all synced")}"""

    # ---- Voice ----
    # Voice runs on the phone app, so there is nothing to configure here - this
    # panel documents the command set and confirms the API is serving it.
    try:
        from voice.commands import config as _icfg
        n_cmds = len(_icfg()["intents"])
    except Exception:
        n_cmds = 0

    sec_voice = f"""
      <div class="sechead">Voice Control</div>
      <div class="subd">Spoken commands run from the CAPHY mobile app</div>
      {_irow("Where it runs", "CAPHY mobile app")}
      {_irow("Speech to text", "on the phone (device recognizer)")}
      {_irow("Spoken replies", "on the phone (device TTS)")}
      {_irow("This console", "serves the commands, does not listen")}
      {_irow("Languages", "English + Tagalog")}
      {_irow("Commands available", f"{n_cmds}")}
      {_irow("Source of truth", "voice/intents.json")}"""

    # ---- Connect Phone (scan-to-connect, no phone login) ----
    sec_device = """
      <div class="sechead">Connect Phone</div>
      <div class="subd">Sign in on your phone just by scanning &mdash; no password</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:18px">
        CAPHY has no dedicated camera hardware &mdash; this laptop's own webcam
        is the camera. Open the CAPHY app on your phone and scan the code below.
        Your phone is signed in as this same account and connected to this
        laptop in one step &mdash; there's no email or password to type on the
        phone. This works from anywhere (mobile data included); the QR just
        needs to be on your phone's camera. The code is single-use and expires
        in a few minutes, so only scan a fresh one you generated yourself.</p>
      <div id="pairWrap" style="display:flex;flex-direction:column;align-items:center;gap:14px;padding:20px 0">
        <div id="pairQr" style="background:#fff;padding:16px;border-radius:12px"></div>
        <div id="pairStatus" style="color:var(--muted);font-size:13px">Generating code&hellip;</div>
        <button class="btn ghost" onclick="genPairCode()">New Code</button>
      </div>
      <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
      <script>
      let pairQrObj = null;
      let pairTimer = null;
      async function genPairCode(){
        clearTimeout(pairTimer);
        document.getElementById('pairStatus').textContent = 'Generating code\\u2026';
        try{
          const r = await fetch('/api/pairing/link-token');
          const d = await r.json();
          if(!d.success){ document.getElementById('pairStatus').textContent = (d.error || 'Failed to generate code.'); return; }
          const el = document.getElementById('pairQr');
          el.innerHTML = '';
          pairQrObj = new QRCode(el, { text: JSON.stringify(d.payload), width: 200, height: 200 });
          document.getElementById('pairStatus').textContent =
            'Code expires in ' + Math.round(d.expires_in/60) + ' min \\u00b7 scan with the CAPHY app';
          // Refresh a bit BEFORE expiry so an on-screen code is always valid.
          pairTimer = setTimeout(genPairCode, Math.max(10, d.expires_in - 15) * 1000);
        }catch(e){
          document.getElementById('pairStatus').textContent = 'Could not reach server.';
        }
      }
      document.addEventListener('DOMContentLoaded', function(){
        if(document.getElementById('sec-device')) genPairCode();
      });
      if(document.readyState !== 'loading') genPairCode();
      </script>"""

    # ---- Users ----
    pw = request.args.get("pw", "")
    msg = ('<div class="fdesc" style="color:var(--green)">Password updated.</div>' if pw == "ok"
           else '<div class="fdesc" style="color:var(--red)">Current password is incorrect.</div>' if pw == "err" else "")
    sec_users = f"""
      <form method="post">
        <input type="hidden" name="section" value="users">
        <div class="sechead">Users</div><div class="subd">Console account</div>
        {_irow("Signed in as", session.get("email","admin"))}
        {_irow("Role", "Homeowner")}
        <div class="secheadsmall" style="margin-top:20px">CHANGE PASSWORD</div>
        <div class="field"><div class="flabel"><b>Current password</b></div>
          <input type="password" name="cur_pw" style="width:100%"></div>
        <div class="field"><div class="flabel"><b>New password</b></div>
          <input type="password" name="new_pw" style="width:100%">{msg}</div>
        <div class="setfoot"><button class="btn" name="action" value="save">Update Password</button></div>
      </form>"""

    # ---- About ----
    ncam = len(workers)
    sec_about = f"""
      <div class="sechead">About CAPHY</div><div class="subd">AI-Intelligent Security System</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:18px">
        Two-Factor Motion Validation &amp; Monitoring Protocols &mdash; pixel-change motion (Factor 1)
        gates YOLOv8 person verification (Factor 2), with a 3-tier distance-based threat response.</p>
      {_irow("Version", "Console v1.0")}
      {_irow("Detection", "OpenCV motion + YOLOv8-nano")}
      {_irow("Cameras online", f"{ncam}")}
      {_irow("Mobile alerts", "Firebase Cloud Messaging")}
      {_irow("Voice commands", "CAPHY mobile app (English + Tagalog)")}"""

    # ---- Offline / Local Mode ----
    lan_ip = _local_ip()
    sec_offline = f"""
      <div class="sechead">Offline / Local Mode</div>
      <div class="subd">Keep using CAPHY when there's no internet</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:16px">
        CAPHY normally works from anywhere over the internet. If the internet
        goes down, it can still run in <b>Local Mode</b> &mdash; your phone talks
        straight to this laptop over your local Wi&#8209;Fi, with no internet
        needed. This only works when your <b>phone and this laptop are on the
        same Wi&#8209;Fi network</b> (or the same hotspot), so it naturally can't
        happen while you're away from home &mdash; that's by design.</p>

      <div class="secheadsmall">THIS LAPTOP'S LOCAL ADDRESS</div>
      {_irow("Local address", f"http://{lan_ip}:5000")}
      <div style="color:var(--dim);font-size:11.5px;margin:6px 2px 18px;line-height:1.6">
        This address only works on your own Wi&#8209;Fi. It changes if you switch
        networks, so the app finds it automatically &mdash; you rarely need to
        type it.</div>

      <div class="secheadsmall">CONNECT PHONE &amp; LAPTOP OFFLINE &mdash; STEP BY STEP</div>
      <ol style="color:var(--muted);font-size:13px;line-height:1.9;padding-left:20px;margin:6px 0 4px">
        <li>Put this laptop and your phone on the <b>same Wi&#8209;Fi</b>. No
            internet? Turn on a phone hotspot and connect the laptop to it, or
            use any router even without an internet uplink.</li>
        <li>Keep CAPHY running on this laptop (this window).</li>
        <li>Open the CAPHY app on your phone. It checks your connection every few
            seconds and, when it sees there's no internet but this laptop is
            reachable, it switches by itself &mdash; you'll see an amber
            <b>&ldquo;Offline mode &middot; Local Wi&#8209;Fi&rdquo;</b> banner at
            the top.</li>
        <li>That's it &mdash; live view and controls work over the local network.</li>
      </ol>

      <div class="secheadsmall" style="margin-top:18px">WHAT WORKS OFFLINE (LOCAL MODE)</div>
      {_irow("Live camera view", "Yes (over local Wi-Fi)")}
      {_irow("Arm / Disarm, camera, siren", "Yes")}
      {_irow("On-laptop detection, alerts &amp; siren", "Yes (always, never needs internet)")}
      {_irow("Push notifications to phone", "No - needs internet")}
      {_irow("Watching from away / mobile data", "No - needs internet")}
      {_irow("Alert history synced to the cloud", "No - resumes when internet returns")}
      <div style="color:var(--dim);font-size:11.5px;margin-top:12px;line-height:1.6">
        Detection, recording and the siren on this laptop keep running no matter
        what &mdash; Local Mode is only about how your <i>phone</i> reaches the
        system.</div>
    """

    sections = {"detection": sec_detection, "cameras": sec_cameras, "alerts": sec_alerts,
                "storage": sec_storage, "voice": sec_voice, "device": sec_device,
                "offline": sec_offline,
                "users": sec_users, "about": sec_about}
    nav = "".join(
        f'<a class="{"active" if tid == active else ""}" onclick="setTab(\'{tid}\',this)">{label}</a>'
        for tid, label in SETTINGS_TABS)
    secs = "".join(
        f'<div class="setsec {"active" if tid == active else ""}" id="sec-{tid}">{sections[tid]}</div>'
        for tid, _ in SETTINGS_TABS)

    body = """
    <div class="setwrap">
      <div class="setnav">__NAV__</div>
      <div class="setbody">__SECS__</div>
    </div>
    <script>
    function setTab(id, el){
      document.querySelectorAll('.setsec').forEach(function(s){ s.classList.remove('active'); });
      document.getElementById('sec-'+id).classList.add('active');
      document.querySelectorAll('.setnav a').forEach(function(a){ a.classList.remove('active'); });
      el.classList.add('active');
    }
    </script>""".replace("__NAV__", nav).replace("__SECS__", secs)
    return page("Settings", "/settings", body,
                subtitle="Configure detection, cameras, alerts &amp; storage")


@app.route("/video_feed/<int:cam>")
@app.route("/video_feed")
def video_feed(cam=0):
    def gen():
        while True:
            jpg = workers[cam].get_jpeg() if 0 <= cam < len(workers) else None
            if jpg is None:
                img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
                jpg = cv2.imencode(".jpg", img)[1].tobytes()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.08)   # ~12 fps stream - lighter on the browser with multiple feeds
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
