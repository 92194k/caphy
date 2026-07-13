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
import csv
import hashlib
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np
from flask import (Flask, Response, request, redirect, url_for, session,
                   render_template_string, send_file, abort)

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

TIER_BGR = {1: (80, 200, 120), 2: (60, 160, 240), 3: (60, 60, 230)}


# ==================== camera / detection worker ====================
class Worker(threading.Thread):
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = source
        self.running = True
        self.lock = threading.Lock()
        self.jpeg = None
        self.stats = {"online": False, "motion": False, "person": False,
                      "tier": 0, "distance": "-", "conf": "-", "fps": 0.0, "armed": True}
        self.logs = deque(maxlen=200)
        self.switch_to = None

        self.motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY,
                                     config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
        self.tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST, config.TIER3_MAX_DIST)
        self.nv = NightVision(config.CLAHE_CLIP, config.CLAHE_TILE, config.NIGHT_LOW_LIGHT,
                              config.NIGHT_GAMMA, config.NIGHT_VISION_AUTO)
        self.person = None
        self.yolo_ok = False
        try:
            from detection.person_detector import PersonDetector
            self.person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
            self.yolo_ok = True
        except Exception as e:
            self._log("WARN", "yolo", f"not loaded ({e})")
        self.engine = TwoFactorDetector(self.motion, self.person, self.tier, config.PERSON_EVERY_N)
        self.siren = Siren()
        self.highest = config.HIGHEST_SECURITY

    def _log(self, level, mod, msg):
        self.logs.appendleft({"t": datetime.now().strftime("%H:%M:%S"),
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

    def switch_camera(self, index):
        self.switch_to = index

    def _annotate(self, frame, result):
        for p in result.get("persons", []):
            x1, y1, x2, y2 = p["box"]
            c = TIER_BGR.get(p.get("tier", 2), (80, 200, 120))
            cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
            lbl = f"{p.get('label','person')} {p['conf']:.2f} {p.get('distance_m','?')}m"
            cv2.rectangle(frame, (x1, y1 - 18), (x1 + 230, y1), c, -1)
            cv2.putText(frame, lbl, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)
        if result.get("threat"):
            c = TIER_BGR[result["tier"]]
            cv2.rectangle(frame, (0, frame.shape[0] - 28), (frame.shape[1], frame.shape[0]), c, -1)
            cv2.putText(frame, f"THREAT - Tier {result['tier']}", (8, frame.shape[0] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        return frame

    def run(self):
        db = Database(config.DB_PATH)
        alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                              config.SNAPSHOT_TIERS, config.RECORD_TIERS, config.PRESENCE_GRACE_SEC)
        push = PushSender(config.FIREBASE_KEY, config.PUSH_TOPIC)
        cap = cv2.VideoCapture(int(self.source) if str(self.source).isdigit() else self.source)
        self._log("INFO", "camera", f"opened source {self.source}")
        prev = time.time()
        while self.running:
            if self.switch_to is not None:
                cap.release()
                self.source = self.switch_to
                cap = cv2.VideoCapture(int(self.source))
                self._log("INFO", "camera", f"switched to CAM {self.source}")
                self.switch_to = None
            ok, frame = cap.read()
            if not ok:
                self._store_placeholder()
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

            siren_on = armed and result["threat"] and result["tier"] in config.SIREN_TIERS
            if siren_on:
                self.siren.start()
            else:
                self.siren.stop()

            self._annotate(frame, result)

            if armed:
                aid = alerts.handle(frame, result, fps)
                if aid is not None:
                    p = max(result["persons"], key=lambda x: x["tier"])
                    self._log("ALERT", "db", f"alert #{aid} Tier {result['tier']} saved")
                    if result["tier"] >= config.PUSH_MIN_TIER:
                        srow = db.conn.execute("SELECT snapshot_path FROM alerts WHERE alert_id=?", (aid,)).fetchone()
                        push.send(result["tier"], p["distance_m"], srow["snapshot_path"] if srow else None)
                        self._log("ALERT", "fcm", f"push Tier {result['tier']} sent")

            p0 = result["persons"][0] if result["persons"] else None
            self._store(frame, {"online": True, "motion": result["motion"],
                                "person": bool(result["persons"]), "tier": result["tier"],
                                "distance": (p0["distance_m"] if p0 else "-"),
                                "conf": (round(p0["conf"], 2) if p0 else "-"),
                                "fps": round(fps, 1), "armed": armed})
        cap.release()
        db.close()

    def _store(self, frame, stats):
        ok, buf = cv2.imencode(".jpg", frame)
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.stats = stats

    def _store_placeholder(self):
        img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
        cv2.putText(img, "Camera offline", (40, config.FRAME_HEIGHT // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (110, 110, 110), 2)
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.stats = {**self.stats, "online": False}

    def get_jpeg(self):
        with self.lock:
            return self.jpeg

    def get_stats(self):
        with self.lock:
            return dict(self.stats)

    def get_logs(self):
        with self.lock:
            return list(self.logs)


worker = None


def start_worker(source):
    global worker
    worker = Worker(source)
    worker.start()


# ==================== auth guard ====================
@app.before_request
def guard():
    if request.endpoint in ("login", "static"):
        return
    if not session.get("auth"):
        return redirect(url_for("login"))


# ==================== HTML / CSS ====================
CSS = """
 *{box-sizing:border-box;margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif}
 body{background:#0d151c;color:#e6edf3;display:flex;min-height:100vh}
 a{text-decoration:none;color:inherit}
 .side{width:220px;background:#14212b;border-right:1px solid #26404f;padding:18px 0;flex-shrink:0;position:fixed;height:100vh}
 .brand{display:flex;align-items:center;gap:10px;padding:0 20px 16px}
 .logo{width:36px;height:36px;border-radius:9px;background:#2A9D8F;display:flex;align-items:center;justify-content:center;font-weight:bold;color:#08110f}
 .brand b{font-size:19px;letter-spacing:1px}.brand small{display:block;color:#2A9D8F;font-size:10px;letter-spacing:1px}
 .nav a{display:flex;align-items:center;gap:10px;padding:11px 22px;color:#8aa0b0;font-size:14px}
 .nav a.active{color:#e6edf3;background:rgba(42,157,143,.15);border-left:3px solid #3dd7c4;font-weight:bold}
 .nav a:hover{color:#e6edf3}
 .sidefoot{position:absolute;bottom:16px;left:0;width:100%;padding:0 20px;color:#8aa0b0;font-size:12px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
 .main{flex:1;margin-left:220px;padding:22px 30px}
 .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
 h1{font-size:22px}.sub{color:#8aa0b0;font-size:13px;margin-bottom:20px}
 .pill{padding:3px 12px;border-radius:12px;font-size:12px;font-weight:bold;border:1px solid}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}
 .card{background:#1a2a36;border:1px solid #26404f;border-radius:12px;padding:18px 20px;flex:1;min-width:170px}
 .card .k{color:#8aa0b0;font-size:11px;letter-spacing:1px;text-transform:uppercase}
 .card .v{font-size:30px;font-weight:bold;margin-top:8px}
 .card.a1{border-left:4px solid #3dd7c4}.card.a2{border-left:4px solid #F4A261}.card.a3{border-left:4px solid #e5484d}.card.a4{border-left:4px solid #2A9D8F}
 .grid2{display:grid;grid-template-columns:2fr 1fr;gap:16px}
 .panel{background:#1a2a36;border:1px solid #26404f;border-radius:12px;padding:18px}
 .panel h2{font-size:15px;margin-bottom:12px}
 img.feed{width:100%;border-radius:10px;background:#000;display:block}
 table{width:100%;border-collapse:collapse}
 th{background:#2A9D8F;color:#fff;text-align:left;padding:9px 12px;font-size:11px;letter-spacing:.5px}
 td{padding:8px 12px;border-top:1px solid #22333f;font-size:13px}
 .p1{background:rgba(61,215,196,.18);color:#3dd7c4;border-color:#3dd7c4}
 .p2{background:rgba(244,162,97,.18);color:#F4A261;border-color:#F4A261}
 .p3{background:rgba(229,72,77,.18);color:#e5484d;border-color:#e5484d}
 .pg{background:rgba(63,185,80,.18);color:#3fb950;border-color:#3fb950}
 input,select{background:#0d151c;border:1px solid #26404f;color:#e6edf3;padding:9px 12px;border-radius:8px;font-size:13px}
 .btn{background:#2A9D8F;color:#08110f;border:none;padding:9px 16px;border-radius:8px;font-weight:bold;cursor:pointer;font-size:13px}
 .btn.ghost{background:transparent;border:1px solid #26404f;color:#8aa0b0}
 label{display:block;color:#8aa0b0;font-size:12px;margin:14px 0 4px}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
 .thumb{width:44px;height:32px;object-fit:cover;border-radius:5px;border:1px solid #26404f;background:#0a1319}
 .bar{display:inline-block;width:3.4%;background:#1c2b36;vertical-align:bottom;margin-right:1px;border-radius:2px 2px 0 0}
 .log{font-family:Consolas,monospace;font-size:12px;line-height:1.9}
 .lvl{padding:1px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px}
 .slider{width:100%;height:6px;background:#22333f;border-radius:3px}
 .slider .fill{height:6px;border-radius:3px;background:#2A9D8F}
 .toggle{width:42px;height:24px;border-radius:12px;position:relative;display:inline-block}
 .toggle .kn{width:18px;height:18px;border-radius:50%;background:#fff;position:absolute;top:3px}
"""

NAV = [("/", "Dashboard"), ("/live", "Live Camera"), ("/history", "Alert History"),
       ("/logs", "System Logs"), ("/settings", "Settings")]

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>CAPHY - {{title}}</title>
<style>""" + CSS + """</style></head><body>
<div class="side">
  <div class="brand"><div class="logo">C</div><div><b>CAPHY</b><small>SECURITY CONSOLE</small></div></div>
  <div class="nav">
  {% for href,label in nav %}
    <a href="{{href}}" class="{{'active' if page==href else ''}}">{{label}}</a>
  {% endfor %}
  </div>
  <div class="sidefoot">
    <span class="dot" style="background:{{'#3fb950' if armed else '#5c7180'}}"></span>{{'ARMED' if armed else 'DISARMED'}}<br>
    <span style="color:#5c7180">{{cam}}</span><br>
    <a href="/logout" style="color:#2A9D8F">Log out</a>
  </div>
</div>
<div class="main">{{ body|safe }}</div>
</body></html>"""


def page(title, href, body):
    st = worker.get_stats() if worker else {"armed": True, "online": False}
    cam = "1 camera online" if st.get("online") else "camera offline"
    return render_template_string(BASE, title=title, page=href, nav=NAV, body=body,
                                  armed=st.get("armed", True), cam=cam)


def tier_pill(t):
    return f'<span class="pill p{t}">Tier {t}</span>'


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
            return redirect(url_for("dashboard"))
        err = "Invalid username or password."
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8">
    <title>CAPHY - Login</title><style>""" + CSS + """
    body{align-items:center;justify-content:center}
    .box{background:#1a2a36;border:1px solid #26404f;border-radius:16px;padding:36px;width:360px}
    </style></head><body>
    <div class="box">
      <div style="text-align:center;margin-bottom:8px"><div class="logo" style="width:56px;height:56px;font-size:24px;margin:0 auto 12px">C</div>
      <b style="font-size:26px;letter-spacing:2px">CAPHY</b><div style="color:#2A9D8F;font-size:12px">SECURITY CONSOLE</div></div>
      <form method="post">
        <label>USERNAME</label><input name="username" value="admin" style="width:100%">
        <label>PASSWORD</label><input name="password" type="password" style="width:100%">
        <div style="color:#e5484d;font-size:12px;margin-top:10px">{{err}}</div>
        <button class="btn" style="width:100%;margin-top:18px;padding:12px">Sign In</button>
      </form>
      <div style="color:#5c7180;font-size:11px;text-align:center;margin-top:16px">Default: admin / admin</div>
    </div></body></html>""", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    db = Database(config.DB_PATH)
    total = db.count_alerts()
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    recent = db.recent_alerts(6)
    hours = {int(r["h"]): r["c"] for r in db.conn.execute(
        "SELECT strftime('%H', timestamp) h, COUNT(*) c FROM alerts "
        "WHERE timestamp >= datetime('now','-1 day') GROUP BY h")}
    db.close()
    st = worker.get_stats() if worker else {"tier": 0, "online": False}
    cur = st.get("tier", 0)
    maxh = max(hours.values()) if hours else 1
    bars = "".join(
        f'<span class="bar" style="height:{6 + int(40*hours.get(h,0)/maxh)}px;'
        f'background:{"#e5484d" if hours.get(h,0)>=maxh and hours.get(h,0)>0 else "#2A9D8F"}"></span>'
        for h in range(24))
    rrows = "".join(
        f'<tr><td>{a["alert_id"]}</td><td>{tier_pill(a["tier"])}</td>'
        f'<td>{a["distance_m"]} m</td><td>{a["timestamp"][-8:]}</td></tr>' for a in recent)
    body = f"""
    <div class="top"><h1>Dashboard</h1><span class="pill pg">● LIVE</span></div>
    <div class="sub">Real-time overview · reads caphy.db</div>
    <div class="cards">
      <div class="card a1"><div class="k">Total Alerts</div><div class="v">{total}</div></div>
      <div class="card a4"><div class="k">Cameras</div><div class="v">{'1 / 1' if st.get('online') else '0 / 1'}</div></div>
      <div class="card a3"><div class="k">Current Threat</div><div class="v">{'Tier '+str(cur) if cur else 'Clear'}</div></div>
      <div class="card a2"><div class="k">Pending Sync</div><div class="v">{pending}</div></div>
    </div>
    <div class="grid2">
      <div class="panel"><h2>Live Feed</h2><img class="feed" src="/video_feed"></div>
      <div class="panel"><h2>Recent Alerts</h2>
        <table><tr><th>ID</th><th>Tier</th><th>Dist</th><th>Time</th></tr>{rrows}</table></div>
    </div>
    <div class="panel" style="margin-top:16px"><h2>Threat Activity — last 24h</h2>
      <div style="height:52px">{bars}</div>
      <div style="color:#5c7180;font-size:10px;margin-top:4px">00:00 &nbsp;&nbsp; 06:00 &nbsp;&nbsp; 12:00 &nbsp;&nbsp; 18:00 &nbsp;&nbsp; 24:00</div>
    </div>"""
    return page("Dashboard", "/", body)


@app.route("/live")
def live():
    st = worker.get_stats() if worker else {}
    f1 = "MOTION" if st.get("motion") else "no motion"
    f2 = "PERSON" if st.get("person") else "no person"
    mcol = "#F4A261" if st.get("motion") else "#5c7180"
    pcol = "#3fb950" if st.get("person") else "#5c7180"
    logs = worker.get_logs()[:12] if worker else []
    logrows = "".join(
        "<div><span style='color:#5c7180'>%s</span> <span style='color:#3dd7c4'>%s</span> %s</div>"
        % (l["t"], l["mod"], l["msg"]) for l in logs)
    body = f"""
    <div class="top"><h1>Live Camera</h1><span class="pill p3">&#9679; REC</span></div>
    <div class="sub">Two-factor validation - boxes colored by tier</div>
    <div class="grid2">
      <div class="panel"><img class="feed" src="/video_feed">
        <div class="row" style="margin-top:12px">
          <form method="post" action="/switch/0"><button class="btn ghost">CAM 0 (built-in)</button></form>
          <form method="post" action="/switch/1"><button class="btn ghost">CAM 1 (USB)</button></form>
        </div>
      </div>
      <div>
        <div class="panel"><h2>Detection</h2>
          <div class="row"><span class="dot" style="background:{mcol}"></span>Factor 1 - {f1}</div>
          <div class="row"><span class="dot" style="background:{pcol}"></span>Factor 2 - {f2}</div>
          <div class="row">Distance: <b style="color:#F4A261">{st.get('distance','-')} m</b></div>
          <div class="row">Confidence: <b>{st.get('conf','-')}</b> &nbsp; FPS: <b>{st.get('fps','-')}</b></div>
        </div>
        <div class="panel" style="margin-top:16px"><h2>Two-Factor Log</h2>
          <div class="log">{logrows}</div></div>
      </div>
    </div>"""
    return page("Live Camera", "/live", body)


@app.route("/switch/<int:idx>", methods=["POST"])
def switch(idx):
    if worker:
        worker.switch_camera(idx)
    return redirect(url_for("live"))


@app.route("/history")
def history():
    import os
    tier = request.args.get("tier", "all")
    q = request.args.get("q", "").strip()
    db = Database(config.DB_PATH)
    sql = ("SELECT * FROM alerts WHERE (?='all' OR tier=?) AND (?='' OR timestamp LIKE ?) "
           "ORDER BY alert_id DESC LIMIT 150")
    rows = db.conn.execute(sql, (tier, tier, q, "%" + q + "%")).fetchall()
    db.close()
    opts = "".join("<option value='%s' %s>%s</option>" % (t, "selected" if tier == t else "", t)
                   for t in ["all", "1", "2", "3"])
    trows = ""
    for a in rows:
        snap = a["snapshot_path"]
        thumb = ("<img class='thumb' src='/snapshot/%s'>" % os.path.basename(snap)) if snap else "<div class='thumb'></div>"
        event = "Person confirmed" if a["tier"] else "Movement"
        trows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>Front Gate</td><td>%s m</td><td>%.2f</td><td>%s</td></tr>"
                  % (thumb, tier_pill(a["tier"]), event, a["distance_m"], a["confidence"], a["timestamp"][-8:]))
    body = f"""
    <div class="top"><h1>Alert History</h1><a class="btn" href="/export">Export CSV</a></div>
    <div class="sub">{len(rows)} events</div>
    <form class="row" method="get">
      <input name="q" placeholder="search date e.g. 2026-07-13" value="{q}" style="width:280px">
      <select name="tier">{opts}</select>
      <button class="btn">Filter</button>
    </form>
    <div class="panel" style="padding:0;overflow:hidden">
    <table><tr><th>Snapshot</th><th>Tier</th><th>Event</th><th>Camera</th><th>Distance</th><th>Conf</th><th>Time</th></tr>
    {trows}</table></div>"""
    return page("Alert History", "/history", body)


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
    rows = db.conn.execute("SELECT alert_id,tier,distance_m,confidence,snapshot_path,video_path,synced,timestamp "
                           "FROM alerts ORDER BY alert_id DESC").fetchall()
    db.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "tier", "distance_m", "confidence", "snapshot", "video", "synced", "timestamp"])
    for r in rows:
        w.writerow([r["alert_id"], r["tier"], r["distance_m"], r["confidence"],
                    r["snapshot_path"], r["video_path"], r["synced"], r["timestamp"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=caphy_alerts.csv"})


@app.route("/logs")
def logs():
    logs = worker.get_logs() if worker else []
    lvlc = {"INFO": "#8aa0b0", "DETECT": "#3dd7c4", "ALERT": "#2A9D8F", "WARN": "#F4A261"}
    logrows = "".join(
        "<div><span style='color:#5c7180'>%s</span> <span class='lvl' style='background:%s33;color:%s'>%s</span>"
        "<span style='color:#5c7180'>%s</span> %s</div>"
        % (l["t"], lvlc.get(l["level"], "#8aa0b0"), lvlc.get(l["level"], "#8aa0b0"), l["level"], l["mod"], l["msg"])
        for l in logs)
    if psutil:
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        bat = psutil.sensors_battery()
        batt = ("%d%%%s" % (int(bat.percent), " (charging)" if bat.power_plugged else "")) if bat else "N/A"
    else:
        cpu = mem = disk = 0
        batt = "install psutil"
    st = worker.get_stats() if worker else {}
    db = Database(config.DB_PATH)
    pending = db.conn.execute("SELECT COUNT(*) c FROM alerts WHERE synced=0").fetchone()["c"]
    db.close()
    ycol = "#3fb950" if (worker and worker.yolo_ok) else "#e5484d"
    scol = "#F4A261" if pending else "#3fb950"
    mods = [("OpenCV capture", "running", "#3fb950"),
            ("YOLOv8-nano", "running" if worker and worker.yolo_ok else "off", ycol),
            ("Tier engine", "running", "#3fb950"),
            ("Tagalog voice", "standby (English)", "#8aa0b0"),
            ("Cloud sync", (str(pending) + " pending") if pending else "synced", scol)]
    modrows = "".join(
        "<div class='row' style='justify-content:space-between;margin:0;padding:7px 0;border-top:1px solid #22333f'>"
        "<span><span class='dot' style='background:%s'></span>%s</span>"
        "<span style='color:%s;font-size:12px'>%s</span></div>" % (c, n, c, s) for n, s, c in mods)

    def metric(name, val, frac, color="#2A9D8F"):
        return ("<div style='margin-bottom:14px'><div class='row' style='justify-content:space-between;margin:0'>"
                "<span style='color:#8aa0b0;font-size:12px'>%s</span><b>%s</b></div>"
                "<div class='slider'><div class='fill' style='width:%s%%;background:%s'></div></div></div>"
                % (name, val, frac, color))
    fpsval = float(st.get("fps", 0) or 0)
    health = (metric("CPU", "%s%%" % cpu, cpu) + metric("Memory", "%s%%" % mem, mem)
              + metric("Disk", "%s%%" % disk, disk, "#F4A261") + metric("Battery", batt, 100)
              + metric("Model FPS", str(st.get("fps", "-")), min(fpsval * 3, 100), "#3fb950"))
    body = f"""
    <div class="top"><h1>System Logs</h1><span class="pill pg">&#9679; STREAMING</span></div>
    <div class="sub">Detection pipeline and sync events - refresh to update</div>
    <div class="grid2">
      <div class="panel" style="background:#070d12"><h2>caphy.log - recent</h2><div class="log">{logrows}</div></div>
      <div>
        <div class="panel"><h2>System Health</h2>{health}</div>
        <div class="panel" style="margin-top:16px"><h2>Module Status</h2>{modrows}</div>
      </div>
    </div>"""
    return page("System Logs", "/logs", body)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = Database(config.DB_PATH)
    if request.method == "POST":
        sens = request.form.get("sensitivity", 1500)
        conf = request.form.get("person_conf", 0.5)
        t1 = request.form.get("tier1", config.TIER1_MIN_DIST)
        t3 = request.form.get("tier3", config.TIER3_MAX_DIST)
        night = 1 if request.form.get("night") == "on" else 0
        auto = 1 if request.form.get("autoarm") == "on" else 0
        highest = 1 if request.form.get("highest") == "on" else 0
        db.conn.execute("UPDATE settings SET sensitivity=?, person_conf=?, armed=?, night_vision=? WHERE setting_id=1",
                        (sens, conf, auto, night))
        db.conn.commit()
        if worker:
            worker.update_settings(sens, conf, t1, t3, night, highest)
        db.close()
        return redirect(url_for("settings"))
    s = db.conn.execute("SELECT * FROM settings WHERE setting_id=1").fetchone()
    db.close()

    def tog(name, on, label, desc):
        c = "#2A9D8F" if on else "#22333f"
        kn = "22px" if on else "3px"
        chk = "checked" if on else ""
        return ("<div class='row' style='justify-content:space-between'><div><b>%s</b>"
                "<div style='color:#8aa0b0;font-size:12px'>%s</div></div>"
                "<label class='toggle' style='background:%s'><input type='checkbox' name='%s' %s "
                "style='opacity:0;position:absolute'><span class='kn' style='left:%s'></span></label></div>"
                % (label, desc, c, name, chk, kn))
    highest_on = worker.highest if worker else False
    toggles = (tog("night", s["night_vision"], "Software Night Vision (CLAHE)", "Auto-brighten low-light frames")
               + tog("autoarm", s["armed"], "Auto-arm", "Keep the system armed")
               + tog("highest", highest_on, "Highest Security", "Any confirmed person triggers full Tier-3 response"))
    body = f"""
    <div class="top"><h1>Settings</h1></div>
    <div class="sub">Tune the two-factor pipeline and tier thresholds</div>
    <form method="post" style="max-width:560px">
      <div class="panel">
        <label>Motion Sensitivity (Factor 1 min area)</label>
        <input name="sensitivity" value="{s['sensitivity']}" style="width:100%">
        <label>Person Confidence (Factor 2, 0-1)</label>
        <input name="person_conf" value="{s['person_conf']}" style="width:100%">
        <div class="row" style="margin-top:16px">
          <div style="flex:1"><label>Tier 1 distance &ge; (m)</label><input name="tier1" value="{config.TIER1_MIN_DIST}" style="width:100%"></div>
          <div style="flex:1"><label>Tier 3 distance &le; (m)</label><input name="tier3" value="{config.TIER3_MAX_DIST}" style="width:100%"></div>
        </div>
      </div>
      <div class="panel" style="margin-top:16px">{toggles}</div>
      <button class="btn" style="margin-top:16px;padding:11px 22px">Save Changes</button>
    </form>"""
    return page("Settings", "/settings", body)


@app.route("/video_feed")
def video_feed():
    def gen():
        while True:
            jpg = worker.get_jpeg() if worker else None
            if jpg is None:
                img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
                jpg = cv2.imencode(".jpg", img)[1].tobytes()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.05)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")