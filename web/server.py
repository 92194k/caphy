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
import secrets
import hashlib
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import cv2
import numpy as np
from flask import (Flask, Response, request, redirect, url_for, session,
                   render_template, render_template_string, send_file, abort, jsonify)

import config
from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine
from detection.night_vision import NightVision
from storage.database import Database
from storage.alerts import AlertManager
from storage.push import PushSender
from siren import Siren

try:
    import psutil
except Exception:
    psutil = None

app = Flask(__name__)
app.secret_key = "caphy-local-console-secret"
app.permanent_session_lifetime = timedelta(days=30)   # "remember this device"

TIER_BGR = {1: (80, 200, 120), 2: (60, 160, 240), 3: (60, 60, 230)}


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
        cv2.putText(frame, self.name, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 2)
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
        push = PushSender(config.FIREBASE_KEY, config.PUSH_TOPIC)
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

            siren_on = armed and result["threat"] and result["tier"] in config.SIREN_TIERS
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

            if armed and time.time() >= _arm_grace_until:
                aid = alerts.handle(frame, result, fps)
                if aid is not None:
                    p = max(result["persons"], key=lambda x: x["tier"])
                    self._log("ALERT", "db", f"alert #{aid} Tier {result['tier']} saved")
                    if result["tier"] >= config.PUSH_MIN_TIER:
                        srow = db.conn.execute("SELECT snapshot_path FROM alerts WHERE alert_id=?", (aid,)).fetchone()
                        push.send_async(result["tier"], p["distance_m"],
                                        srow["snapshot_path"] if srow else None, self.name, aid)
                        self._log("ALERT", "fcm", f"push Tier {result['tier']} queued")

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
        # (fourcc, extension) candidates, in order of preference
        for fourcc, ext in (("mp4v", "mp4"), ("avc1", "mp4"), ("MJPG", "avi"), ("XVID", "avi")):
            path = os.path.join(vdir, f"manual_{stamp}_cam{self.cam_id}.{ext}")
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
                self.stats = stats

    def _store_placeholder(self, text="Camera offline"):
        img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
        cv2.putText(img, text, (40, config.FRAME_HEIGHT // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (110, 110, 110), 2)
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.stats = {**self.stats, "online": False, "motion": False,
                              "person": False, "tier": 0,
                              "camera_on": not self.paused}

    def get_jpeg(self):
        with self.lock:
            return self.jpeg

    def get_stats(self):
        with self.lock:
            return dict(self.stats)

    def get_logs(self):
        with self.lock:
            return list(self.logs)


workers = []

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


def resolve_cameras():
    """Auto-scan connected cameras (+ any phone URLs) or use the manual list."""
    if getattr(config, "AUTO_SCAN_CAMERAS", False):
        cams = scan_cameras()
        cams += list(getattr(config, "EXTRA_CAMERAS", []))
        cams = cams[:getattr(config, "MAX_CAMERAS", 2)]
        if not cams:
            cams = [0]
        print(f"[CAPHY] Auto-scan cameras: {cams}")
        return cams
    return config.CAMERAS


def start_workers(sources):
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


def all_logs(limit=200):
    merged = []
    for w in workers:
        merged.extend(w.get_logs())
    merged.sort(key=lambda l: l["t"], reverse=True)
    return merged[:limit]


# ==================== auth (web session + mobile token) ====================
API_TOKENS = set()   # bearer tokens issued to the mobile app


def _token_from_request():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.args.get("token", "")   # allow ?token= for image/stream URLs


def _authed():
    return bool(session.get("auth")) or _token_from_request() in API_TOKENS


# paths the mobile app reaches with a token instead of a web session
# ("/video" covers both /video_feed (live stream) and /video/<file> (recordings))
_TOKEN_PATHS = ("/api/", "/video", "/snapshot")


@app.before_request
def guard():
    if request.endpoint in ("login", "static"):
        return
    if request.path == "/api/login":
        return                         # login endpoint issues the token
    if request.path.startswith(_TOKEN_PATHS):
        if _authed():
            return
        return jsonify({"error": "unauthorized"}), 401
    if not session.get("auth"):
        return redirect(url_for("login"))


# ==================== HTML / CSS ====================
NAV = [("/", "Dashboard"), ("/live", "Live Camera"), ("/alerts", "Alerts"),
       ("/history", "Alert History"), ("/logs", "System Logs"), ("/settings", "Settings")]


def page(title, href, body, subtitle=""):
    st = workers[0].get_stats() if workers else {"armed": True, "online": False}
    online = sum(1 for w in workers if w.get_stats().get("online"))
    cam = f"{online} of {len(workers)} cameras online" if workers else "no cameras"
    return render_template("base.html", title=title, page=href, nav=NAV, body=body,
                           subtitle=subtitle, ncam=online, user=session.get("user", "admin"),
                           armed=st.get("armed", True), cam=cam)


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
@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        db = Database(config.DB_PATH)
        row = db.conn.execute("SELECT password_hash FROM users WHERE username=?", (u,)).fetchone()
        db.close()
        if row and row["password_hash"] == hashlib.sha256(p.encode()).hexdigest():
            session["auth"] = True
            session["user"] = u
            session.permanent = request.form.get("remember") == "on"  # keep 30 days if checked
            return redirect(url_for("dashboard"))
        err = "Invalid username or password."
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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

    # ---- 24h threat chart ----
    maxh = max(hours.values()) if hours else 1
    bars = ""
    for h in range(24):
        c = hours.get(h, 0)
        cls = "bar hot" if (c and c >= maxh) else ("bar warm" if c >= maxh * 0.5 and c else "bar")
        pct = int(100 * c / maxh) if maxh else 0
        bars += f'<div class="{cls}" style="height:{max(pct,6)}%"></div>'

    body = f"""
    {cards}
    <div class="grid2">
      <div class="panel">
        <div class="ph"><h2>Live Feed{feed_title}</h2><a class="link" href="/live">Open Live Camera &rarr;</a></div>
        {feed}
      </div>
      <div class="panel">
        <div class="ph"><h2>Recent Alerts</h2><a class="link" href="/history">View all</a></div>
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
            f'<div class="cs" id="cs{w.cam_id}">online</div></div>'
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
          <button class="ctrlbtn primary" id="armBtn" onclick="toggleArm()">Arm</button>
          <button class="ctrlbtn" id="camBtn" onclick="toggleCam()">Camera Off</button>
          <button class="ctrlbtn" onclick="snap()">Snapshot</button>
          <button class="ctrlbtn" id="recBtn" onclick="toggleRec()">Record</button>
          <button class="ctrlbtn" id="nvBtn" onclick="toggleNV()">Night Vision</button>
          <button class="ctrlbtn siren" id="sirenBtn" onclick="siren()">Trigger Siren</button>
          <button class="ctrlbtn emg" id="emgBtn" onclick="toggleEmg()">Emergency</button>
          <button class="ctrlbtn" onclick="fsMain()">Fullscreen</button>
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
        b.textContent = recOn ? 'Stop Recording' : 'Record';
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
        armBtn.textContent = ST.armed ? 'Disarm' : 'Arm';
        armBtn.classList.toggle('active', ST.armed);

        const camBtn=document.getElementById('camBtn');
        camBtn.textContent = ST.camera_on ? 'Camera Off' : 'Camera On';
        camBtn.classList.toggle('active', !ST.camera_on);

        document.getElementById('nvBtn').classList.toggle('active', ST.night_vision);
        document.getElementById('sirenBtn').textContent = ST.siren ? 'Stop Siren' : 'Trigger Siren';

        const emgBtn=document.getElementById('emgBtn');
        emgBtn.textContent = ST.emergency ? 'Cancel Emergency' : 'Emergency';
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
      if(!ST.emergency && !confirm('Activate EMERGENCY mode?\\n\\nThis forces the camera on, arms the system, sounds the siren and sends a push alert.')) return;
      try{ await fetch('/api/emergency',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: !ST.emergency})}); }catch(e){}
      refreshState();
    }

    async function poll(){
      try{
        const r=await fetch('/api/stats'); const data=await r.json();
        data.forEach(function(s){
          const camOn = (s.camera_on !== false);
          const cd=document.getElementById('cd'+s.cam);
          if(cd) cd.style.background = !camOn ? 'var(--orange)' : (s.online?'var(--green)':'var(--dim)');
          const cs=document.getElementById('cs'+s.cam);
          // "camera off" and "offline" are different problems - say which
          if(cs) cs.textContent = !camOn ? 'camera off' : (s.online?'online':'offline');
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

      .controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:9px;
        overflow:hidden;transition:max-height .25s ease,opacity .2s;max-height:400px}
      .controls.hidden{max-height:0;opacity:0;margin:0}
      /* one consistent button style everywhere - no icons, just words */
      .ctrlbtn{display:flex;align-items:center;justify-content:center;
        background:var(--panel);border:1px solid var(--line);color:var(--text);
        border-radius:10px;padding:12px 10px;font-size:13px;font-weight:600;
        letter-spacing:.2px;cursor:pointer;transition:.15s;text-align:center}
      .ctrlbtn:hover{border-color:var(--teal2);color:var(--teal2)}
      .ctrlbtn.active{background:var(--teal);color:#04110e;border-color:var(--teal)}
      .ctrlbtn.primary{border-color:var(--teal2);color:var(--teal2)}
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
                subtitle="two-factor validation active")


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


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    u, p = data.get("username", ""), data.get("password", "")
    db = Database(config.DB_PATH)
    row = db.conn.execute("SELECT password_hash, role FROM users WHERE username=?", (u,)).fetchone()
    db.close()
    if row and row["password_hash"] == hashlib.sha256(p.encode()).hexdigest():
        tok = secrets.token_hex(24)
        API_TOKENS.add(tok)
        return jsonify({"ok": True, "token": tok, "user": u, "role": row["role"] or "Homeowner"})
    return jsonify({"ok": False, "error": "invalid credentials"}), 401


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
            PushSender(config.FIREBASE_KEY, config.PUSH_TOPIC).send_async(
                3, "-", None, "EMERGENCY", None)
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


@app.route("/api/alerts/feed")
def api_alerts_feed():
    """Visible (unacknowledged) alerts as JSON, for live polling."""
    db = Database(config.DB_PATH)
    try:
        rows = db.recent_alerts(30)
        out = [{"id": a["alert_id"], "tier": a["tier"] or 0,
                "distance_m": a["distance_m"], "camera": a.get("camera"),
                "timestamp": a["timestamp"]} for a in rows]
        return jsonify({"alerts": out})
    finally:
        db.close()


@app.route("/alerts")
def alerts_page():
    body = """
    <div class="panel">
      <div class="ph"><h2>Recent Alerts</h2>
        <div class="phactions">
          <span class="livedot" id="liveDot"></span>
          <span class="livetxt" id="liveTxt">live</span>
          <button class="btn btn-ghost" id="ackAllBtn" onclick="ackAll()">Acknowledge all</button>
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
      const lbl = t? ('Tier '+t) : 'Motion';
      return '<span class="pill '+cls+'">'+lbl+'</span>';
    }
    function rowHtml(a){
      const event = a.tier ? 'Person confirmed' : 'Movement (no person)';
      const cam = a.camera ? ' &middot; '+a.camera : '';
      return '<div class="arow" id="ar'+a.id+'">'+
        '<div class="av"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/>'+
        '<path d="M4 21v-1a6 6 0 0 1 12 0v1"/></svg></div>'+
        '<div class="txt"><div class="tt">'+tpill(a.tier)+'</div>'+
        '<div class="ss">'+event+cam+' &middot; '+(a.distance_m||'-')+' m</div></div>'+
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
      if(!confirm('Acknowledge all alerts?')) return;
      try{ await fetch('/api/alerts/dismiss_all',{method:'POST'}); }catch(e){}
      refresh();
    }
    refresh(); setInterval(refresh, 3000);
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
      .arow .av svg{width:22px;height:22px;stroke:var(--muted);fill:none;stroke-width:2}
      .arow .txt{flex:1}
      .arow .ss{color:var(--muted);font-size:12.5px;margin-top:2px}
      .arow .tm{color:var(--dim);font-size:11.5px;white-space:nowrap}
      .ackbtn{background:transparent;border:1px solid var(--line);color:var(--muted);
        border-radius:9px;padding:6px 13px;font-size:12px;cursor:pointer;transition:.15s;white-space:nowrap}
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
    trows = ""
    for a in rows:
        snap = a["snapshot_path"]
        if snap:
            thumb = "<img class='thumb' src='/snapshot/%s'>" % os.path.basename(snap)
        else:
            thumb = ("<div class='snapav'><svg viewBox='0 0 24 24'><circle cx='12' cy='8' r='4'/>"
                     "<path d='M4 21v-1a6 6 0 0 1 12 0v1'/></svg></div>")
        has_person = a["confidence"] and a["confidence"] > 0
        event = "Person detected" if has_person else "Movement (no person)"
        conf = ("%.2f" % a["confidence"]) if has_person else "&mdash;"
        view = ("/snapshot/%s" % os.path.basename(snap)) if snap else "#"
        cam = (a["camera"] if "camera" in a.keys() and a["camera"] else "&mdash;")
        trows += (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td style='color:var(--muted)'>%s</td>"
            "<td>%s m</td><td>%s</td><td style='color:var(--muted)'>%s</td>"
            "<td><a class='link' href='%s'>View &rsaquo;</a></td></tr>"
            % (thumb, tier_pill(a["tier"] or 1), event, cam, a["distance_m"], conf,
               a["timestamp"][:16].replace("T", " "), view))
    if not trows:
        trows = "<tr><td colspan='8' style='color:var(--dim);padding:22px'>No alerts match this filter.</td></tr>"

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
    <div class="panel toolbar">
      <form class="searchbox" method="get">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
        <input name="q" value="{q}" placeholder="Search by date, e.g. 2026-07-14">
        <input type="hidden" name="tier" value="{tier}">
        <input type="hidden" name="range" value="{rng}">
      </form>
      <div class="fpills">{pills}</div>
      <a class="btn ghost" href="/export">Export CSV</a>
    </div>
    <div class="panel" style="padding:0;overflow:hidden">
      <table>
        <tr><th>Snapshot</th><th>Tier</th><th>Event</th><th>Camera</th><th>Distance</th>
            <th>Conf.</th><th>Time</th><th></th></tr>
        {trows}
      </table>
    </div>
    <div class="pager">
      <span class="muted">Showing {lo}&ndash;{hi} of {total}</span>
      <div class="pnums">{prev_b}{nums}{next_b}</div>
    </div>"""
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
        f'<span style="color:{c}">{s}</span></div>' for n, s, c in mods)

    body = f"""
    <div class="grid2">
      <div class="panel logpanel">
        <div class="ph"><h2>caphy.log &mdash; live tail</h2><span class="pill pg dotb">STREAMING</span></div>
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
    async function tail(){{
      try{{
        const r=await fetch('/api/logs?n=40'); const logs=await r.json();
        document.getElementById('logtail').innerHTML = logs.map(function(l){{
          const c=LC[l.level]||'var(--muted)';
          const mc=(['DETECT','ALERT','WARN','SYNC','TIER'].indexOf(l.level)>=0)?c:'var(--text)';
          return '<div class="logline"><span class="lt">'+l.t+'</span>'+
            '<span class="lvl" style="background:color-mix(in srgb,'+c+' 20%,transparent);color:'+c+'">'+l.level+'</span>'+
            '<span class="lm">'+l.mod+'</span><span style="color:'+mc+'">'+l.msg+'</span></div>';
        }}).join('');
      }}catch(e){{}}
    }}
    setInterval(tail,1500); tail();
    </script>"""
    return page("System Logs", "/logs", body,
                subtitle="Diagnostics &middot; detection pipeline &amp; sync events")


SETTINGS_TABS = [("detection", "Detection"), ("cameras", "Cameras"), ("alerts", "Alerts"),
                 ("storage", "Storage &amp; Sync"), ("voice", "Voice"),
                 ("users", "Users"), ("about", "About")]


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
            cur = request.form.get("cur_pw", "")
            new = request.form.get("new_pw", "")
            user = session.get("user", "admin")
            row = db.conn.execute("SELECT password_hash FROM users WHERE username=?", (user,)).fetchone()
            ok = row and new and row["password_hash"] == hashlib.sha256(cur.encode()).hexdigest()
            if ok:
                db.conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                                (hashlib.sha256(new.encode()).hexdigest(), user))
                db.conn.commit()
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
        <div class="togglerow"><div><b>Auto-arm at night</b>
          <div class="fdesc" style="margin:2px 0 0">Arm system on schedule &middot; {getattr(config,'AUTO_ARM_START_HOUR',22):02d}:00&ndash;{getattr(config,'AUTO_ARM_END_HOUR',6):02d}:00</div></div>{_sw("autoarm", auto_on)}</div>
        <div class="togglerow"><div><b>Highest Security</b>
          <div class="fdesc" style="margin:2px 0 0">Any confirmed person triggers a full Tier-3 response</div></div>{_sw("highest", highest_on)}</div>
        <div class="setfoot">
          <button class="btn ghost" name="action" value="reset">Reset defaults</button>
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
      <form method="post">
        <input type="hidden" name="section" value="cameras">
        <div class="sechead">Cameras</div><div class="subd">Name your cameras and review capture settings</div>
        {cam_inputs}
        <div class="secheadsmall">CAPTURE (edit config.py to change)</div>
        {_irow("Resolution", f"{config.FRAME_WIDTH} &times; {config.FRAME_HEIGHT}")}
        {_irow("Stream quality", f"{getattr(config,'JPEG_QUALITY',70)} / 100")}
        {_irow("Auto-scan cameras", "on" if getattr(config,'AUTO_SCAN_CAMERAS',False) else "off")}
        {_irow("Max cameras", getattr(config,'MAX_CAMERAS',2))}
        {_irow("Network cameras", extra_txt)}
        <div class="setfoot"><button class="btn" name="action" value="save">Save Changes</button></div>
      </form>"""

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

    # ---- Users ----
    pw = request.args.get("pw", "")
    msg = ('<div class="fdesc" style="color:var(--green)">Password updated.</div>' if pw == "ok"
           else '<div class="fdesc" style="color:var(--red)">Current password is incorrect.</div>' if pw == "err" else "")
    sec_users = f"""
      <form method="post">
        <input type="hidden" name="section" value="users">
        <div class="sechead">Users</div><div class="subd">Console account</div>
        {_irow("Signed in as", session.get("user","admin"))}
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

    sections = {"detection": sec_detection, "cameras": sec_cameras, "alerts": sec_alerts,
                "storage": sec_storage, "voice": sec_voice, "users": sec_users,
                "about": sec_about}
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
