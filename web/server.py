"""Phase 5 - CAPHY Desktop Dashboard (Flask web app).

Runs the detection engine in a background thread, streams the annotated camera
as MJPEG, and serves four pages that read caphy.db:
  /          Dashboard  (total alerts, camera status, current threat)
  /live      Live camera with bounding boxes
  /history   Event log with search + tier filter
  /settings  Sensitivity and camera settings
"""
import threading
import time

import cv2
import numpy as np
from flask import Flask, Response, request, redirect, url_for, render_template_string

import config
from detection.motion_detector import MotionDetector
from detection.two_factor import TwoFactorDetector
from detection.tier_engine import TierEngine
from storage.database import Database
from storage.alerts import AlertManager

app = Flask(__name__)

TIER_BGR = {1: (80, 200, 120), 2: (60, 160, 240), 3: (60, 60, 230)}


def _placeholder(text="Camera off"):
    img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 20, np.uint8)
    cv2.putText(img, text, (40, config.FRAME_HEIGHT // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 120, 120), 2)
    return img


def annotate(frame, result):
    """Draw boxes + tier onto a frame for the live stream."""
    for p in result.get("persons", []):
        x1, y1, x2, y2 = p["box"]
        c = TIER_BGR.get(p.get("tier", 2), (80, 200, 120))
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        label = f"{p.get('label', 'person')} {p['conf']:.2f} {p.get('distance_m', '?')}m"
        cv2.rectangle(frame, (x1, y1 - 18), (x1 + 240, y1), c, -1)
        cv2.putText(frame, label, (x1 + 4, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)
    if result.get("threat"):
        c = TIER_BGR[result["tier"]]
        cv2.rectangle(frame, (0, frame.shape[0] - 30), (frame.shape[1], frame.shape[0]), c, -1)
        cv2.putText(frame, f"THREAT - Tier {result['tier']}", (10, frame.shape[0] - 9),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return frame


class CameraWorker(threading.Thread):
    """Grabs frames, runs the two-factor engine, saves alerts, keeps the latest
    JPEG for the MJPEG stream. Runs in its own thread with its own DB handle."""
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = source
        self.lock = threading.Lock()
        self.jpeg = None
        self.stats = {"motion": False, "persons": 0, "tier": 0, "online": False}
        self.running = True

        self.motion = MotionDetector(config.MOTION_MIN_AREA, config.MOG2_HISTORY,
                                     config.MOG2_VAR_THRESHOLD, config.MOTION_BLUR)
        self.tier = TierEngine(config.DISTANCE_K, config.TIER1_MIN_DIST, config.TIER3_MAX_DIST)
        self.person = None
        try:
            from detection.person_detector import PersonDetector
            self.person = PersonDetector(config.YOLO_MODEL, config.PERSON_CLASS_ID, config.PERSON_CONF)
        except Exception as e:
            print(f"[CAPHY] YOLO not loaded ({e}) - motion only.")
        self.engine = TwoFactorDetector(self.motion, self.person, self.tier)

    def update_settings(self, sensitivity, person_conf):
        self.motion.min_area = float(sensitivity)
        if self.person is not None:
            self.person.conf = float(person_conf)

    def run(self):
        db = Database(config.DB_PATH)
        alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                              config.VIDEO_SECONDS, config.VIDEO_FPS,
                              config.SNAPSHOT_TIERS, config.VIDEO_TIERS)
        cap = cv2.VideoCapture(int(self.source) if str(self.source).isdigit() else self.source)
        while self.running:
            ok, frame = cap.read()
            if not ok:
                self._store(_placeholder(), {"online": False, "motion": False, "persons": 0, "tier": 0})
                time.sleep(0.5)
                continue
            result = self.engine.process(frame)
            annotate(frame, result)
            alerts.handle(frame, result)
            self._store(frame, {"online": True, "motion": result["motion"],
                                "persons": len(result["persons"]), "tier": result["tier"]})
        cap.release()
        db.close()

    def _store(self, frame, stats):
        ok, buf = cv2.imencode(".jpg", frame)
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()
                self.stats = stats

    def get_jpeg(self):
        with self.lock:
            return self.jpeg

    def get_stats(self):
        with self.lock:
            return dict(self.stats)


worker = None   # set by start_camera()


def start_camera(source):
    global worker
    worker = CameraWorker(source)
    worker.start()


# ---------------- HTML ----------------
BASE = """
<!doctype html><html><head><meta charset="utf-8"><title>CAPHY - {{title}}</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif}
 body{background:#0d151c;color:#e6edf3;display:flex;min-height:100vh}
 .side{width:210px;background:#14212b;border-right:1px solid #26404f;padding:20px 0;flex-shrink:0}
 .brand{padding:0 20px 18px;font-size:22px;font-weight:bold;letter-spacing:1px}
 .brand small{display:block;color:#2A9D8F;font-size:11px;font-weight:normal;letter-spacing:1px}
 .nav a{display:block;padding:11px 20px;color:#8aa0b0;text-decoration:none;font-size:14px}
 .nav a.active{color:#e6edf3;background:rgba(42,157,143,.15);border-left:3px solid #3dd7c4;font-weight:bold}
 .main{flex:1;padding:26px 32px}
 h1{font-size:22px;margin-bottom:4px}.sub{color:#8aa0b0;font-size:13px;margin-bottom:22px}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:22px}
 .card{background:#1a2a36;border:1px solid #26404f;border-radius:12px;padding:18px 20px;min-width:180px;flex:1}
 .card .k{color:#8aa0b0;font-size:11px;letter-spacing:1px;text-transform:uppercase}
 .card .v{font-size:30px;font-weight:bold;margin-top:8px}
 .card.t1{border-left:4px solid #3dd7c4}.card.t2{border-left:4px solid #F4A261}.card.t3{border-left:4px solid #e5484d}
 table{width:100%;border-collapse:collapse;background:#1a2a36;border-radius:12px;overflow:hidden}
 th{background:#2A9D8F;color:#fff;text-align:left;padding:10px 14px;font-size:12px}
 td{padding:9px 14px;border-top:1px solid #22333f;font-size:13px}
 .pill{padding:2px 10px;border-radius:10px;font-size:12px;font-weight:bold}
 .p1{background:rgba(61,215,196,.2);color:#3dd7c4}.p2{background:rgba(244,162,97,.2);color:#F4A261}.p3{background:rgba(229,72,77,.2);color:#e5484d}
 img.feed{width:100%;max-width:760px;border:1px solid #26404f;border-radius:12px;background:#000}
 input,select{background:#0d151c;border:1px solid #26404f;color:#e6edf3;padding:9px 12px;border-radius:8px;font-size:13px}
 .btn{background:#2A9D8F;color:#08110f;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer}
 label{display:block;color:#8aa0b0;font-size:12px;margin:14px 0 4px}
 .row{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
</style></head><body>
 <div class="side">
   <div class="brand">CAPHY<small>SECURITY CONSOLE</small></div>
   <div class="nav">
     <a href="/" class="{{'active' if page=='dash' else ''}}">Dashboard</a>
     <a href="/live" class="{{'active' if page=='live' else ''}}">Live Camera</a>
     <a href="/history" class="{{'active' if page=='hist' else ''}}">Alert History</a>
     <a href="/settings" class="{{'active' if page=='set' else ''}}">Settings</a>
   </div>
 </div>
 <div class="main">{{ body|safe }}</div>
</body></html>
"""


def page(title, page_id, body):
    return render_template_string(BASE, title=title, page=page_id, body=body)


def tier_pill(t):
    return f'<span class="pill p{t}">Tier {t}</span>'


@app.route("/")
def dashboard():
    db = Database(config.DB_PATH)
    total = db.count_alerts()
    tiers = {r["tier"]: r["c"] for r in db.conn.execute("SELECT tier, COUNT(*) c FROM alerts GROUP BY tier")}
    recent = db.recent_alerts(6)
    db.close()
    st = worker.get_stats() if worker else {"online": False, "tier": 0}
    cam = "1 / 1 online" if st.get("online") else "offline"
    cur = st.get("tier", 0)
    cur_txt = f"Tier {cur}" if cur else "clear"
    rows = "".join(
        f"<tr><td>{a['alert_id']}</td><td>{tier_pill(a['tier'])}</td>"
        f"<td>{a['distance_m']} m</td><td>{a['timestamp']}</td></tr>" for a in recent)
    body = f"""
    <h1>Dashboard</h1><div class="sub">Real-time overview - reads caphy.db</div>
    <div class="cards">
      <div class="card t1"><div class="k">Total Alerts</div><div class="v">{total}</div></div>
      <div class="card t2"><div class="k">Cameras</div><div class="v">{cam}</div></div>
      <div class="card t3"><div class="k">Current Threat</div><div class="v">{cur_txt}</div></div>
      <div class="card"><div class="k">Tier 1 / 2 / 3</div><div class="v">{tiers.get(1,0)} / {tiers.get(2,0)} / {tiers.get(3,0)}</div></div>
    </div>
    <h1 style="font-size:16px">Recent Alerts</h1><div class="sub">latest events</div>
    <table><tr><th>ID</th><th>Tier</th><th>Distance</th><th>Time</th></tr>{rows}</table>
    """
    return page("Dashboard", "dash", body)


@app.route("/live")
def live():
    body = """
    <h1>Live Camera</h1><div class="sub">Two-factor validation - boxes colored by tier</div>
    <img class="feed" src="/video_feed">
    """
    return page("Live Camera", "live", body)


@app.route("/history")
def history():
    tier = request.args.get("tier", "all")
    q = request.args.get("q", "").strip()
    db = Database(config.DB_PATH)
    sql = "SELECT * FROM alerts WHERE (?='all' OR tier=?) AND (?='' OR timestamp LIKE ?) ORDER BY alert_id DESC LIMIT 100"
    rows_db = db.conn.execute(sql, (tier, tier, q, f"%{q}%")).fetchall()
    db.close()
    opts = "".join(f'<option value="{t}" {"selected" if tier==t else ""}>{t}</option>'
                   for t in ["all", "1", "2", "3"])
    rows = "".join(
        f"<tr><td>{a['alert_id']}</td><td>{tier_pill(a['tier'])}</td><td>{a['distance_m']} m</td>"
        f"<td>{a['confidence']:.2f}</td><td>{a['timestamp']}</td>"
        f"<td>{(a['snapshot_path'] or '-')}</td></tr>" for a in rows_db)
    body = f"""
    <h1>Alert History</h1><div class="sub">{len(rows_db)} events</div>
    <form class="row" method="get">
      <input name="q" placeholder="search date e.g. 2026-07-11" value="{q}">
      <select name="tier">{opts}</select>
      <button class="btn">Filter</button>
    </form>
    <table><tr><th>ID</th><th>Tier</th><th>Distance</th><th>Conf</th><th>Time</th><th>Snapshot</th></tr>{rows}</table>
    """
    return page("Alert History", "hist", body)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = Database(config.DB_PATH)
    if request.method == "POST":
        sens = request.form.get("sensitivity", 1500)
        conf = request.form.get("person_conf", 0.5)
        armed = 1 if request.form.get("armed") == "on" else 0
        nv = 1 if request.form.get("night_vision") == "on" else 0
        db.conn.execute("UPDATE settings SET sensitivity=?, person_conf=?, armed=?, night_vision=? WHERE setting_id=1",
                        (sens, conf, armed, nv))
        db.conn.commit()
        if worker:
            worker.update_settings(sens, conf)
        db.close()
        return redirect(url_for("settings"))
    s = db.conn.execute("SELECT * FROM settings WHERE setting_id=1").fetchone()
    db.close()
    body = f"""
    <h1>Settings</h1><div class="sub">Detection sensitivity and camera options (saved to settings table)</div>
    <form method="post" style="max-width:420px">
      <label>Motion sensitivity (Factor 1 min area)</label>
      <input name="sensitivity" value="{s['sensitivity']}">
      <label>Person confidence (Factor 2, 0-1)</label>
      <input name="person_conf" value="{s['person_conf']}">
      <div class="row" style="margin-top:16px">
        <label style="margin:0"><input type="checkbox" name="armed" {'checked' if s['armed'] else ''}> Armed</label>
        <label style="margin:0"><input type="checkbox" name="night_vision" {'checked' if s['night_vision'] else ''}> Night vision</label>
      </div>
      <button class="btn" style="margin-top:14px">Save Changes</button>
    </form>
    """
    return page("Settings", "set", body)


@app.route("/video_feed")
def video_feed():
    def gen():
        while True:
            jpg = worker.get_jpeg() if worker else None
            if jpg is None:
                ok, buf = cv2.imencode(".jpg", _placeholder())
                jpg = buf.tobytes()
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.05)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")