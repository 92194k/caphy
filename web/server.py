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
                   render_template, send_file, abort, jsonify,
                   stream_with_context)

import sys
import config
from storage.sync import internet_available

# RTSP low-latency tuning for IP cameras (Tapo, etc) - OFF by default.
#
# A previous version of this forced rtsp_transport;tcp + nobuffer/low_delay/
# max_delay unconditionally for every RTSP source. That's the textbook fix
# for RTSP lag, but on at least one real setup it made the camera's live
# view get stuck on "Camera loading..." / show a black screen instead - the
# forced TCP transport or the aggressive max_delay cutoff is very likely
# incompatible with that specific camera/network combo (some cameras only
# publish an RTSP server that behaves correctly over UDP, or GOP-align in a
# way that a tight max_delay cuts off before a keyframe arrives). A global
# default that can silently break an existing working camera is worse than
# the lag it was trying to fix, so this now only activates when explicitly
# opted into via config.py (RTSP_LOW_LATENCY = True) - safe to try, easy to
# turn back off if a specific camera doesn't like it. Must run before any
# cv2.VideoCapture() call touches an RTSP URL - OpenCV's FFmpeg backend
# reads this env var lazily when it opens the stream, so this just needs to
# run before Worker._open_capture() does (module load time, well before
# any camera is actually opened, is early enough).
if getattr(config, "RTSP_LOW_LATENCY", False):
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000",
    )
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

# ==================== system armed/unarmed state ====================
# Global armed state - when True, all tiers trigger full high-alert response
SYSTEM_ARMED = False
_armed_lock = threading.Lock()

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
        # Cameras start CLOSED by default - the user must explicitly open
        # each one (or press "Open All Cameras") before it captures anything.
        self.paused = True              # camera OFF: release the device, stop detecting
        self.lock = threading.Lock()
        self.jpeg = None
        self.raw_frame = None    # last raw BGR frame, for the WebRTC track
        self.manual_record = False      # toggled by the phone Live tab
        self._mrec = None
        self._mrec_path = None
        self.last_record = None         # basename of the last finished recording
        self.stats = {"online": False, "motion": False, "person": False,
                      "tier": 0, "distance": "-", "conf": "-", "fps": 0.0,
                      "armed": True, "camera_on": False}
        self.logs = deque(maxlen=200)
        # Heartbeat: timestamp of the last successfully-processed frame, so
        # System Health can tell "camera open, detecting fine" apart from
        # "camera open, but the detection loop silently died" - previously
        # both looked identical (a frozen last frame, yolo_ok still True).
        self._last_frame_ok = 0.0

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

    def _publish_and_push(self, aid, result, armed):
        """The genuinely SLOW, NETWORK part of handling an alert - Firestore
        cloud publish and FCM push notification. Runs on its own background
        thread (started from run() via threading.Thread) so however long
        these network calls take can never delay the next frame capture.

        Deliberately does NOT include alerts.handle() (snapshot save, video
        writer, local SQLite insert) - that part MUST stay on the Worker's
        own thread and use the single long-lived AlertManager instance
        run() already owns, for two reasons a previous version of this fix
        got wrong: (1) AlertManager tracks video-recording state
        (self._writer) ACROSS calls - a person staying in frame means
        multiple handle() calls should all write to the SAME ongoing
        recording. Creating a fresh AlertManager per detection (as an
        earlier version of this fix did) reset that state every time,
        starting a NEW video file on every detection instead of continuing
        one, and abandoned each previous AlertManager's still-open
        cv2.VideoWriter without ever calling .release() on it - a real
        leak (file handles + encoder buffers) that compounds with two
        cameras running and gets worse the longer the app runs, which
        matches the "smooth at first, RAM climbs, everything grinds to a
        halt" symptom exactly. (2) local disk/SQLite writes are fast -
        there was never a need to background them; only the actual network
        calls (Firestore, FCM) are slow enough to threaten frame timing.

        Opens its OWN Database connection here (not run()'s) for the same
        check_same_thread reason as before - sqlite3 connections default
        to check_same_thread=True, so a connection must only be used from
        the thread that created it, and this runs on a different thread
        than run()'s own database reads.
        """
        db = Database(config.DB_PATH)
        try:
            self._log("ALERT", "db", f"alert #{aid} Tier {result['tier']} saved")
            p = max(result["persons"], key=lambda x: x["tier"])
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
                            vid_path = arow["video_path"] if "video_path" in arow.keys() else None
                            # At this exact moment (right when the alert row
                            # is first written) a Tier 2/3 alert's video
                            # usually hasn't finished recording yet - it
                            # keeps going for a grace period after the
                            # person leaves frame, so video_path is often
                            # still empty here even though ajson['has_video']
                            # will end up true once recording finishes.
                            published = cloud_alerts.publish_alert(
                                owner, get_device_identity()["device_id"],
                                ajson, snap_path, vid_path)
                            # Only mark synced=1 if we didn't skip a video
                            # that's expected to exist - otherwise a video
                            # that finishes AFTER this point would never get
                            # uploaded, since start_cloud_sync_retry() only
                            # re-checks rows still marked synced=0. Leaving
                            # this row at synced=0 for now means the retry
                            # sweep will pick it up again once video_path is
                            # actually populated in the database, and
                            # publish it (with the video) on that next pass.
                            video_pending = ajson.get("has_video") and not vid_path
                            if published and not video_pending:
                                db.conn.execute(
                                    "UPDATE alerts SET synced=1 WHERE alert_id=?", (aid,))
                                db.conn.commit()
                    except Exception as ce:
                        self._log("WARN", "alerts", f"cloud publish failed: {ce}")
            except Exception as e:
                self._log("WARN", "alerts", f"broadcast failed: {e}")
            # Push gate (alert fatigue): armed -> notify every tier;
            # disarmed -> notify Tier 3 only. Either way the alert was
            # already saved above, so nothing is lost - Tier 1/2 just
            # stay silent in the list until you open the app.
            #
            # Disarm grace: a Tier 3 walk-by in the seconds right after
            # disarming shouldn't fire an urgent push either, for the same
            # reason it doesn't sound the siren (see _disarm_grace_until in
            # run() above) - the alert row/snapshot is still saved either
            # way, only the push is held back briefly.
            in_disarm_grace = (not armed) and time.time() < _disarm_grace_until
            should_push = (armed or (result["tier"] >= 3)) and not in_disarm_grace
            if should_push:
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
                        device_id = get_device_identity()["device_id"]
                        sender = get_push_sender_for_alert(owner_uid, device_id)
                        if sender:
                            push_title = f"CAPHY Alert - Tier {result['tier']}"
                            push_body = f"{self.name}: person detected ({p['distance_m']}m)"
                            push_data = {"alert_id": str(aid), "camera": self.name}
                            sent = sender.send_alert(
                                title=push_title, body=push_body,
                                data=push_data, tier=result["tier"],
                            )
                            if sent:
                                self._log("ALERT", "fcm", f"push Tier {result['tier']} sent (owner-scoped)")
                            else:
                                # send_alert() returns False on failure (e.g. no
                                # internet right now) - previously that just meant
                                # the notification was silently lost forever.
                                # Queue it so start_cloud_sync_retry() can retry
                                # once connectivity is back, same as it already
                                # does for unsynced alert rows.
                                db.queue_pending_push(
                                    aid, owner_uid, device_id,
                                    push_title, push_body, push_data, result["tier"])
                                self._log("WARN", "fcm", f"push Tier {result['tier']} failed, queued for retry")
                except Exception as e:
                    self._log("WARN", "fcm", f"scoped push failed: {e}")
        finally:
            # Always close this thread's own connection when done - it was
            # opened solely for this one background task and nothing else
            # in the process holds a reference to it.
            db.close()

    def run(self):
        # This connection is only ever used by run()'s own thread - both
        # the armed-status check each loop iteration AND alerts.handle()
        # below (snapshot save, video-writer state, local SQLite insert).
        # _publish_and_push() opens its own SEPARATE Database on whatever
        # background thread it runs on, specifically to avoid sharing a
        # sqlite3 connection across threads (see that method's docstring).
        db = Database(config.DB_PATH)
        # ONE persistent AlertManager for this camera's whole lifetime -
        # NOT recreated per detection. Its self._writer (an open
        # cv2.VideoWriter) has to survive across many consecutive
        # alerts.handle() calls while a person stays in frame, so the
        # SAME instance must keep being reused; a fresh AlertManager per
        # detection was the actual bug behind the ~7GB RAM climb (every
        # detection abandoned the previous instance's still-open
        # VideoWriter without ever calling .release() on it, and reset
        # the "currently recording" state so it never continued one
        # recording, it kept starting new ones instead).
        alerts = AlertManager(db, config.CAPTURES_DIR, config.ALERT_COOLDOWN_SEC,
                              config.SNAPSHOT_TIERS, config.RECORD_TIERS, config.PRESENCE_GRACE_SEC,
                              self.name,
                              videos_dir=getattr(config, "VIDEOS_DIR", config.CAPTURES_DIR))
        # Cameras start closed by default (self.paused=True) - don't touch the
        # device at all until the user explicitly opens it. Opening and
        # immediately releasing it here would still briefly grab the camera
        # (visible as the webcam light flashing on for USB cams) for no reason.
        cap = None if self.paused else self._open_capture()
        prev = time.time()
        # MOG2's background model is empty on the first frames, which can
        # flag the whole frame as "motion" and fire a bogus alert the instant
        # the app starts (before there's any real background to compare
        # against). Give the detector a short warm-up window per camera
        # start/restart during which detections are computed (so MOG2 keeps
        # learning the background) but never turned into a saved alert.
        startup_warmup_until = time.time() + getattr(config, "STARTUP_WARMUP_SEC", 3.0)
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
                # camera just (re)started - MOG2 needs to relearn the background
                startup_warmup_until = time.time() + getattr(config, "STARTUP_WARMUP_SEC", 3.0)

            ok, frame = cap.read()
            if not ok:
                self._store_placeholder()
                _update_siren(self.cam_id, False)
                time.sleep(0.4)
                continue

            # Everything below this point (night vision, motion, YOLO, tier
            # classification, alert saving, push, cloud sync) used to run with
            # NO exception guard. A single bad frame or a failed file write
            # (e.g. cv2.imwrite to a snapshot path, a locked DB, a malformed
            # detection box) would raise, and since this is a bare daemon
            # thread with nothing supervising it, the whole detection loop for
            # this camera died silently - forever. The last successfully
            # stored frame kept being served on the live feed and the
            # System Health panel kept showing "YOLOv8-nano: running" (that
            # flag is only set once at startup, never re-checked), so the
            # camera LOOKED fine while zero alerts could ever fire again.
            # Now: log the failure loudly, mark a heartbeat miss, and keep the
            # loop alive so the next frame gets a fresh chance.
            try:
                frame, nv_on = self.nv.process(frame)
                result = self.engine.process(frame)
                in_startup_warmup = time.time() < startup_warmup_until

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

                # Sync armed state to tier engine
                self.tier.set_armed(armed)

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

                # Siren per the armed/disarmed x tier table:
                #   Tier 3 -> siren in EITHER mode (already the max response)
                #   Tier 2 -> siren only when ARMED (disarmed Tier 2 is
                #             snapshot/notify + a conditional short clip,
                #             no alarm - someone lingering nearby while
                #             you're just going about your day shouldn't
                #             sound an alarm)
                #   Tier 1 -> never sirens, either mode
                # Two grace windows gate this regardless of tier:
                #   - just-armed grace: arming while still in frame doesn't
                #     instantly blast the siren
                #   - just-DISARMED grace: the walk-by right after turning
                #     the system off doesn't treat the owner as an
                #     intruder for Tier 3 (Tier 2 already can't siren
                #     disarmed at all, so this only matters for Tier 3)
                now_ts = time.time()
                in_arm_grace_siren = armed and now_ts < _arm_grace_until
                in_disarm_grace_siren = (not armed) and now_ts < _disarm_grace_until
                siren_tier_ok = (result["tier"] == 3) or (result["tier"] == 2 and armed)
                siren_on = (result["threat"] and siren_tier_ok
                            and not in_arm_grace_siren and not in_disarm_grace_siren)
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

                # IMPORTANT: store the frame for live viewers (MJPEG + WebRTC)
                # RIGHT HERE, immediately after annotation - BEFORE running any
                # of the alert pipeline below (snapshot save, SQLite writes,
                # Firestore upload, FCM push). Those are all network/disk I/O
                # that can each take anywhere from tens of ms to multiple
                # seconds, especially the cloud publish and push notification
                # calls. This used to run BEFORE _store(), which meant every
                # live viewer (both the local MJPEG stream and the phone's
                # remote WebRTC video) would freeze/stutter for however long
                # that whole pipeline took, EVERY time a detection fired -
                # reported as "video stops stops on detection" specifically
                # over WebRTC/mobile data, where the extra network calls
                # (cloud publish, push) are slowest. Moving _store() up here
                # means viewers always get the newest frame within one loop
                # iteration regardless of how slow the alert/cloud pipeline
                # is - video no longer waits on it at all.
                p0 = result["persons"][0] if result["persons"] else None
                self._store(frame, {"online": True, "motion": result["motion"],
                                    "person": bool(result["persons"]), "tier": result["tier"],
                                    "distance": (p0["distance_m"] if p0 else "-"),
                                    "conf": (round(p0["conf"], 2) if p0 else "-"),
                                    "fps": round(fps, 1), "armed": armed,
                                    "camera_on": True})
                self._last_frame_ok = time.time()

                # Alert-fatigue policy:
                #   - DETECT & SAVE all tiers ALWAYS (even disarmed), each with its
                #     snapshot, so the alert history is complete and Tier 1/2 are
                #     there to review - they just don't buzz your phone.
                #   - PUSH a phone notification only for Tier 3 when disarmed; when
                #     ARMED, push every tier (you're actively guarding, you want to
                #     know about everything).
                # The just-armed grace window still suppresses alerts right after
                # arming (so walking away from the laptop doesn't alert on you).
                # in_startup_warmup covers the camera's own first few seconds,
                # regardless of armed state - MOG2 hasn't learned the background
                # yet, so anything it flags this early is noise, not a real alert.
                in_arm_grace = armed and time.time() < _arm_grace_until
                if not in_arm_grace and not in_startup_warmup:
                    # alerts.handle() stays INLINE here, on this same loop
                    # thread - it's local disk/SQLite work (snapshot save,
                    # video-writer frame write, one small insert), all fast,
                    # and it MUST run on the persistent `alerts` instance
                    # created once in run() above so its video-writer state
                    # (self._writer) correctly carries over from one call to
                    # the next while a person stays in frame. Only the
                    # actual network calls - Firestore publish, FCM push -
                    # are slow enough to threaten frame timing, so only
                    # THOSE get handed to a background thread below.
                    try:
                        aid = alerts.handle(frame, result, fps, armed=armed)
                    except Exception as e:
                        self._log("ERROR", "alerts", f"alert pipeline failed: {e}")
                        aid = None
                    if aid is not None:
                        # frame is not needed beyond this point by the
                        # background task (only aid/result/armed are), so
                        # no frame.copy() is needed here - _publish_and_push
                        # only reads from the DB row it fetches itself.
                        threading.Thread(
                            target=self._publish_and_push,
                            args=(aid, result, armed),
                            daemon=True,
                        ).start()
            except Exception as e:
                # This is the fix: a failure anywhere in detection/alerting no
                # longer kills the camera's thread. Log it loudly, mark the
                # heartbeat as missed (so System Health can show a real
                # "stalled" state instead of a stale "running"), and let the
                # loop pick back up on the next frame.
                self._log("ERROR", "loop", f"detection frame failed: {e}")
                self._last_frame_ok = getattr(self, "_last_frame_ok", 0)
                time.sleep(0.2)
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

    def _configure_and_probe(self, cap):
        """Applies our capture settings, then reads ONE real frame and times
        it. Returns (ok, elapsed_seconds) - ok is False if the camera never
        opened or that first read failed outright."""
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, getattr(config, "CAP_BUFFERSIZE", 1))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
        except Exception:
            pass
        if not cap.isOpened():
            return False, None
        t0 = time.time()
        ok, _ = cap.read()
        return ok, (time.time() - t0)

    def _open_capture(self):
        """Open the video source. Returns a VideoCapture (possibly not opened).

        On Windows with an integer camera index (built-in/USB webcams, not
        RTSP/URL sources), tries DirectShow (CAP_DSHOW) first - it's the
        traditionally more reliable backend and works fine for most
        cameras. BUT some webcams (this varies by model/driver, laptop
        built-in cameras especially) run DirectShow in a mode that makes
        every single cap.read() block for a long time waiting on the
        driver - which shows up as near-0% CPU usage combined with a
        crawling frame rate (e.g. ~1 fps), since the thread is genuinely
        just sitting idle waiting for the OS/driver, not doing real work
        CAPHY could speed up by itself. If that's detected (first-frame
        read takes noticeably long), automatically retries with Media
        Foundation (CAP_MSMF) instead - a well-known, well-documented
        OpenCV/Windows fix for exactly this symptom on certain camera/driver
        combinations. Whichever backend actually works is logged clearly so
        it's visible in System Logs, instead of silently picking one.
        """
        src = self.source
        if isinstance(src, str) and src.isdigit():
            src = int(src)

        if isinstance(src, int) and os.name == "nt":
            SLOW_FIRST_FRAME_SEC = 0.5   # a healthy camera's first read is near-instant

            cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
            ok, elapsed = self._configure_and_probe(cap)
            if ok and elapsed is not None and elapsed < SLOW_FIRST_FRAME_SEC:
                self._log("INFO", "camera",
                          f"{self.name}: opened source {self.source} via DirectShow "
                          f"(first frame in {elapsed*1000:.0f}ms)")
                return cap

            # DirectShow either failed to open, failed its first read, or
            # was slow enough to explain a crawling fps - try Media
            # Foundation instead before giving up.
            reason = "did not open/read" if not ok else f"first frame took {elapsed*1000:.0f}ms (too slow)"
            self._log("WARN", "camera",
                      f"{self.name}: DirectShow {reason} - trying Media Foundation instead")
            cap.release()
            cap2 = cv2.VideoCapture(src, cv2.CAP_MSMF)
            ok2, elapsed2 = self._configure_and_probe(cap2)
            if ok2:
                self._log("INFO", "camera",
                          f"{self.name}: opened source {self.source} via Media Foundation "
                          f"(first frame in {elapsed2*1000:.0f}ms)")
                return cap2

            # Neither backend worked well - fall back to whichever
            # DirectShow handle we originally had (matches old behavior)
            # rather than returning nothing, so the rest of the app's
            # "camera not opening" handling (which already exists and is
            # user-visible) still applies normally.
            cap2.release()
            self._log("WARN", "camera",
                      f"{self.name}: Media Foundation also did not open/read - "
                      "falling back to DirectShow anyway (camera unplugged, wrong "
                      "RTSP URL/password, or in use by another app?)")
            return cv2.VideoCapture(src, cv2.CAP_DSHOW)

        # RTSP/URL source (an IP camera like a Tapo). Same self-testing
        # philosophy as the DirectShow/MSMF fallback above: TRY the
        # low-latency FFmpeg options (TCP transport, no internal buffering -
        # the standard fix for RTSP lag) first, but PROBE it the same way
        # (first-frame timing) before committing to it, and fall straight
        # back to a plain, untouched cv2.VideoCapture() if the tuned
        # version fails to open or doesn't deliver a frame quickly. A
        # global env-var toggle (the previous approach) meant one bad
        # camera/network combo broke live view entirely with no recovery
        # short of a manual config edit + restart; this way each camera
        # gets the speed-up if its RTSP server tolerates it, and silently
        # keeps working at default speed if it doesn't - never breaks.
        is_rtsp = isinstance(src, str) and src.lower().startswith("rtsp://")
        if is_rtsp and getattr(config, "RTSP_LOW_LATENCY", True):
            prev_opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000"
            )
            try:
                cap = cv2.VideoCapture(src)
                # _configure_and_probe() already applies CAP_PROP_BUFFERSIZE/
                # FRAME_WIDTH/FRAME_HEIGHT internally - no need to set them
                # twice here.
                ok, elapsed = self._configure_and_probe(cap)
                SLOW_FIRST_FRAME_SEC = 2.0  # RTSP handshake is inherently slower than a local webcam
                if ok and elapsed is not None and elapsed < SLOW_FIRST_FRAME_SEC:
                    self._log("INFO", "camera",
                              f"{self.name}: opened source {self.source} with low-latency "
                              f"RTSP options (first frame in {elapsed*1000:.0f}ms)")
                    return cap
                reason = "did not open/read" if not ok else f"first frame took {elapsed*1000:.0f}ms (too slow)"
                self._log("WARN", "camera",
                          f"{self.name}: low-latency RTSP options {reason} - "
                          "falling back to default RTSP settings for this camera")
                cap.release()
            finally:
                # Restore whatever was there before (nothing, normally) so
                # this camera's tuning doesn't leak into any OTHER camera's
                # capture opened later on the same process.
                if prev_opts is None:
                    os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
                else:
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = prev_opts

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


# Cache of REAL Windows device names (e.g. "DroidCam Video", "ACER HD User
# Facing", "Iriun Webcam", "Camera (NVIDIA Broadcast)") - the same names you
# see in a Google Meet / Zoom camera picker. Populated lazily by
# _real_device_names() so every camera_label() call doesn't re-enumerate
# DirectShow devices (that call has noticeable latency).
_device_name_cache = None


def _real_device_names():
    """Returns {index: friendly_name} using pygrabber's DirectShow device
    enumeration on Windows, e.g. {0: "ACER HD User Facing Camera",
    1: "DroidCam Video", 2: "Iriun Webcam"} - in the SAME index order
    cv2.VideoCapture(i, cv2.CAP_DSHOW) uses, so index i's real name is
    exactly what a user would see for that same index in any other app's
    camera picker (Zoom, Meet, etc).

    Returns {} (empty) on any failure - not installed, not on Windows, or
    DirectShow enumeration errors - so callers ALWAYS have a safe fallback
    to the generic "USB / External Camera N" label. This is optional
    enrichment, never a hard requirement for camera detection to work."""
    global _device_name_cache
    if _device_name_cache is not None:
        return _device_name_cache

    _device_name_cache = {}
    if os.name != "nt":
        return _device_name_cache
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
        _device_name_cache = {i: n for i, n in enumerate(names)}
    except Exception as e:
        print(f"[CAPHY] Real camera device names unavailable ({e}); "
              f"using generic labels instead. Install with: pip install pygrabber")
    return _device_name_cache


def camera_label(source):
    """Friendly name for a camera source - the REAL Windows device name
    when available (e.g. "DroidCam Video", "Iriun Webcam"), so the Settings
    -> Cameras picker shows the same names you'd see in any other app's
    camera dropdown, not a generic guess. Falls back to a sensible generic
    label (index 0 = built-in, others = "USB / External Camera N") if the
    real name can't be read (pygrabber missing, non-Windows, or the device
    disappeared between enumeration and lookup)."""
    if isinstance(source, str):
        return "Network Camera"
    real = _real_device_names().get(source)
    if real:
        return real
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
# UI: TWO separate dropdowns - "Camera 1" and "Camera 2" - each listing every
# camera device Windows/OpenCV can see right now (built-in webcam, USB
# cameras, ...). Each dropdown defaults to whichever source is currently
# running in that slot. Pick a device for each slot, press Apply.
_CAMERA_PICKER_JS = """
<script>
let camAvail = [], camMax = 2, camSlots = [null, null];
// rescan=true forces the server to re-read real device names too (a camera
// plugged in AFTER this page opened needs a fresh DirectShow enumeration,
// not just a fresh port scan) - see api_cameras_available()'s ?rescan=1.
async function loadCameras(rescan){
  const st = document.getElementById('camPickStatus');
  if(!st) return;
  st.textContent = rescan ? 'Rescanning\\u2026' : 'Detecting cameras\\u2026';
  document.getElementById('camApplyBtn').disabled = true;
  try{
    const r = await fetch('/api/cameras/available' + (rescan ? '?rescan=1' : ''));
    const d = await r.json();
    camAvail = d.cameras || [];
    camMax = d.max || 2;
    // pre-fill each slot with whichever camera is currently active there,
    // in order - so Camera 1's dropdown defaults to the first active source,
    // Camera 2's to the second.
    const activeSources = camAvail.filter(c => c.active).map(c => String(c.source));
    camSlots = [activeSources[0] || null, activeSources[1] || null];
    renderCameraSlots();
    st.textContent = camAvail.length ? '' : 'No cameras detected. Plug one in and press Rescan.';
  }catch(e){ st.textContent = 'Could not detect cameras.'; }
}
function renderCameraSlots(){
  for(let slot = 0; slot < camMax; slot++){
    const dd = document.getElementById('camSlot' + slot);
    if(!dd) continue;
    dd.innerHTML = '';
    const noneOpt = document.createElement('option');
    noneOpt.value = ''; noneOpt.textContent = '(none)';
    dd.appendChild(noneOpt);
    camAvail.forEach(function(c){
      const id = String(c.source);
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = c.label + (c.active ? '  (currently in use)' : '');
      if(id === camSlots[slot]) opt.selected = true;
      dd.appendChild(opt);
    });
    dd.onchange = function(){ camSlots[slot] = dd.value || null; checkCameraConflict(); };
  }
  document.getElementById('camApplyBtn').disabled = !camSlots.some(s => s);
}
function checkCameraConflict(){
  const msg = document.getElementById('camApplyMsg');
  const picked = camSlots.filter(s => s);
  const dupes = picked.length !== new Set(picked).size;
  if(dupes){
    msg.textContent = 'Camera 1 and Camera 2 must be different devices.';
    msg.style.color = 'var(--orange, #e5a13f)';
    document.getElementById('camApplyBtn').disabled = true;
  } else {
    msg.textContent = '';
    document.getElementById('camApplyBtn').disabled = !picked.length;
  }
}
async function applyCameras(){
  const btn = document.getElementById('camApplyBtn'), msg = document.getElementById('camApplyMsg');
  const sources = camSlots.filter(s => s).map(s => /^\\d+$/.test(s) ? parseInt(s) : s);
  if(!sources.length) return;
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
// Auto-detect the moment this page opens - NO button press needed. The
// dropdowns you see are already live-detected; Rescan (below) is only for
// picking up a camera plugged in AFTER the page was already open.
if(document.getElementById('camSlot0')) loadCameras(false);
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
    _threading.Thread(target=_start_command_listener,
                      daemon=True).start()
    _threading.Thread(target=_run_command_poll_backstop,
                      daemon=True).start()
    _threading.Thread(target=_run_pairing_request_poll_loop,
                      daemon=True).start()
    _threading.Thread(target=_watch_command_listener_health,
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
        ok = _push_state_heartbeat_now(device_id)
        if ok:
            if first_ok:
                print("[CAPHY] Cloud heartbeat OK - this laptop is now "
                      "reachable from the phone over the internet.")
                first_ok = False
        else:
            first_ok = True   # so recovery is logged too
        # This periodic tick is now just a BACKSTOP - the real
        # responsiveness fix is that every state-changing route (arm,
        # camera power, emergency, siren) calls _push_state_heartbeat_now()
        # itself immediately after changing state (see _push_state_async
        # below), so a phone that's off the laptop's WiFi sees the new
        # state within roughly a second of the change, not up to 20s later.
        # Lowered from 20s anyway (was the ONLY update mechanism before)
        # so anything that mutates state without going through those
        # routes, or a missed push due to a transient Firestore error,
        # still self-corrects quickly rather than leaving stale cloud state
        # sitting for up to 20 seconds.
        time.sleep(5)


def _build_state_snapshot():
    """Builds the small state snapshot published to Firestore so a phone
    off the LAN can see real armed/camera/emergency/siren status instead of
    guessing. Factored out of the heartbeat loop so state-changing routes
    (api_arm, api_camera_power, api_emergency, api_siren) can push an
    updated snapshot immediately after changing state, instead of the
    phone having to wait for the next periodic heartbeat tick to see it."""
    try:
        from web.webrtc_stream import AIORTC_AVAILABLE as _webrtc_ok
    except Exception:
        _webrtc_ok = False
    try:
        return {
            "armed": _is_armed(),
            "camera_on": any(not w.paused for w in workers) if workers else False,
            "emergency": bool(_emergency_on),
            "siren": bool(_siren_manual),
            "webrtc_available": bool(_webrtc_ok),
            "cameras": [
                {"cam": w.cam_id, "name": w.name,
                 "on": not w.paused,
                 "online": bool(w.get_stats().get("online", False))}
                for w in workers
            ],
        }
    except Exception:
        return None


def _push_state_heartbeat_now(device_id=None):
    """Publishes the current state snapshot (+ LAN address + TURN creds) to
    Firestore right now, synchronously. Returns True on success. Used both
    by the periodic heartbeat loop and by _push_state_async (called from
    every state-mutating route) so a remote phone's status view catches up
    to a local change almost immediately instead of on the next ~20s tick -
    this was the root cause of "status wrong after toggling" cross-network:
    the phone's own optimistic UI update was correct, but the next routine
    poll of Firestore was reading a heartbeat snapshot that hadn't been
    refreshed yet, so it looked like the toggle had reverted."""
    try:
        from storage import device_registry
        if device_id is None:
            from identity import get_device_identity
            device_id = get_device_identity()["device_id"]
        snapshot = _build_state_snapshot()
        turn_servers = None
        try:
            from web.webrtc_stream import get_ice_servers_cached
            turn_servers, _turn_ok = get_ice_servers_cached()
        except Exception:
            turn_servers = None
        device_registry.heartbeat(device_id, lan_ip=_local_ip(),
                                  lan_port=5000, state=snapshot,
                                  turn_servers=turn_servers)
        return True
    except Exception as e:
        print(f"[CAPHY] Heartbeat skipped (offline?): {e}")
        return False


def _push_state_async():
    """Fire-and-forget state push, called from state-mutating routes right
    after they change something. Runs on a short-lived background thread so
    the HTTP response to the phone/laptop UI that triggered the change is
    never delayed waiting on a Firestore round-trip - the whole point is to
    make the NEXT status read (from anywhere) fast, not to slow down the
    action that's happening right now."""
    import threading as _threading
    _threading.Thread(target=_push_state_heartbeat_now, daemon=True).start()


_command_worker_pool = None   # ThreadPoolExecutor, created lazily
_command_watch = None         # the active Firestore Watch, so we can stop/restart it
_seen_command_ids = None      # bounded de-dupe cache, belt-and-suspenders on top of claim_command()


def _get_command_worker_pool():
    """Dedicated thread pool for EXECUTING remote commands, kept completely
    separate from camera/YOLO/recording work. Commands are dispatched here
    the instant the Firestore listener sees them, so a slow frame (YOLO
    inference, video encode, upload, etc) on the camera threads can never
    delay ARM/DISARM/SIREN/SNAPSHOT/ACKNOWLEDGE - and, symmetrically, a
    command's own HTTP round-trip into Flask can't stall the listener
    thread from noticing the next command. A small fixed pool (not
    unbounded) still protects against a burst of many commands starving
    the process."""
    global _command_worker_pool
    if _command_worker_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _command_worker_pool = ThreadPoolExecutor(max_workers=4,
                                                   thread_name_prefix="caphy-cmd")
    return _command_worker_pool


def _on_pending_command(device_id, cmd):
    """Callback fired by the Firestore realtime listener the moment a
    command doc becomes 'pending' - runs on Firestore's own listener thread,
    so it must be fast and non-blocking: claim (or skip if already claimed/
    stale) and hand off to the worker pool, then return immediately."""
    global _seen_command_ids
    if _seen_command_ids is None:
        from collections import OrderedDict
        _seen_command_ids = OrderedDict()

    cmd_id = cmd.get("id")
    if not cmd_id:
        return
    # Cheap in-process de-dupe: on_snapshot can redeliver the same doc (e.g.
    # right after a reconnect) before Firestore itself has reflected our
    # claim_command() write yet. This is just an optimization to skip an
    # unnecessary transaction attempt - claim_command() is what actually
    # guarantees single execution.
    if cmd_id in _seen_command_ids:
        return
    _seen_command_ids[cmd_id] = True
    if len(_seen_command_ids) > 200:
        _seen_command_ids.popitem(last=False)

    print(f"[CAPHY] Remote command received: {cmd.get('type')} (id={cmd_id})")
    _get_command_worker_pool().submit(_claim_and_execute_remote_command, device_id, cmd)


def _claim_and_execute_remote_command(device_id, cmd):
    """Runs on a worker-pool thread. Claims the command via an atomic
    Firestore transaction (the real duplicate-execution guard - safe even
    if this fires more than once for the same doc) and, only if the claim
    was won, executes it."""
    from storage import device_registry
    cmd_id = cmd.get("id")
    try:
        if not device_registry.claim_command(device_id, cmd_id):
            print(f"[CAPHY] Command {cmd_id} already claimed - skipping duplicate")
            return   # already claimed/handled by another delivery of this event
    except Exception as e:
        print(f"[CAPHY] Command claim failed for {cmd_id}: {e}")
        return
    print(f"[CAPHY] Command {cmd_id} claimed, executing...")
    try:
        _execute_remote_command(device_id, cmd)
        print(f"[CAPHY] Command {cmd_id} finished executing")
    except Exception as e:
        print(f"[CAPHY] Command {cmd_id} execution raised: {e}")
        try:
            device_registry.complete_command(device_id, cmd_id, {"error": str(e)}, ok=False)
        except Exception:
            pass


def _start_command_listener():
    """Attaches the realtime Firestore listener for the commands queue.
    Replaces the old 1s poll loop - commands now execute the instant
    Firestore pushes the change instead of waiting for the next tick, and
    execution happens on a dedicated worker pool (see
    _get_command_worker_pool) so it can never be blocked by, or block,
    camera/AI processing. Retries with backoff if the listener drops
    (network hiccup, laptop was asleep, etc) so reconnection is automatic -
    no app restart needed on either end.

    Also starts a lightweight SAFETY-NET poll (_run_command_poll_backstop)
    alongside the realtime watch. The watch is a live gRPC stream that can
    in rare cases stall or silently stop delivering events without ever
    raising an exception anywhere catchable - if that happens with ONLY a
    listener and no backstop, remote commands would go dead with zero
    visible error on either end, which is unacceptable this close to a
    defense. The backstop polls every 5s (far cheaper than the old 1s loop,
    since it only needs to catch what the realtime path missed) and calls
    the exact same claim-then-execute path, so claim_command()'s atomic
    transaction guarantees the realtime and poll paths can never both
    execute the same command."""
    global _command_watch
    from identity import get_device_identity
    from storage import device_registry
    device_id = get_device_identity()["device_id"]

    backoff = 2
    while True:
        try:
            _command_watch = device_registry.watch_pending_commands(
                device_id, lambda cmd: _on_pending_command(device_id, cmd))
            print("[CAPHY] Remote command listener attached (realtime).")
            backoff = 2
            return
        except Exception as e:
            print(f"[CAPHY] Remote command listener failed to attach "
                  f"(offline?): {e} - retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _run_command_poll_backstop():
    """Safety net that runs alongside the realtime listener (see
    _start_command_listener's docstring for why). Polls every 5s and routes
    anything it finds through the SAME claim_command()-guarded path the
    realtime listener uses, so this can never cause a duplicate execution -
    it can only catch commands the realtime path missed."""
    from identity import get_device_identity
    device_id = get_device_identity()["device_id"]
    time.sleep(25)   # let the realtime listener attach first
    while True:
        try:
            from storage import device_registry
            for cmd in device_registry.pending_commands(device_id):
                cmd_id = cmd.get("id")
                print(f"[CAPHY] Backstop poll caught command {cmd_id} "
                      f"(realtime listener may have missed it)")
                _get_command_worker_pool().submit(
                    _claim_and_execute_remote_command, device_id, cmd)
        except Exception as e:
            print(f"[CAPHY] Command backstop poll skipped (offline?): {e}")
        time.sleep(5)


_command_listener_last_event = 0.0


def _watch_command_listener_health():
    """Firestore's on_snapshot watch runs on its own internal gRPC thread and
    can die quietly on a network drop (sleep/wake, wifi switch, laptop
    roaming networks) without ever raising an exception anywhere we'd catch
    it - the symptom would be "remote commands stop working until the app is
    restarted", which is exactly the failure mode this feature must not
    have. To guard against that without needing a true low-level heartbeat
    from the watch itself, this periodically nudges reconnection by checking
    whether Firestore is reachable at all and, if the watch object reports
    itself closed/errored, re-attaches it. Cheap (~ once/60s) and fully
    automatic - no app restart needed on either side."""
    time.sleep(30)
    while True:
        try:
            global _command_watch
            watch = _command_watch
            is_dead = watch is None
            if not is_dead:
                # google-cloud-firestore's Watch exposes _closed once the
                # underlying stream has terminated; treat any inability to
                # read that as "assume alive" so this check fails open
                # rather than restarting a perfectly healthy listener.
                is_dead = bool(getattr(watch, "_closed", False))
            if is_dead:
                print("[CAPHY] Remote command listener appears down - reattaching...")
                _start_command_listener()
        except Exception as e:
            print(f"[CAPHY] Command listener health check error: {e}")
        time.sleep(60)


def _run_pairing_request_poll_loop():
    """QR-pairing confirmation requests remain on a short poll - this path
    is a one-time, human-initiated action (scanning a QR code), not a
    repeated remote-control action, so polling latency here doesn't produce
    the "buttons feel dead" problem ARM/DISARM/SIREN/etc had. Kept
    unchanged/separate on purpose to minimize the blast radius of this
    change."""
    time.sleep(20)   # let registration land first
    while True:
        try:
            from storage import device_registry
            for req in device_registry.pending_pairing_requests_for_device(
                    __import__("identity").get_device_identity()["device_id"]):
                _execute_confirm_pairing(req)
        except Exception as e:
            print(f"[CAPHY] Pairing-request poll skipped (offline?): {e}")
        time.sleep(4)


def _execute_remote_command(device_id, cmd):
    """Runs one queued remote command against THIS laptop's own local Flask
    app, so remote and local control share one source of truth."""
    from storage import device_registry
    import requests as _requests

    cmd_id = cmd["id"]
    cmd_type = cmd.get("type")
    cmd_args = cmd.get("args") or {}
    try:
        route_map = {
            "arm": ("POST", "/api/arm", {"on": True}),
            "disarm": ("POST", "/api/arm", {"on": False}),
            "camera_on": ("POST", "/api/camera/power", {"on": True}),
            "camera_off": ("POST", "/api/camera/power", {"on": False}),
            "emergency_on": ("POST", "/api/emergency", {"on": True}),
            "emergency_off": ("POST", "/api/emergency", {"on": False}),
            # Snapshot/record were hardcoded to camera 0 regardless of which
            # camera the phone actually had selected - if you were viewing
            # camera 1 and hit snapshot cross-network, it silently
            # snapshotted camera 0 instead. Now uses whichever cam index
            # the phone actually sent (defaults to 0 only if it didn't).
            "snapshot": ("POST", f"/api/snapshot/{int(cmd_args.get('cam', 0) or 0)}", {}),
            # Record had NO entry here at all before - Api.record() in
            # api.dart had no cross-network fallback either, so "Record"
            # simply did nothing on mobile data. /api/record/<cam> is a
            # pure toggle (no body needed) that returns the finished
            # video's URL when stopping, same as the direct-HTTP path -
            # the phone's route through here returns that same shape.
            "record": ("POST", f"/api/record/{int(cmd_args.get('cam', 0) or 0)}", {}),
            "siren": ("POST", "/api/siren", {}),
            # Acknowledge (dismiss) an alert - added because the phone's
            # dismissAlert()/dismissAllAlerts() previously only ever tried
            # a DIRECT HTTP call to the laptop's LAN address, with no
            # fallback through this remote-command queue at all (unlike
            # arm/disarm/siren/etc, which already had one). On a different
            # network than the laptop (e.g. phone on mobile data, laptop on
            # Wi-Fi), that direct call always fails, dismissAlert() returns
            # false, and the UI's failure handler re-loads the whole alert
            # list - which is exactly what looked like "Acknowledge keeps
            # reloading / doesn't work" cross-network. Routing it through
            # here (same pattern as every other remote action) fixes that.
            "dismiss_alert": ("POST", f"/api/alert/{cmd_args.get('id')}/dismiss", {}),
            "dismiss_all_alerts": ("POST", "/api/alerts/dismiss_all", {}),
            # Same cross-network fallback as dismiss_alert above, for the
            # phone's new delete action (permanently removes the alert +
            # its snapshot/video, not just hides it).
            "delete_alert": ("POST", f"/api/alert/{cmd_args.get('id')}/delete", {}),
            # Voice assistant, routed cross-network - added because
            # Api.assistant() in api.dart previously had ONLY a direct HTTP
            # call with no fallback at all (unlike every action button,
            # which already had one), so a spoken command like "arm the
            # system" would fail with "can't reach the laptop" on mobile
            # data even though the exact same action via the Arm BUTTON
            # worked fine cross-network through this same queue. The
            # laptop still does all AI classification and the Groq call
            # itself (see api_assistant/_execute_remote_command below) -
            # only the transport (direct HTTP vs Firestore queue) changes
            # depending on network, same as every other action.
            "assistant": ("POST", "/api/assistant", {"text": cmd_args.get("text", "")}),
        }
        if cmd_type not in route_map:
            device_registry.complete_command(device_id, cmd_id,
                                             {"error": "unknown command"}, ok=False)
            return
        if cmd_type in ("dismiss_alert", "delete_alert") and not cmd_args.get("id"):
            device_registry.complete_command(device_id, cmd_id,
                                             {"error": "missing alert id"}, ok=False)
            return
        if cmd_type == "assistant" and not cmd_args.get("text"):
            device_registry.complete_command(device_id, cmd_id,
                                             {"error": "missing text"}, ok=False)
            return
        method, path, body = route_map[cmd_type]
        # The assistant needs a longer timeout than instant actions like
        # arm/siren - it calls Groq (up to REQUEST_TIMEOUT_SECONDS=8s in
        # assistant_ai/groq_client.py) PLUS classification/action-execution
        # overhead on top, so the old fixed timeout=10 used for every
        # command type here was genuinely too tight and likely the real
        # cause of "voice commands fail on mobile data" - the internal
        # loopback call itself was timing out and getting reported as a
        # failed command before Groq ever had a chance to finish.
        request_timeout = 20 if cmd_type == "assistant" else 10
        if cmd_type == "assistant":
            print(f"[CAPHY] Remote voice command received (id={cmd_id}): "
                  f"{cmd_args.get('text', '')!r}")
        r = _requests.request(method, f"http://127.0.0.1:5000{path}",
                              json=body, timeout=request_timeout,
                              headers={"X-CAPHY-Internal": _INTERNAL_TOKEN})
        if cmd_type == "assistant":
            print(f"[CAPHY] Remote voice command (id={cmd_id}) got HTTP "
                  f"{r.status_code} from /api/assistant.")
        # Commands whose caller needs actual structured fields back (not
        # just "did it succeed") get the full parsed JSON body, same as
        # assistant already did - "body": r.text[:500] only ever gave the
        # phone a raw (possibly truncated) string, which is why record()'s
        # video_url (needed to save the finished clip to the gallery) and
        # similar fields were unusable when a command came through this
        # cross-network path instead of the direct-HTTP one.
        NEEDS_PARSED_BODY = ("assistant", "record", "snapshot")
        if cmd_type in NEEDS_PARSED_BODY:
            try:
                parsed = r.json()
            except Exception:
                parsed = ({"type": "error", "reply": "CAPHY couldn't process that just now."}
                          if cmd_type == "assistant" else {})
            device_registry.complete_command(
                device_id, cmd_id, parsed, ok=r.status_code == 200)
        else:
            device_registry.complete_command(
                device_id, cmd_id,
                {"status_code": r.status_code, "body": r.text[:500]},
                ok=r.status_code == 200)
    except Exception as e:
        print(f"[CAPHY] Remote command {cmd_id} (type={cmd_type}) raised: {e}")
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
    if request.path in ("/api/auth/signup", "/api/auth/signup/verify",
                        "/api/auth/google", "/logout",
                        "/auth/google/start", "/auth/google/callback",
                        "/api/auth/google/status", "/forgot-password"):
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
NAV = [("/", "Dashboard"), ("/live", "Live Camera"),
       ("/history", "Alert History"), ("/logs", "System Logs"), ("/settings", "Settings")]

# Cache-busting version for style.css: a short hash of the file's own
# contents, computed once at startup. Browsers were caching an old copy of
# this stylesheet indefinitely (Flask's static file serving sends long-lived
# cache headers with no versioned URL), so CSS fixes -- like the profile
# dropdown background/visibility fix -- silently never reached the browser
# even after a normal refresh. Appending ?v=<hash> to the <link> URL forces
# the browser to fetch a fresh copy any time this file's contents change.
def _css_version():
    try:
        css_path = os.path.join(app.static_folder, "style.css")
        with open(css_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:10]
    except Exception:
        return "0"


CSS_VER = _css_version()


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
    # Prefer the display name (set at signup, via Google, or changed later
    # in Settings > Users) over the raw email - falls back to email if no
    # name was ever set, so this never shows a blank name in the header.
    display_name = session.get("display_name", "").strip()
    shown_user = display_name if display_name else session.get("email", "admin")
    return render_template("base.html", title=title, page=href, nav=NAV, body=body,
                           subtitle=subtitle, ncam=online, user=shown_user,
                           armed=st.get("armed", True), cam=cam, nalerts=unread,
                           css_ver=CSS_VER)


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
    session["display_name"] = _get_display_name(result["uid"])
    session.permanent = remember
    if session.permanent:
        app.permanent_session_lifetime = timedelta(days=30)
    _mirror_local_user(result["uid"], email)
    return redirect(url_for("dashboard"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Forgot-password PIN flow (see password_reset.py).

    Two steps rendered by the same template/route:
      - "request": user types their email, we email a 6-digit PIN.
      - "verify":  user types the PIN + a new password.

    Deliberately shows the same generic "PIN sent" message whether or not
    the email actually has an account, so this page can't be used to probe
    which emails are registered.
    """
    import password_reset

    if request.method == "GET":
        return render_template("forgot_password.html", step="request")

    action = request.form.get("action", "request")
    email = request.form.get("email", "").strip()

    if action == "request":
        if not email:
            return render_template("forgot_password.html", step="request",
                                    msg=None)
        sent, reason = password_reset.request_reset_pin(email)
        if not sent:
            print(f"[CAPHY Auth] /forgot-password: PIN not sent to {email} - {reason}")
        return render_template(
            "forgot_password.html", step="verify", email=email, error=None)

    if action == "verify":
        pin = request.form.get("pin", "").strip()
        new_password = request.form.get("new_password", "")
        ok, err = password_reset.verify_and_reset(email, pin, new_password)
        if ok:
            return redirect(url_for("login"))
        return render_template("forgot_password.html", step="verify", email=email, error=err)

    return render_template("forgot_password.html", step="request")


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


def _get_display_name(uid):
    """Looks up the Firebase Auth displayName for a uid, via the Admin SDK -
    one shared helper so every login path (email/password, signup, Google
    web, Google desktop) fills session['display_name'] the same way,
    instead of four separate lookups drifting apart. Returns "" (never
    raises) if the user has no name set or the lookup fails, so callers can
    just fall back to email - never a hard requirement."""
    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        user = fb_auth.get_user(uid)
        return user.display_name or ""
    except Exception:
        return ""


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
    Step 1 of signup: validate the form and email a 6-digit confirmation
    PIN (see password_reset.request_signup_pin). No Firebase account is
    created here - that only happens in /api/auth/signup/verify, once the
    PIN is confirmed. This is what stops someone from creating an account
    with an email they don't actually control.
    """
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")
    name = data.get("name", "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters"}), 400

    import password_reset
    sent, error = password_reset.request_signup_pin(email, name, password)
    if not sent:
        return jsonify({"success": False, "error": error}), 400

    return jsonify({"success": True, "pending": True})


@app.route("/api/auth/signup/verify", methods=["POST"])
def api_signup_verify():
    """
    Step 2 of signup: confirm the PIN emailed in /api/auth/signup, and only
    now actually create the Firebase Auth account (via
    password_reset.verify_signup_pin) - the exact same identity store the
    phone's createUserWithEmailAndPassword() writes into. This is what
    makes "create on desktop, log in on phone" actually work: the account
    did not exist anywhere until this call, and after this call it exists
    in Firebase Auth, reachable from either platform.
    """
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    pin = data.get("pin", "").strip()

    if not email or not pin:
        return jsonify({"success": False, "error": "Email and PIN required"}), 400

    import password_reset
    uid, error = password_reset.verify_signup_pin(email, pin)
    if not uid:
        return jsonify({"success": False, "error": error}), 400

    session["user_uid"] = uid
    session["email"] = email
    session["display_name"] = ""  # backfilled by _get_display_name below if set
    session["display_name"] = _get_display_name(uid)
    session.permanent = True
    _mirror_local_user(uid, email)

    return jsonify({"success": True, "uid": uid})


@app.route("/api/auth/change-password/request", methods=["POST"])
def api_change_password_request():
    """
    Step 1 of the signed-in "Change Password" flow (Settings > User
    Account). Verifies the CURRENT password against Firebase Auth first -
    exactly the same check the old direct-save version of this form did -
    and only if that passes does it stage the new password and email a
    6-digit confirmation PIN (password_reset.request_change_pin). Nothing
    about the real account changes yet; that only happens in
    /api/auth/change-password/verify once the PIN is confirmed.

    Requires an active session - this is NOT the forgot-password flow for
    a signed-out user, it's a second factor on top of a password the user
    already correctly entered.
    """
    uid = session.get("user_uid")
    email = session.get("email", "")
    if not uid or not email:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json() or {}
    cur_pw = data.get("current_password", "")
    new_pw = data.get("new_password", "")

    if not new_pw or len(new_pw) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters."}), 400

    verify = _firebase_rest_sign_in(email, cur_pw)
    if verify.get("uid") != uid:
        return jsonify({"success": False, "error": "Current password is incorrect."}), 400

    import password_reset
    sent, error = password_reset.request_change_pin(uid, email, new_pw)
    if not sent:
        return jsonify({"success": False, "error": error}), 400

    return jsonify({"success": True, "pending": True,
                    "expires_in": password_reset.CHANGE_PW_PIN_TTL_MINUTES * 60})


@app.route("/api/auth/change-password/verify", methods=["POST"])
def api_change_password_verify():
    """
    Step 2: confirm the PIN emailed in change-password/request, and only
    now actually update the Firebase Auth password
    (password_reset.verify_change_pin). Deliberately does NOT touch the
    Flask session in any way - the user stays signed in throughout, same
    as before this feature existed. Every other account/auth behavior is
    unchanged.
    """
    uid = session.get("user_uid")
    if not uid:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json() or {}
    pin = data.get("pin", "").strip()
    if not pin:
        return jsonify({"success": False, "error": "Code required"}), 400

    import password_reset
    ok, error = password_reset.verify_change_pin(uid, pin)
    if not ok:
        return jsonify({"success": False, "error": error}), 400

    return jsonify({"success": True})


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
    intent = data.get("intent", "signin")  # "signin" or "signup"

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
        # Google's tokeninfo response includes the account's profile name
        # (e.g. "Kem Alaokhemberly") - captured here exactly like an
        # email/password signup with a name field would.
        google_name = payload.get("name", "")
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
            # Account already exists. If the user came from the "Sign up"
            # tab, don't silently log them into an existing account -
            # point them at Sign In instead (matches the email/password
            # signup form's "email already registered" behavior).
            if intent == "signup":
                return jsonify({
                    "success": False,
                    "error": "An account with this email already exists. Please sign in instead.",
                    "reason": "account_exists"}), 400
            needs_password = not any(p.provider_id == "password" for p in user.provider_data)
            uid = user.uid
            # Backfill the name for accounts that existed before this field
            # was captured, or that were created via email/password without
            # one - never overwrites a name the user already has.
            if google_name and not user.display_name:
                fb_auth.update_user(uid, display_name=google_name)
        except fb_auth.UserNotFoundError:
            # No account yet. If the user came from the "Sign in" tab,
            # don't auto-create one behind their back - tell them to sign
            # up first (matches how email/password sign-in behaves when
            # the email isn't registered).
            if intent == "signin":
                return jsonify({
                    "success": False,
                    "error": "No CAPHY account is linked to this email. Please create an account first.",
                    "reason": "no_account"}), 404
            create_kwargs = {"email": email, "email_verified": email_verified}
            if google_name:
                create_kwargs["display_name"] = google_name
            user = fb_auth.create_user(**create_kwargs)
            uid = user.uid
            needs_password = True

    except Exception as e:
        return jsonify({"success": False, "error": f"Google sign-in failed: {e}"}), 500

    session["user_uid"] = uid
    session["email"] = email
    session["display_name"] = (user.display_name or google_name or "")
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

    # "signin" (default) or "signup" - which tab/button the user clicked.
    # Carried through state -> callback so the callback can enforce
    # "no account -> sign up instead" / "account exists -> sign in
    # instead" without the browser tab needing to remember anything.
    intent = request.args.get("intent", "signin")
    if intent not in ("signin", "signup"):
        intent = "signin"

    state = secrets.token_urlsafe(24)
    _PENDING_GOOGLE_LOGINS[state] = {"done": False, "uid": None, "email": None,
                                      "error": None, "intent": intent, "created": time.time()}

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
        google_name = payload.get("name", "")
        if not email:
            raise RuntimeError("Invalid Google token payload")

        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()

        intent = pending.get("intent", "signin")

        needs_password = False
        try:
            user = fb_auth.get_user_by_email(email)
            # Account already exists - if they clicked "Sign up with
            # Google", don't silently log them into it. Send them back
            # to Sign In instead.
            if intent == "signup":
                pending["done"] = True
                pending["error"] = "An account with this email already exists. Please sign in instead."
                return render_template("google_done.html", ok=False, message=pending["error"])
            uid = user.uid
            needs_password = not any(p.provider_id == "password" for p in user.provider_data)
            if google_name and not user.display_name:
                fb_auth.update_user(uid, display_name=google_name)
        except fb_auth.UserNotFoundError:
            # No account - if they clicked "Sign in with Google", don't
            # auto-create one. Send them to Sign up instead.
            if intent == "signin":
                pending["done"] = True
                pending["error"] = "No CAPHY account is linked to this email. Please create an account first."
                return render_template("google_done.html", ok=False, message=pending["error"])
            create_kwargs = {"email": email, "email_verified": email_verified}
            if google_name:
                create_kwargs["display_name"] = google_name
            user = fb_auth.create_user(**create_kwargs)
            uid = user.uid
            needs_password = True

        pending["done"] = True
        pending["uid"] = uid
        pending["email"] = email
        pending["display_name"] = (user.display_name or google_name or "")
        pending["needs_password"] = needs_password
        _mirror_local_user(uid, email)

        return render_template("google_done.html", ok=True,
                               message=f"Signed in as: {email}. You can now close this tab and "
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
    session["display_name"] = pending.get("display_name", "")
    session.permanent = True
    next_url = url_for("set_password") if pending.get("needs_password") else url_for("dashboard")
    return jsonify({"done": True, "next": next_url})


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

    # Detection heartbeat: an open camera whose _last_frame_ok hasn't updated
    # in 10s+ has a detection loop that's stuck repeatedly throwing (caught
    # now, so the thread survives, but it's still not actually detecting).
    # yolo_ok alone can't catch this - it's only set once at startup and
    # never re-checked, so it stayed "running" even while the old bug let
    # the whole loop die silently. This is the real "is it working RIGHT NOW"
    # signal.
    now_ts = time.time()
    open_workers = [w for w in workers if not w.paused]
    stalled = any((now_ts - getattr(w, "_last_frame_ok", 0)) > 10
                  for w in open_workers) if open_workers else False
    detect_ok = yolo_ok and not stalled

    # Check actual network connectivity (not just database status)
    is_online = internet_available()
    if is_online and pending > 0:
        sync_status = f"syncing · queue {pending}"
        sync_color = "#5b8dff"  # Blue for syncing
    elif is_online:
        sync_status = "synced"
        sync_color = "#3fb98a"  # Green for synced
    else:
        sync_status = f"offline · queue {pending}"
        sync_color = "#e5544e"  # Red for offline

    G, T, M, R = "#3fb98a", "#5b8dff", "#8d9bb5", "#e5544e"
    if not yolo_ok:
        yolo_status, yolo_color = "off", R
    elif stalled:
        yolo_status, yolo_color = "stalled", R
    else:
        yolo_status, yolo_color = "running", G
    modules = [
        {"name": "OpenCV capture", "status": "running", "color": G},
        {"name": "YOLOv8-nano", "status": yolo_status, "color": yolo_color},
        {"name": "Tier engine", "status": "stalled" if stalled else "running", "color": R if stalled else G},
        {"name": "Night vision (CLAHE)", "status": "engaged" if nv_on else "standby", "color": T if nv_on else M},
        {"name": "Flask API / MJPEG", "status": "running", "color": G},
        {"name": "SQLite", "status": "ok", "color": T},
        {"name": "Cloud sync", "status": sync_status, "color": sync_color},
        {"name": "Firebase FCM", "status": "connected" if fcm_on else "off", "color": T if fcm_on else M},
    ]
    return jsonify({"cpu": cpu, "mem": mem, "mem_txt": mem_txt, "disk": disk,
                    "fps": fps, "modules": modules, "detect_ok": detect_ok})


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
    threat_foot = ("person &middot; %s m away" % cur_dist) if cur else "No active threat"
    cam_foot = "all online" if online == ncam and ncam else ("%d online" % online)
    cards = f"""
    <div class="cards">
      <div class="card a1"><div class="k">Alerts Today</div><div class="v">{total}</div>
        <div class="foot"><span class="d"></span>+{today} today</div></div>
      <div class="card a4"><div class="k">Cameras Online</div><div class="v">{online} / {ncam}</div>
        <div class="foot"><span class="d"></span>{cam_foot}</div></div>
      <div class="card {'a2' if cur else 'a1'}"><div class="k">Current Status</div><div class="v">{threat_txt}</div>
        <div class="foot"><span class="d"></span>{threat_foot}</div></div>
      <div class="card a3"><div class="k">Pending Uploads</div><div class="v">{pending}</div>
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
            event = "Person detected" if a["tier"] else "Movement (no person)"
            alerts_html += (
                f'<div class="arow t{t}">'
                f'<div class="av"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/>'
                f'<path d="M4 21v-1a6 6 0 0 1 12 0v1"/></svg></div>'
                f'<div class="txt"><div class="tt">{tier_pill(t)}</div>'
                f'<div class="ss">{event} &middot; {a["distance_m"]} m away</div></div>'
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
      <div class="ph"><h2>Threat Activity</h2><span style="color:var(--muted);font-size:12px">Last 24 hours</span></div>
      <div class="chart">{bars}</div>
      <div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    </div>"""
    return page("Dashboard", "/", body,
                subtitle='Live System Overview &middot; Updated Just Now')


@app.route("/live")
def live():
    main_id = workers[0].cam_id if workers else 0
    main_name = workers[0].name if workers else "No camera"
    main_closed = workers[0].paused if workers else True
    # Icon-only Close/Open Camera control button, rendered up front with the
    # REAL starting state (e.g. closed by default on a fresh launch) so
    # there's no flash of the wrong icon before JS/poll() catches up.
    #
    # Deliberately NOT a camera glyph (open or crossed-out) - at 24px next to
    # the Snapshot button (also camera-shaped) the two were indistinguishable
    # at a glance. A power-toggle symbol (circle + vertical line) reads
    # instantly as "on/off" and doesn't compete visually with Snapshot.
    _cam_icon_power = '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>'
    cam_close_btn = (
        f'<button class="ctrlbtn icon camtoggle{" active" if main_closed else ""}" id="camCloseBtn" '
        f'onclick="toggleSelectedCam()" title="{"Open Camera" if main_closed else "Close Camera"}">'
        f'<svg id="camCloseIcon" viewBox="0 0 24 24">{_cam_icon_power}</svg>'
        f'</button>')
    cam_items = ""
    for w in workers:
        active = " active" if w.cam_id == main_id else ""
        closed = w.paused
        # Each thumbnail is already a live MJPEG stream (/video_feed/{id}),
        # not a static image - so this sidebar already shows every camera
        # live at once, it just used to read as a plain selector list.
        # camThumbFs() opens THIS camera fullscreen directly (via the same
        # fsMain() the main feed uses, after selecting it first) without
        # disturbing selectCam()'s normal click-to-control behavior on the
        # rest of the tile - a small fullscreen icon overlay makes that
        # "click to go fullscreen, like a real CCTV" affordance visible
        # instead of being a hidden gesture.
        cam_items += (
            f'<div class="camitem{active}{" camclosed" if closed else ""}" id="ci{w.cam_id}" onclick="selectCam({w.cam_id})">'
            f'<div class="camthumbwrap">'
            f'<img class="camthumb" src="/video_feed/{w.cam_id}">'
            f'<button class="camthumbfs" onclick="event.stopPropagation();camThumbFs({w.cam_id})" title="Fullscreen">'
            f'<svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>'
            f'</button></div>'
            f'<div class="cimeta"><div><div class="cn">{w.name}</div>'
            f'<div class="cs" id="cs{w.cam_id}">{"Closed" if closed else "Online"}</div></div>'
            f'<span class="dot" id="cd{w.cam_id}" style="background:{"var(--dim)" if closed else "var(--green)"}"></span></div></div>')

    # Close-all / open-all toggle button - reflects the REAL current state of
    # every camera, not a fixed "Close All" label. If every camera is closed,
    # this becomes "Open All Cameras"; if any camera is open, it's "Close All
    # Cameras" (closing whichever ones are still open).
    if cam_items and not cam_items.startswith('<div style='):
        all_closed = all(w.paused for w in workers) if workers else False
        if all_closed:
            cam_items += '<button class="closeallbtn openall" id="closeAllBtn" onclick="openAllCams()">Open All Cameras</button>'
        else:
            cam_items += '<button class="closeallbtn" id="closeAllBtn" onclick="closeAllCams()">Close All Cameras</button>'

    if not cam_items:
        cam_items = '<div style="color:var(--dim);font-size:13px">No cameras running</div>'

    body = """
    <div class="livewrap">
      <div class="panel maincam">
        <div class="ph"><h2 id="mainName">__MAIN_NAME__</h2><span class="pill p3 rec">&#9679; REC</span></div>
        <div class="feedwrap live" id="mainwrap">
          <img class="feed" id="mainFeed" src="/video_feed/__MAIN_ID__">
          <!-- Covers the <img> above until the selected camera has actually
               produced a real frame (per /api/stats' `online` flag - the
               same signal System Health already uses). Without this, a
               camera that's still opening the device serves a flat gray
               placeholder JPEG from the server, which looks exactly like a
               frozen/broken feed to anyone watching - especially on first
               page load or right after switching cameras. This is the fix:
               nothing that could be mistaken for a frozen frame is ever
               visible, only an explicit loading or unavailable state. -->
          <div class="camstate" id="camLoadingState">
            <div class="camstateicon loading">
              <svg viewBox="0 0 24 24"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
            </div>
            <div class="camspinner"></div>
            <div class="camstatetxt">Camera Loading&hellip;</div>
          </div>
          <div class="camstate" id="camUnavailableState" style="display:none">
            <div class="camstateicon error">
              <svg viewBox="0 0 24 24"><path d="M1 1l22 22"/><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
            </div>
            <div class="camstatetxt">Camera Unavailable</div>
            <div class="camstatesub">Could not connect to this camera.</div>
            <button class="camretrybtn" onclick="retryMainFeed()">
              <svg viewBox="0 0 24 24"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
              Retry</button>
          </div>
          <div class="detbox">
            <div class="dh">DETECTION</div>
            <div class="dr"><span>Factor 1 &middot; Motion</span><span class="dot" id="dm" style="background:var(--dim)"></span></div>
            <div class="dr"><span>Factor 2 &middot; Person</span><span class="dot" id="dp" style="background:var(--dim)"></span></div>
            <div class="dd">Est. distance <span id="ddist">-</span> m away</div>
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
          <button class="ctrlbtn icon" onclick="snap()" title="Snapshot">
            <svg viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
          </button>
          <button class="ctrlbtn icon" id="recBtn" onclick="toggleRec()" title="Record">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/></svg>
          </button>
          <button class="ctrlbtn icon" id="nvBtn" onclick="toggleNV()" title="Night vision">
            <svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
          </button>
          __CAM_CLOSE_BTN__
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
          <h2 style="margin-bottom:14px">Activity Log</h2>
          <div class="tflog" id="tflog"></div>
        </div>
      </div>
    </div>
    <script>
    let sel = __MAIN_ID__;
    // Icon-only Close/Open Camera button: a fixed power-toggle glyph (not a
    // camera shape, so it never gets confused with the Snapshot button) that
    // switches its ON/OFF appearance via the .active class, plus a tooltip
    // that always describes what clicking it will DO next.
    function syncCamCloseBtn(isClosed){
      const btn = document.getElementById('camCloseBtn');
      if(!btn) return;
      btn.title = isClosed ? 'Open Camera' : 'Close Camera';
      btn.classList.toggle('active', !!isClosed);
    }
    // ---- main feed loading/unavailable overlay ----
    // camReadySince: first poll() tick where the selected camera reported
    // online:true - null while we're still waiting for a real frame.
    // camFirstWaitAt: when we started waiting for THIS camera (reset every
    // time selectCam() switches feeds), used to decide when "still
    // loading" has gone on long enough to call it Unavailable instead -
    // same ~5s threshold the phone app's MjpegView uses, so both platforms
    // feel consistent.
    let camReadySince = false;
    let camFirstWaitAt = Date.now();
    const CAM_UNAVAILABLE_AFTER_MS = 7000;

    function showCamLoading(){
      document.getElementById('camLoadingState').style.display = 'flex';
      document.getElementById('camUnavailableState').style.display = 'none';
    }
    function showCamUnavailable(){
      document.getElementById('camLoadingState').style.display = 'none';
      document.getElementById('camUnavailableState').style.display = 'flex';
    }
    function showCamReady(){
      document.getElementById('camLoadingState').style.display = 'none';
      document.getElementById('camUnavailableState').style.display = 'none';
    }
    // Called from poll() below with this tick's online flag for whichever
    // camera is currently selected - drives which of the three states
    // (loading / unavailable / hidden-because-ready) is visible right now.
    function updateCamFeedState(online){
      if(online){
        camReadySince = true;
        showCamReady();
        return;
      }
      camReadySince = false;
      if(Date.now() - camFirstWaitAt > CAM_UNAVAILABLE_AFTER_MS){
        showCamUnavailable();
      }else{
        showCamLoading();
      }
    }
    // Retry: re-request the MJPEG stream (fresh <img> src, cache-busted so
    // the browser doesn't just replay a cached broken connection) and go
    // back to the loading state to give the new attempt its own full
    // timeout window, exactly like the phone app's Retry button does.
    function retryMainFeed(){
      camFirstWaitAt = Date.now();
      camReadySince = false;
      showCamLoading();
      document.getElementById('mainFeed').src = '/video_feed/'+sel+'?retry='+Date.now();
    }

    function selectCam(id){
      sel = id;
      camFirstWaitAt = Date.now();
      camReadySince = false;
      showCamLoading();
      document.getElementById('mainFeed').src = '/video_feed/'+id;
      document.getElementById('mainName').textContent = document.querySelector('#ci'+id+' .cn').textContent;
      document.querySelectorAll('.camitem').forEach(function(e){ e.classList.remove('active'); });
      const it = document.getElementById('ci'+id); if(it) it.classList.add('active');
      // reflect this camera's open/closed state on the Close/Open Camera
      // button immediately, without waiting for the next poll() tick
      syncCamCloseBtn(!!(it && it.classList.contains('camclosed')));
    }
    function fsMain(){
      const el = document.getElementById('mainwrap');
      if(document.fullscreenElement){ document.exitFullscreen(); }
      else if(el.requestFullscreen){ el.requestFullscreen(); }
      else if(el.webkitRequestFullscreen){ el.webkitRequestFullscreen(); }
    }
    // Fullscreen affordance on each camera thumbnail (see cam_items in
    // /live's HTML) - selects that camera as the "controlled" one (same
    // as clicking the tile normally would) and immediately takes it
    // fullscreen too, so a panelist can go straight from the grid to a
    // full CCTV-style view of whichever camera without two separate
    // clicks in two different places.
    function camThumbFs(id){
      selectCam(id);
      fsMain();
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

    // fetch() has no built-in timeout, and setInterval doesn't wait for the
    // previous call's promise before firing the next one. Combined, a slow
    // request (e.g. from SQLite lock contention under multi-tab polling)
    // could leave several overlapping fetches in flight at once, each
    // piling onto server load and making the page look like it's frozen
    // even though new poll cycles keep firing underneath. This wraps fetch
    // with an AbortController timeout so a stuck request gives up instead
    // of lingering forever.
    function fetchWithTimeout(url, opts, timeoutMs){
      const controller = new AbortController();
      const t = setTimeout(function(){ controller.abort(); }, timeoutMs || 6000);
      return fetch(url, Object.assign({}, opts, {signal: controller.signal}))
        .finally(function(){ clearTimeout(t); });
    }

    let _stateInFlight = false;
    let _pollInFlight = false;

    function setPill(el, label, on, onText, offText){
      el.textContent = label + ' [' + (on ? onText : offText) + ']';
      el.classList.toggle('on', !!on);
    }
    async function refreshState(){
      if (_stateInFlight) return;   // previous poll still running - skip this tick rather than stack
      _stateInFlight = true;
      try{
        const r = await fetchWithTimeout('/api/state', {}, 5000); ST = await r.json();
        setPill(document.getElementById('sbArm'),   'System',       ST.armed,        'Armed','Disarmed');
        setPill(document.getElementById('sbCam'),   'Camera',       ST.camera_on,    'On','Off');
        setPill(document.getElementById('sbNv'),    'Night Vision', ST.night_vision, 'On','Off');
        setPill(document.getElementById('sbSiren'), 'Siren',        ST.siren,        'On','Off');
        setPill(document.getElementById('sbEmg'),   'Emergency',    ST.emergency,    'Active','Off');

        const armBtn=document.getElementById('armBtn');
        document.getElementById('armLabel').textContent = ST.armed ? 'Disarm' : 'Arm';
        armBtn.classList.toggle('active', ST.armed);

        document.getElementById('nvBtn').classList.toggle('active', ST.night_vision);
        document.getElementById('sirenBtn').textContent = ST.siren ? 'Stop Siren' : 'Trigger Siren';
        document.getElementById('sirenBtn').classList.toggle('active', ST.siren);

        const emgBtn=document.getElementById('emgBtn');
        emgBtn.title = ST.emergency ? 'Cancel Emergency' : 'Emergency';
        emgBtn.classList.toggle('active', ST.emergency);
      }catch(e){}
      finally{ _stateInFlight = false; }
    }
    async function toggleArm(){
      try{ await fetch('/api/arm',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: !ST.armed})}); }catch(e){}
      refreshState();
    }
    async function toggleSelectedCam(){
      const closeBtn = document.getElementById('camCloseBtn');
      const isCurrentlyClosed = closeBtn && closeBtn.classList.contains('active');
      const action = isCurrentlyClosed ? 'resume' : 'pause';
      try{ await fetch('/api/camera/'+sel+'/'+action,{method:'POST'}); }catch(e){}
      toast('Camera ' + sel + (isCurrentlyClosed ? ' opened' : ' closed'));
      setTimeout(function(){ location.reload(); }, 300);
    }
    async function closeAllCams(){
      try{ await fetch('/api/camera/power',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: false})}); }catch(e){}
      toast('All cameras closed');
      setTimeout(function(){ location.reload(); }, 300);
    }
    async function openAllCams(){
      try{ await fetch('/api/camera/power',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on: true})}); }catch(e){}
      toast('All cameras opened');
      setTimeout(function(){ location.reload(); }, 300);
    }
    async function toggleEmg(){
      if(!ST.emergency){
        const ok = await openModal({
          title: 'Activate Emergency Mode?',
          message: 'This will:<br>&bull; Turn on the camera<br>&bull; Arm the system<br>&bull; Sound the siren<br>&bull; Send an alert to everyone on your account.',
          confirmText: 'Proceed?',
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
      if (_pollInFlight) return;   // previous poll still running - skip this tick rather than stack
      _pollInFlight = true;
      try{
        const r=await fetchWithTimeout('/api/stats', {}, 5000); const data=await r.json();
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
            syncCamCloseBtn(!camOn);
            // Camera deliberately closed (paused) is a distinct, expected
            // state - not a failure - so it should show neither the
            // loading spinner nor "Camera Unavailable"; the existing
            // "Camera Off" placeholder frame from the server is correct
            // and intentional in that case, nothing to cover it with.
            if(!camOn){ showCamReady(); }
            else{ updateCamFeedState(!!s.online); }
          }
        });
        const lr=await fetchWithTimeout('/api/logs', {}, 5000); const logs=await lr.json();
        const box=document.getElementById('tflog');
        box.innerHTML = logs.map(function(l){
          const col = l.level==='ALERT' ? 'var(--orange)' : (l.level==='DETECT' ? 'var(--teal2)' : 'var(--muted)');
          return '<div class="lg"><span class="tt">'+l.t+'</span><span style="color:'+col+'">'+l.msg+'</span></div>';
        }).join('');
      }catch(e){}
      finally{ _pollInFlight = false; }
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

      /* Camera list item - dims + grayscales when that camera is closed.
         Opening/closing itself now lives in the main Controls row (Close
         Camera / Open Camera button), not on the list item. */
      .camitem{position:relative}
      .camitem.camclosed{opacity:.55}
      .camitem.camclosed .camthumb{filter:grayscale(1)}

      /* Close/Open Camera control button - a clear ON/OFF toggle, not just
         another icon button. Resting (camera OPEN) state gets a soft green
         tint so it visibly reads as "on"; active (camera CLOSED) state is a
         solid orange fill, unmistakably different from every other button
         in the row - this is the switch that turns the whole feed off, so
         it needs to stand out at a glance, not blend in. */
      .camtoggle{background:rgba(63,185,80,.12) !important;border-color:rgba(63,185,80,.5) !important;
        color:#3fb950 !important}
      .camtoggle:hover{background:rgba(63,185,80,.2) !important;border-color:#3fb950 !important}
      .camtoggle.active{background:var(--orange, #e5a13f) !important;color:#04110e !important;
        border-color:var(--orange, #e5a13f) !important;box-shadow:0 0 0 3px rgba(229,161,63,.18)}
      .camtoggle.active:hover{filter:brightness(1.08)}

      /* Close all cameras button */
      .closeallbtn{display:block;width:100%;margin-top:8px;padding:8px 12px;
        background:var(--panel);border:1px solid var(--line);color:var(--text);
        border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:.15s}
      .closeallbtn:hover{border-color:var(--red);color:var(--red)}
      .closeallbtn.openall{border-color:var(--teal2);color:var(--teal2)}
      .closeallbtn.openall:hover{background:rgba(63,215,196,.1)}
    </style>
    """.replace("__CAM_ITEMS__", cam_items).replace("__MAIN_NAME__", main_name).replace("__MAIN_ID__", str(main_id)).replace("__CAM_CLOSE_BTN__", cam_close_btn)
    return page("Live Camera", "/live", body)


@app.route("/api/stats")
def api_stats():
    out = []
    for w in workers:
        st = w.get_stats()
        out.append({"cam": w.cam_id, "name": w.name, "online": st.get("online", False),
                    "motion": st.get("motion", False), "person": st.get("person", False),
                    "tier": st.get("tier", 0), "distance": st.get("distance", "-"),
                    "fps": st.get("fps", "-"),
                    # w.paused is the source of truth for open/closed - NOT
                    # st["camera_on"], which is only refreshed by
                    # _store_placeholder() and can lag or never fire, leaving
                    # the client to wrongly default camera_on to True.
                    "camera_on": not w.paused})
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
    """Toggles night vision for ALL cameras together, not just the one the
    caller happened to have selected.

    Night vision was previously per-camera (w.nv.enabled on just the
    :cam worker), but everything else about it - the single 'night_vision'
    column in the settings table, the voice command ("night vision
    on"/"off", no camera specified), and the web/phone status indicators
    (sbNv pill, phone StateChip) - all treat it as ONE global on/off. That
    mismatch was exactly what made the button "not sync": toggling camera
    A's night vision left camera B's flag untouched, so a global status
    indicator built from `any(w.nv.enabled for w in workers)` could show
    ON/OFF in ways that didn't match what the button you just pressed
    seemed to say, on both platforms, depending on which camera was
    selected when you looked. Applying the same value to every worker
    means there is only one true state, and every UI that reads it -
    web pill, web button, phone StateChip, phone button, /api/state -
    is now reading and driving that same single value.
    """
    if not (0 <= cam < len(workers)):
        return jsonify({"on": False})

    new_state = not workers[cam].nv.enabled
    for w in workers:
        w.nv.enabled = new_state
        w._log("INFO", "night", "CLAHE " + ("ON" if new_state else "OFF") + " (manual)")
    return jsonify({"on": new_state})


@app.route("/api/siren", methods=["POST"])
def api_siren():
    global _siren_manual
    _siren_manual = not _siren_manual
    with _siren_lock:
        _recompute_siren()
    _push_state_async()
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
    over WiFi/hotspot to talk to the local Flask server for camera/live/
    arm-disarm) - must work with ZERO internet, since Local Offline Mode is
    explicitly a same-LAN-no-internet scenario (e.g. laptop connected to
    the PHONE'S OWN hotspot with mobile data off).

    Primary approach: connect() a UDP socket toward a public IP. This does
    NOT require actual internet reachability - UDP connect() never sends a
    packet, it only asks the OS's routing table which local interface WOULD
    be used for that destination, which resolves from routing rules, not
    live connectivity. So this keeps working even with the destination
    totally unreachable. It can still occasionally pick the wrong interface
    on a multi-adapter laptop (e.g. an idle Ethernet port), or throw outright
    on an unusual network stack - if either happens, fall back to directly
    enumerating the machine's own network interfaces and picking the first
    private (RFC1918) address, which needs no destination/routing decision
    at all. Only return 127.0.0.1 (a loopback address the phone can NEVER
    reach) as an absolute last resort, and log loudly when that happens so
    it's obvious in the logs rather than silently producing a dead QR code.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Fallback: enumerate actual local interfaces directly, no routing
    # decision involved - works even if the UDP-connect trick above picked
    # a bad interface or failed outright.
    #
    # getaddrinfo() has NO built-in timeout parameter, unlike every other
    # network call in this file. On Windows with no reachable DNS server
    # (exactly the no-internet scenario this function exists to handle)
    # and multiple network adapters (VPN clients, WSL/Hyper-V virtual
    # adapters, etc. are all common), resolving the local hostname can fall
    # through slow NetBIOS/LLMNR paths with no application-level bound -
    # which would block whatever Flask request thread called this,
    # reintroducing exactly the kind of stall the sync.py timeout fix was
    # meant to eliminate elsewhere. Running it in a daemon thread with a
    # hard join() timeout caps the worst case instead of trusting the OS.
    try:
        result = {}

        def _resolve():
            try:
                result["addrs"] = socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET)
            except Exception:
                result["addrs"] = []

        t = threading.Thread(target=_resolve, daemon=True)
        t.start()
        t.join(timeout=1.5)
        for info in result.get("addrs", []):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and (
                ip.startswith("10.") or ip.startswith("192.168.")
                or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)
            ):
                return ip
    except Exception:
        pass

    print("[CAPHY] WARNING: could not determine a real LAN IP - "
          "falling back to 127.0.0.1, which the phone cannot reach. "
          "Check the laptop's network adapters.")
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
    device the system can detect right now (see available_cameras()).

    Called automatically the moment Settings -> Cameras opens (no button
    press needed - see the auto-load at the bottom of _CAMERA_PICKER_JS),
    and again if the user presses Rescan after plugging something in. The
    ?rescan=1 flag on that second case forces the real device-name cache
    (_device_name_cache) to refresh too, so a camera plugged in after the
    page first loaded shows its real name (e.g. "DroidCam Video") instead
    of a stale/missing one.
    """
    if request.args.get("rescan"):
        global _device_name_cache
        _device_name_cache = None
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


# ==================== Camera Auto-Detection & Selection (V380, USB, etc) ====================
@app.route("/camera-setup")
def camera_setup_page():
    """Serve camera selector UI for plug-and-play setup"""
    return send_file("web/templates/camera_selector.html", mimetype="text/html")


@app.route("/api/detector/cameras", methods=["GET"])
def api_detector_cameras():
    """Scan and return all available cameras (local + network)"""
    try:
        from camera_detector import get_all_cameras
        return jsonify(get_all_cameras())
    except Exception as e:
        return jsonify({"error": str(e), "local": [], "network": []}), 500


@app.route("/api/detector/cameras/saved", methods=["GET"])
def api_detector_cameras_saved():
    """Get currently saved single-camera selection"""
    try:
        from camera_detector import load_camera_config
        saved = load_camera_config()
        return jsonify({"saved_camera": saved})
    except Exception as e:
        return jsonify({"saved_camera": None, "error": str(e)})


@app.route("/api/detector/cameras/test", methods=["POST"])
def api_detector_cameras_test():
    """Test if camera connection works"""
    try:
        data = request.get_json(silent=True) or {}
        camera_source = data.get("camera")
        if not camera_source:
            return jsonify({"connected": False, "error": "No camera specified"}), 400

        from camera_detector import test_camera_connection
        result = test_camera_connection(camera_source)
        return jsonify({
            "connected": result['connected'],
            "camera": str(camera_source),
            "error": result.get('error')
        })
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500


@app.route("/api/detector/cameras/select", methods=["POST"])
def api_detector_cameras_select():
    """Select and save a single camera for single-camera mode"""
    try:
        data = request.get_json(silent=True) or {}
        camera_source = data.get("camera")
        if not camera_source:
            return jsonify({"ok": False, "error": "No camera specified"}), 400

        from camera_detector import save_camera_config
        config = save_camera_config(camera_source)

        if "error" in config:
            return jsonify({"ok": False, "error": config["error"]}), 500

        return jsonify({
            "ok": True,
            "message": "Camera saved for offline use",
            "camera": str(camera_source)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_emergency_on = False        # panic override; remembers the state it interrupted
_emergency_prev = None


_arm_grace_until = 0.0     # no alerts until this time (set when arming)
_disarm_grace_until = 0.0  # Tier 3 does not escalate (siren/urgent push) until this time (set when disarming)


def _set_armed(value):
    global _arm_grace_until, _disarm_grace_until
    if value:
        # Arming grace: give the user a few seconds to leave the frame, so
        # arming while standing in view does not instantly fire an alert.
        _arm_grace_until = time.time() + getattr(config, "ARM_GRACE_SEC", 8)
    else:
        # Disarming grace: the moment you turn the system off you very
        # likely walk right past (or in front of) the camera to get on
        # with your day - without this, that walk-by would still hit
        # Tier 3 (close range) and sound the siren/send an urgent push for
        # what is obviously just the owner. A Tier 3 snapshot still gets
        # saved either way (see the siren_on gate below) - only the alarm
        # response is suppressed, and only briefly.
        _disarm_grace_until = time.time() + getattr(config, "DISARM_GRACE_SEC", 45)
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


LAN_DISCOVERY_PORT = 58471          # arbitrary fixed port, phone and laptop must agree on it
LAN_DISCOVERY_MAGIC = "CAPHY_DISCOVER_V1"   # simple request marker, not a security boundary


def start_lan_discovery_beacon():
    """Answers local-network 'where are you?' broadcasts with this laptop's
    current IP and Flask port - works with ZERO internet on either side.

    This exists specifically for the case Firestore-based discovery cannot
    solve: phone creates a hotspot, laptop joins it, phone's mobile data is
    off. In that setup the LAPTOP also has no internet, so its heartbeat
    loop can't reach Firestore to publish a fresh last_lan_ip either - the
    phone's isLanReachable() self-heal (which reads that Firestore field)
    has nothing current to read, and would keep failing even though the
    two devices are sitting on the same network right now. A plain UDP
    broadcast never touches the internet at all, so it works in exactly
    this case.

    Protocol: phone broadcasts the literal string LAN_DISCOVERY_MAGIC as a
    UDP packet to the local broadcast address on LAN_DISCOVERY_PORT. Any
    laptop running this listener replies directly to the sender with JSON
    {"ip": ..., "port": ...}. Not a security boundary (anyone on the LAN
    could probe it) - identical trust model to the existing QR-code pairing
    flow, which also hands out ip:port to anyone who can see the code.
    """
    def loop():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", LAN_DISCOVERY_PORT))
        except Exception as e:
            print(f"[CAPHY] LAN discovery beacon disabled (couldn't bind: {e})")
            return

        print(f"[CAPHY] LAN discovery beacon listening on UDP {LAN_DISCOVERY_PORT}")
        while True:
            try:
                s.settimeout(5.0)
                try:
                    data, addr = s.recvfrom(256)
                except socket.timeout:
                    continue
                if data.decode("utf-8", errors="ignore").strip() != LAN_DISCOVERY_MAGIC:
                    continue
                reply = json.dumps({"ip": _local_ip(), "port": config.WEB_PORT
                                     if hasattr(config, "WEB_PORT") else 5000}).encode("utf-8")
                s.sendto(reply, addr)
            except Exception as e:
                print(f"[CAPHY] LAN discovery beacon error: {e}")
                time.sleep(1)

    t = threading.Thread(target=loop, name="caphy-lan-discovery", daemon=True)
    t.start()
    return t


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
                prefs = load_prefs()
                if prefs.get("autoarm"):
                    h = datetime.now().hour
                    start = prefs.get("autoarm_start_hour", getattr(config, "AUTO_ARM_START_HOUR", 22))
                    end = prefs.get("autoarm_end_hour", getattr(config, "AUTO_ARM_END_HOUR", 6))
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


def start_cloud_sync_retry():
    """Background sweep for two kinds of "saved locally, not yet delivered
    to the cloud" backlog:
      1. Alerts stuck with synced=0 - either saved while genuinely offline,
         or whose live publish_alert() call in the Worker loop failed/raised
         (or is deliberately still waiting on a video that hasn't finished
         recording yet - see the has_video check below).
      2. Push notifications that failed to send (queued into pending_pushes
         by the Worker loop when send_alert() returns False) - previously
         these were just lost with no retry at all.
    Runs every 30s, only does anything when internet_available() is true, and
    is a no-op the rest of the time. This is what makes the "pending upload"
    counter in Settings > Storage & Sync (and the Dashboard health panel)
    actually count down instead of growing forever, and now also what makes
    a missed push notification eventually arrive instead of vanishing.
    """
    def loop():
        while True:
            try:
                if internet_available():
                    db = Database(config.DB_PATH)
                    try:
                        rows = db.conn.execute(
                            "SELECT * FROM alerts WHERE synced=0 ORDER BY alert_id LIMIT 25").fetchall()
                        if rows:
                            from identity import get_device_identity
                            from storage import cloud_alerts
                            device_id = get_device_identity()["device_id"]
                            for a in rows:
                                owner = a["user_uid"] if "user_uid" in a.keys() else None
                                owner = owner or _current_device_owner_uid()
                                if not owner:
                                    continue   # no account to publish to yet
                                ajson = _alert_json(a)
                                snap_path = a["snapshot_path"] if "snapshot_path" in a.keys() else None
                                vid_path = a["video_path"] if "video_path" in a.keys() else None
                                # Only treat this as fully synced if a video isn't
                                # expected, or is expected AND actually present now -
                                # otherwise leave synced=0 so we try again next sweep
                                # once the recording (still in progress elsewhere)
                                # finishes and video_path gets populated.
                                if ajson.get("has_video") and not vid_path:
                                    continue
                                try:
                                    ok = cloud_alerts.publish_alert(
                                        owner, device_id, ajson, snap_path, vid_path)
                                except Exception:
                                    ok = False
                                if ok:
                                    db.conn.execute(
                                        "UPDATE alerts SET synced=1 WHERE alert_id=?", (a["alert_id"],))
                                    db.conn.commit()

                        pushes = db.pending_pushes(limit=25)
                        if pushes:
                            import json as _json
                            from storage.firebase_push import FirebasePushSender
                            for p in pushes:
                                try:
                                    data = _json.loads(p["data_json"]) if p["data_json"] else {}
                                    sender = FirebasePushSender(p["user_uid"], p["device_id"])
                                    sent = sender.send_alert(
                                        title=p["title"], body=p["body"],
                                        data=data, tier=p["tier"])
                                except Exception:
                                    sent = False
                                if sent:
                                    db.delete_pending_push(p["push_id"])
                                else:
                                    db.bump_pending_push_attempts(p["push_id"])
                    finally:
                        db.close()
            except Exception as e:
                print(f"[CAPHY] Cloud sync retry error: {e}")
            time.sleep(30)

    t = threading.Thread(target=loop, name="caphy-cloudsync", daemon=True)
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
    # Push the new state to Firestore right away (fire-and-forget, doesn't
    # delay this response) so a phone that's off the LAN sees the correct
    # armed/disarmed status within ~1s instead of waiting for the next
    # routine heartbeat tick - this is what fixed "status wrong after
    # toggling" cross-network, since the phone's periodic status poll was
    # previously reading a heartbeat snapshot up to 20s stale.
    _push_state_async()
    return jsonify({"ok": True, "armed": on})


@app.route("/api/camera/power", methods=["POST"])
def api_camera_power():
    """Turn the camera device on or off. Off releases it and stops detection."""
    data = request.get_json(silent=True) or request.form
    on = data.get("on")
    current = any(not w.paused for w in workers) if workers else False
    on = (not current) if on is None else (str(on).lower() in ("1", "true", "yes"))
    _set_camera(on)
    _push_state_async()
    return jsonify({"ok": True, "camera_on": on})


@app.route("/api/camera/<int:cam>/pause", methods=["POST"])
def api_camera_pause(cam):
    """Pause/close a single camera without affecting others."""
    if not (0 <= cam < len(workers)):
        return jsonify({"error": "Camera not found"}), 404
    workers[cam].paused = True
    return jsonify({"ok": True, "paused": True})


@app.route("/api/camera/<int:cam>/resume", methods=["POST"])
def api_camera_resume(cam):
    """Resume/reopen a single camera that was individually closed, without
    touching any other camera's state."""
    if not (0 <= cam < len(workers)):
        return jsonify({"error": "Camera not found"}), 404
    workers[cam].paused = False
    return jsonify({"ok": True, "paused": False})


@app.route("/api/emergency", methods=["POST"])
def api_emergency():
    """Panic override on/off."""
    data = request.get_json(silent=True) or request.form
    on = data.get("on")
    on = (not _emergency_on) if on is None else (str(on).lower() in ("1", "true", "yes"))
    _do_action("emergency_on" if on else "emergency_off")
    _push_state_async()
    return jsonify({"ok": True, "emergency": on})


@app.route("/api/alert/<int:aid>/dismiss", methods=["POST"])
def api_dismiss_alert(aid):
    """Acknowledge one alert - hides it, keeps the row in the database."""
    db = Database(config.DB_PATH)
    try:
        db.dismiss_alert(aid)
        # Same SSE channel new alerts use (_broadcast_alert -> /api/alerts/
        # stream) - every connected phone/dashboard already listens on this
        # and re-fetches its list the instant anything arrives (see
        # alerts_tab.dart's watchAlerts().listen(...)). Previously this
        # channel only ever fired on a NEW alert, so dismissing/deleting on
        # one device never told any other connected device anything changed -
        # they'd only find out on their next manual refresh or the 15s
        # fallback poll. A lightweight {"event": "changed"} marker (no need
        # to resend the full alert payload) makes every screen stay in sync
        # immediately, regardless of which device (or the system itself,
        # e.g. auto-dismiss) made the change.
        _broadcast_alert({"event": "changed", "reason": "dismissed", "alert_id": aid})
        return jsonify({"ok": True, "alert_id": aid, "dismissed": True})
    finally:
        db.close()


@app.route("/api/alert/<int:aid>/restore", methods=["POST"])
def api_restore_alert(aid):
    db = Database(config.DB_PATH)
    try:
        db.restore_alert(aid)
        _broadcast_alert({"event": "changed", "reason": "restored", "alert_id": aid})
        return jsonify({"ok": True, "alert_id": aid, "dismissed": False})
    finally:
        db.close()


@app.route("/api/alert/<int:aid>/delete", methods=["POST"])
def api_delete_alert(aid):
    """Permanently delete an alert and its associated files (snapshot/video)."""
    db = Database(config.DB_PATH)
    try:
        row = db.conn.execute("SELECT snapshot_path, video_path FROM alerts WHERE alert_id=?", (aid,)).fetchone()
        if row:
            # Delete associated files
            for path_col in ("snapshot_path", "video_path"):
                path = row[path_col]
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass  # Log but don't fail if file deletion fails
            # Delete from database
            db.conn.execute("DELETE FROM alerts WHERE alert_id=?", (aid,))
            db.conn.execute("DELETE FROM threat_logs WHERE alert_id=?", (aid,))
            db.conn.commit()
        # Also remove the Firestore copy - this was missing entirely, which
        # is why a "deleted" alert kept reappearing for anyone reading it
        # off-LAN (see cloud_alerts.delete_alert()'s docstring for the full
        # explanation). Best-effort: a cloud failure here must not turn a
        # successful local delete into a reported failure.
        try:
            from identity import get_device_identity
            from storage.cloud_alerts import delete_alert as _cloud_delete_alert
            _cloud_delete_alert(get_device_identity()["device_id"], aid)
        except Exception as e:
            print(f"[CAPHY] cloud alert delete failed (local delete still succeeded): {e}")
        _broadcast_alert({"event": "changed", "reason": "deleted", "alert_id": aid})
        return jsonify({"ok": True, "alert_id": aid, "deleted": True})
    finally:
        db.close()


@app.route("/api/alerts/delete_many", methods=["POST"])
def api_delete_many():
    """Permanently delete several alerts (and their snapshot/video files) in
    one call - used by the Alert History page's 'Delete Selected' bulk
    action, instead of firing api_delete_alert once per row (which would
    still work, but this keeps it one request/one DB transaction instead
    of N round-trips for a multi-select delete)."""
    data = request.get_json() or {}
    ids = data.get("ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid ids"}), 400
    if not ids:
        return jsonify({"ok": False, "error": "no ids given"}), 400

    db = Database(config.DB_PATH)
    deleted = []
    try:
        for aid in ids:
            row = db.conn.execute(
                "SELECT snapshot_path, video_path FROM alerts WHERE alert_id=?", (aid,)).fetchone()
            if not row:
                continue
            for path_col in ("snapshot_path", "video_path"):
                path = row[path_col]
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            db.conn.execute("DELETE FROM alerts WHERE alert_id=?", (aid,))
            db.conn.execute("DELETE FROM threat_logs WHERE alert_id=?", (aid,))
            deleted.append(aid)
        db.conn.commit()
        if deleted:
            # Same Firestore cleanup as the single-delete route above -
            # without this, bulk-deleted alerts also kept reappearing for
            # anyone reading them off-LAN.
            try:
                from identity import get_device_identity
                from storage.cloud_alerts import delete_alerts as _cloud_delete_alerts
                _cloud_delete_alerts(get_device_identity()["device_id"], deleted)
            except Exception as e:
                print(f"[CAPHY] cloud alerts batch delete failed (local delete still succeeded): {e}")
            _broadcast_alert({"event": "changed", "reason": "deleted_many", "alert_ids": deleted})
        return jsonify({"ok": True, "deleted": deleted})
    finally:
        db.close()


@app.route("/api/alerts/dismiss_all", methods=["POST"])
def api_dismiss_all():
    """'Clear alerts' - dismisses every visible alert. Deletes nothing."""
    db = Database(config.DB_PATH)
    try:
        n = db.dismiss_all_alerts()
        if n:
            _broadcast_alert({"event": "changed", "reason": "dismissed_all"})
        return jsonify({"ok": True, "dismissed": n})
    finally:
        db.close()


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
        out = []
        for a in rows:
            snap = a["snapshot_path"] if "snapshot_path" in a.keys() else None
            out.append({
                "id": a["alert_id"], "tier": a["tier"] or 0,
                "distance_m": a["distance_m"], "camera": a.get("camera"),
                "timestamp": a["timestamp"],
                "snapshot_url": ("/snapshot/" + os.path.basename(snap)) if snap else None,
            })
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


# ==================== voice assistant v2 ====================
def _assistant_action_handlers():
    """Builds the action-name -> real-function mapping for the voice
    assistant router (assistant_ai/router.py + assistant_ai/actions.py).

    Built fresh per-request (cheap - just closures) rather than once at
    import time, so it always reads the current `workers` list rather than
    whatever existed when the module first loaded.
    """
    def _cam0():
        return 0 if workers else None

    def _handle_snapshot(ctx):
        cam = _cam0()
        if cam is None:
            return {"ok": False, "error": "No camera available."}
        name = _save_snapshot(cam)
        return {"ok": bool(name), "name": name}

    def _handle_record(ctx, start):
        cam = _cam0()
        if cam is None:
            return {"ok": False, "error": "No camera available."}
        w = workers[cam]
        if w.manual_record == start:
            return {"ok": True, "recording": w.manual_record}  # already in the requested state
        w.manual_record = start
        w._log("INFO", "record", "manual recording " + ("started" if start else "stopped"))
        return {"ok": True, "recording": w.manual_record}

    def _handle_camera_pause_resume(ctx, pause):
        cam = _cam0()
        if cam is None:
            return {"ok": False, "error": "No camera available."}
        workers[cam].paused = pause
        return {"ok": True, "paused": pause}

    return {
        "arm":               lambda ctx: (_set_armed(True),  {"ok": True, "armed": True})[1],
        "disarm":            lambda ctx: (_set_armed(False), {"ok": True, "armed": False})[1],
        "siren_on":          lambda ctx: (_do_action("siren_on"),  {"ok": True, "siren": True})[1],
        "siren_off":         lambda ctx: (_do_action("siren_off"), {"ok": True, "siren": False})[1],
        "emergency_on":      lambda ctx: (_do_action("emergency_on"),  {"ok": True, "emergency": True})[1],
        "emergency_off":     lambda ctx: (_do_action("emergency_off"), {"ok": True, "emergency": False})[1],
        "camera_on":         lambda ctx: (_set_camera(True),  {"ok": True, "camera_on": True})[1],
        "camera_off":        lambda ctx: (_set_camera(False), {"ok": True, "camera_on": False})[1],
        "camera_pause":      lambda ctx: _handle_camera_pause_resume(ctx, True),
        "camera_resume":     lambda ctx: _handle_camera_pause_resume(ctx, False),
        "night_vision_on":   lambda ctx: (_set_night_vision(True),  {"ok": True, "night_vision": True})[1],
        "night_vision_off":  lambda ctx: (_set_night_vision(False), {"ok": True, "night_vision": False})[1],
        "snapshot":          _handle_snapshot,
        "record_start":      lambda ctx: _handle_record(ctx, True),
        "record_stop":       lambda ctx: _handle_record(ctx, False),
        "check_status":      lambda ctx: {"ok": True, **_system_snapshot()},
        "check_health":      lambda ctx: {"ok": True, "fps": (workers[0].get_stats().get("fps", 0) if workers else 0),
                                           "cameras_online": sum(1 for w in workers if w.get_stats().get("online"))},
        "check_alerts":      lambda ctx: (_do_action("alert_status")),
        "dismiss_all_alerts": lambda ctx: (_do_action("clear_alerts"), {"ok": True})[1],
    }


def _assistant_live_state():
    """Live facts handed to the AI so status/health questions are answered
    from what's actually true right now, not guessed."""
    state = _system_snapshot()
    state["cameras_online"] = sum(1 for w in workers if w.get_stats().get("online")) if workers else 0
    state["auto_arm"] = bool(load_prefs().get("autoarm", False))
    return state


@app.route("/api/assistant", methods=["POST"])
def api_assistant():
    """Single entry point for the voice assistant (v2). Takes transcribed
    text from the phone app, classifies it as talk/know/do/clarify via
    assistant_ai.router, and - for 'do' - executes the action through the
    closed whitelist in assistant_ai.actions.

    Request JSON: {"text": "turn on the siren"}
    Response JSON: {"type": "do", "reply": "...", "action": "siren_on",
                    "action_result": {"ok": true, "siren": true}}
    """
    data = request.get_json(silent=True) or {}
    user_text = (data.get("text") or "").strip()
    if not user_text:
        return jsonify({"type": "error", "reply": "I didn't catch anything - could you say that again?"}), 400

    from assistant_ai.router import handle_request

    # session.get("display_name") only exists for browser dashboard
    # requests (a Flask session cookie) - the phone app authenticates with
    # a Firebase bearer token instead and never has a Flask session at all,
    # so this was ALWAYS empty for every voice/chat request from the app.
    # That's why CAPHY never knew who it was talking to on mobile: the name
    # lookup silently no-opped every single time instead of just being
    # wrong sometimes. _current_uid() resolves from EITHER auth method, the
    # same way every other dual-purpose route here already does (see
    # api_fcm_register above) - then _get_display_name() does the same
    # Firebase Admin SDK lookup the web dashboard's own header already uses.
    user_uid = _current_uid()
    user_name = _get_display_name(user_uid) if user_uid else ""
    result = handle_request(
        user_text,
        user_name=user_name,
        live_state=_assistant_live_state(),
        action_handlers=_assistant_action_handlers(),
    )
    return jsonify(result)


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
        return (f'<a class="fpill{" active" if active else ""}" href="{href}" '
                f'onclick="return loadHistory(this.href, event)">{label}</a>')

    # ---- tier pills ----
    # IMPORTANT CORRECTNESS FIX: tier filtering used to be pure client-side
    # show/hide of whatever rows happened to already be in the DOM. That
    # looked right in a quick test (few alerts, one page) but was actually
    # WRONG in general: the table only ever holds the CURRENT PAGE's rows
    # (PAGE_SIZE=12, see below) - the SQL LIMIT/OFFSET already discarded
    # every alert outside this page before the browser ever saw them.
    # Clicking "Tier 1" would only show whichever of the 12 on-screen rows
    # happened to be Tier 1 - it would silently miss every Tier 1 alert
    # sitting on page 2, 3, etc, while the pager/count still said "Showing
    # 1-12 of N" for the wrong (unfiltered) N. So tier pills now go through
    # the SAME loadHistory() AJAX path as Last 24h/Include acknowledged/
    # pagination (see the page's <script>) - a real, correctly-paginated
    # server query for that tier, fetched in the background and swapped in
    # without a full page reload. This keeps it exactly as fast/lag-free as
    # before while making "Tier 1/2/3" actually mean "every Tier 1/2/3
    # alert", not just "whichever happen to be on this page".
    def tier_btn(label, tier_value):
        active = tier == tier_value
        return (f'<button type="button" class="fpill{" active" if active else ""}" '
                f'data-tierval="{tier_value}" '
                f'onclick="loadHistory(\'{qs(tier=tier_value, page=1)}\')">{label}</button>')

    pills = (
        tier_btn("All Tiers", "all")
        + tier_btn("Tier 3", "3")
        + tier_btn("Tier 2", "2")
        + tier_btn("Tier 1", "1")
        # Last 24h / Include acknowledged also change which rows exist at
        # all (a real date-range/visibility filter on the underlying
        # query) - same loadHistory() path, no full navigation.
        + pill("Last 24h", rng == "24h", qs(range="" if rng == "24h" else "24h", page=1))
        + pill("Include acknowledged", show == "all",
               qs(show="" if show == "all" else "all", page=1)))

    # ---- table rows ----
    import html as _html
    trows = ""
    for a in rows:
        snap = a["snapshot_path"]
        has_person = a["confidence"] and a["confidence"] > 0
        event = "Person Detected" if has_person else "Movement (no person)"
        sub = "Motion + Person Verified" if has_person else "motion only &middot; no person confirmed"
        cam = (a["camera"] if "camera" in a.keys() and a["camera"] else "&mdash;")
        ts = a["timestamp"][:19].replace("T", " ")
        if snap:
            view_url = "/snapshot/%s" % os.path.basename(snap)
            title = _html.escape(f"{event} · {cam} · {ts}", quote=True)
            thumb = "<img class='av thumb' src='%s'>" % view_url
            view_btn = (f"<button class='viewbtn' onclick=\"event.stopPropagation(); openSnap('{view_url}','{title}')\">View</button>")
        else:
            thumb = ("<div class='av'><svg viewBox='0 0 24 24'><circle cx='12' cy='8' r='4'/>"
                     "<path d='M4 21v-1a6 6 0 0 1 12 0v1'/></svg></div>")
            view_btn = "<span class='viewbtn disabled'>View</span>"
        del_btn = f"<button class='delbtn' onclick=\"delHistAlert({a['alert_id']}, event)\" title='Delete'>×</button>"
        trows += (
            "<tr data-id='%s' data-tier='%s' onclick=\"rowClicked(event, %s)\">"
            "<td class='selcell' style='display:none'><input type='checkbox' class='rowchk' onclick=\"event.stopPropagation(); toggleRowSelect(%s, this)\"></td>"
            "<td><div class='evrow'>%s"
            "<div class='evtxt'><div class='evname'>%s</div><div class='evsub'>%s</div></div></div></td>"
            "<td>%s</td><td>%s m away</td><td style='color:var(--muted)'>%s</td>"
            "<td>%s</td><td>%s</td></tr>"
            % (a["alert_id"], a["tier"], a["alert_id"], a["alert_id"], thumb, event, sub, cam, a["distance_m"], ts, view_btn, del_btn))
    if not trows:
        trows = "<tr><td colspan='6' style='color:var(--dim);padding:22px'>No alerts match this filter.</td></tr>"

    # ---- pagination ----
    lo = offset + 1 if total else 0
    hi = offset + len(rows)

    def pgbtn(n, active=False, label=None, href=None):
        cls = "pg-btn active" if active else "pg-btn"
        href = href if href is not None else qs(page=n)
        return (f'<a class="{cls}" href="{href}" '
                f'onclick="return loadHistory(this.href, event)">{label or n}</a>')

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
      <div class="ph"><h2><span id="totalCount">{total:,}</span> Confirmed Alerts</h2>
        <div class="phactions" id="normalActions">
          <span class="livedot" id="liveDot"></span>
          <span class="livetxt" id="liveTxt">Live Updates</span>
          <a class="btn ghost" href="/export">Export CSV</a>
          <button class="btn ghost" onclick="enterSelectMode()">Select</button>
          <button class="btn ghost danger" onclick="clearHist()">Clear History</button>
        </div>
        <div class="phactions" id="selectActions" style="display:none">
          <span class="livetxt" id="selCountTxt">0 selected</span>
          <button class="btn ghost" id="selectAllBtn" onclick="selectAllRows()">Select All</button>
          <button class="btn ghost" onclick="deselectAllRows()">Deselect All</button>
          <button class="btn ghost danger" onclick="deleteSelected()">Delete Selected</button>
          <button class="btn ghost" onclick="exitSelectMode()">Cancel</button>
        </div>
      </div>
      <div id="newAlertBanner" style="display:none;margin-top:10px;padding:10px 14px;background:rgba(63,215,196,.1);
        border:1px solid var(--teal2);border-radius:8px;color:var(--teal2);font-size:13px;font-weight:600;
        cursor:pointer;text-align:center" onclick="location.reload()">
        New alert(s) came in &mdash; click to refresh
      </div>
    </div>
    <div class="panel toolbar">
      <form class="searchbox" method="get" onsubmit="return submitHistorySearch(this, event)">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
        <input name="q" value="{q}" placeholder="Search by date, e.g. 2026-07-14">
        <input type="hidden" name="tier" value="{tier}">
        <input type="hidden" name="range" value="{rng}">
      </form>
      <div class="fpills" id="fpillsBar">{pills}</div>
    </div>
    <!-- historyResults wraps everything that changes when Last 24h /
         Include acknowledged / search / pagination change - loadHistory()
         below fetches the new URL and swaps ONLY this container's HTML in
         place, instead of a full browser navigation. That's what removes
         the remaining lag/spinner on those filters: no full-page reload,
         no re-fetch of the sidebar/header/CSS, no lost scroll position -
         same idea as the tier buttons above, just extended to the two
         filters that still need a real server query (they change which
         rows exist at all, not just which of the current rows are shown).
         The <a href> targets are kept as-is underneath, so the page still
         works completely normally if JavaScript is ever unavailable. -->
    <div id="historyResults">
    <div class="panel" style="padding:0;overflow:hidden">
      <table class="histtable" id="histTable">
        <tr><th class="selcell" style="display:none"></th><th>Event</th><th>Camera</th><th>Distance</th><th>Time</th><th>Snapshot</th><th></th></tr>
        {trows}
        <tr id="tierEmptyRow" style="display:none"><td colspan="6" style="color:var(--dim);padding:22px">No alerts match this filter.</td></tr>
      </table>
    </div>
    <div class="pager">
      <span class="muted" id="showingTxt">Showing {lo}&ndash;{hi} of {total}</span>
      <div class="pnums">{prev_b}{nums}{next_b}</div>
    </div>
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
      <div class="snaphint">Scroll to zoom &bull; Drag to move &bull; Press Esc to close</div>
    </div>

    <script>
    // Running total shown in the header/pager, kept in sync client-side as
    // rows are removed - avoids a full page reload (and the resulting
    // continuous spinner) for what should be an instant, local UI update.
    let currentTotal = {total};

    // Which tier is active is now determined by the SERVER's response
    // every time (see loadHistory() below reading it straight off the
    // freshly-fetched pills), not a client-side guess - this variable just
    // mirrors that for anything that wants to display/compare it locally.
    let activeTierFilter = {tier!r};

    // ---- Last 24h / Include acknowledged / search / pagination ----
    // These change which rows exist at all (a real server-side date-range/
    // visibility/search query, not just which already-loaded rows are
    // shown), so - unlike the tier buttons above - they DO need a fresh
    // request. What they must NOT do is a full browser page navigation:
    // that reloads the sidebar, header, CSS and every script on the page
    // from scratch and shows the browser's own loading spinner, which is
    // exactly the remaining lag/spinner being reported. Fetching the same
    // URL in the background and swapping in just the #historyResults
    // (table + pager) and #fpillsBar (so the new active-filter highlight
    // is correct) containers keeps it feeling instant while still getting
    // fully correct, up-to-date rows from the database every time.
    let _historyLoadInFlight = false;
    async function loadHistory(url, event){{
      if(event) event.preventDefault();
      if(_historyLoadInFlight) return false;   // avoid stacking overlapping fetches
      _historyLoadInFlight = true;
      const panel = document.getElementById('historyResults');
      if(panel) panel.style.opacity = '0.55';
      try{{
        const r = await fetch(url, {{headers: {{'X-Requested-With':'fetch'}}}});
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const newResults = doc.getElementById('historyResults');
        const newPills = doc.getElementById('fpillsBar');
        const newTotalEl = doc.getElementById('totalCount');
        if(newResults) document.getElementById('historyResults').innerHTML = newResults.innerHTML;
        if(newPills) document.getElementById('fpillsBar').innerHTML = newPills.innerHTML;
        if(newTotalEl) {{
          currentTotal = parseInt(newTotalEl.textContent.replace(/,/g,''), 10) || 0;
          document.getElementById('totalCount').textContent = currentTotal.toLocaleString();
        }}
        // The server already rendered the correct pill as .active (it
        // knows the real tier= it just queried with) - read that back
        // instead of trusting a client-side guess, so activeTierFilter
        // always reflects what the database actually returned.
        const activePill = document.querySelector('#fpillsBar .fpill[data-tierval].active');
        activeTierFilter = activePill ? activePill.dataset.tierval : 'all';
        window.history.pushState({{}}, '', url);
      }}catch(e){{
        // Network hiccup - fall back to a normal navigation rather than
        // leaving the user stuck on a filter click that silently did
        // nothing.
        window.location.href = url;
      }}finally{{
        _historyLoadInFlight = false;
        if(panel) panel.style.opacity = '1';
      }}
      return false;
    }}

    function submitHistorySearch(form, event){{
      event.preventDefault();
      const params = new URLSearchParams(new FormData(form));
      // Empty search should still clear back to "no q" cleanly rather than
      // sending an empty q= param forever.
      if(!params.get('q')) params.delete('q');
      loadHistory('/history?' + params.toString());
      return false;
    }}

    function renumberVisibleCount(){{
      const visible = document.querySelectorAll('#histTable tr[data-id]').length;
      document.getElementById('totalCount').textContent = currentTotal.toLocaleString();
      // "Showing X-Y of Z" only needs Z (the total) to stay accurate after
      // an in-place delete - X/Y describe this page's slice, which doesn't
      // shift just because a row disappeared from view.
      const showingEl = document.getElementById('showingTxt');
      const m = showingEl.textContent.match(/Showing (\\d+).(\\d+) of \\d+/);
      if(m) showingEl.textContent = 'Showing ' + m[1] + '\\u2013' + m[2] + ' of ' + currentTotal;
      if(visible === 0){{
        document.getElementById('histTable').innerHTML =
          "<tr><td colspan='6' style='color:var(--dim);padding:22px'>No alerts match this filter.</td></tr>";
      }}
    }}

    // Removes a row from the DOM in place - no reload, no scroll jump, no
    // refetch. Used by both single delete and bulk "Delete Selected".
    function removeRowsFromDom(ids){{
      const idSet = new Set(ids.map(String));
      document.querySelectorAll('#histTable tr[data-id]').forEach(function(tr){{
        if(idSet.has(tr.dataset.id)){{
          tr.remove();
          currentTotal = Math.max(0, currentTotal - 1);
        }}
      }});
      renumberVisibleCount();
    }}

    async function clearHist(){{
      const ok = await openModal({{
        title: 'Clear History?',
        message: 'Clear all entries from this view. This does not delete exported records.',
        confirmText: 'Clear History',
        danger: true,
        icon: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
      }});
      if(!ok) return;
      try{{ await fetch('/api/alerts/dismiss_all',{{method:'POST'}}); }}catch(e){{}}
      location.reload();
    }}

    async function delHistAlert(id, event){{
      event.stopPropagation();
      const ok = await openModal({{
        title: 'Delete this alert?',
        message: 'This will permanently remove the alert and its saved media. This action cannot be undone.',
        confirmText: 'Delete',
        danger: true,
        icon: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
      }});
      if(!ok) return;
      try{{
        const r = await fetch('/api/alert/'+id+'/delete',{{method:'POST'}});
        const d = await r.json();
        if(d && d.ok) removeRowsFromDom([id]);
      }}catch(e){{}}
    }}

    // ---- Select mode: checkboxes, Select All (scoped to what's on screen
    // under the current filter/page), Delete Selected, Cancel ----
    let selectMode = false;
    const selectedIds = new Set();

    function enterSelectMode(){{
      selectMode = true;
      selectedIds.clear();
      document.getElementById('normalActions').style.display = 'none';
      document.getElementById('selectActions').style.display = 'flex';
      document.getElementById('histTable').classList.add('selectmode');
      document.querySelectorAll('#histTable .selcell').forEach(function(c){{ c.style.display = ''; }});
      document.querySelectorAll('#histTable tr[data-id]').forEach(function(tr){{
        tr.classList.remove('rowselected');
        const chk = tr.querySelector('.rowchk');
        if(chk) chk.checked = false;
      }});
      updateSelCount();
    }}

    function exitSelectMode(){{
      selectMode = false;
      selectedIds.clear();
      document.getElementById('normalActions').style.display = 'flex';
      document.getElementById('selectActions').style.display = 'none';
      document.getElementById('histTable').classList.remove('selectmode');
      document.querySelectorAll('#histTable .selcell').forEach(function(c){{ c.style.display = 'none'; }});
      document.querySelectorAll('#histTable tr[data-id]').forEach(function(tr){{
        tr.classList.remove('rowselected');
      }});
    }}

    function setRowSelected(tr, selected){{
      const id = parseInt(tr.dataset.id, 10);
      const chk = tr.querySelector('.rowchk');
      if(selected){{
        selectedIds.add(id);
        tr.classList.add('rowselected');
        if(chk) chk.checked = true;
      }}else{{
        selectedIds.delete(id);
        tr.classList.remove('rowselected');
        if(chk) chk.checked = false;
      }}
    }}

    function toggleRowSelect(id, checkbox){{
      const tr = checkbox.closest('tr');
      setRowSelected(tr, checkbox.checked);
      updateSelCount();
    }}

    // Tapping anywhere on an alert row (not just its checkbox) toggles
    // selection while Select mode is active. Outside Select mode this is a
    // no-op, so normal browsing (View/Delete buttons, etc.) is unaffected -
    // those buttons also call event.stopPropagation() so they never
    // double-fire a row toggle on top of their own action.
    function rowClicked(event, id){{
      if(!selectMode) return;
      const tr = event.currentTarget;
      setRowSelected(tr, !selectedIds.has(id));
      updateSelCount();
    }}

    function updateSelCount(){{
      const n = selectedIds.size;
      document.getElementById('selCountTxt').textContent = n + (n === 1 ? ' selected' : ' selected');
    }}

    // Selects every alert row currently on screen - since rows are already
    // filtered server-side by the active tier/date/search filter before
    // this page renders, "every row on screen" and "every row under the
    // active filter" are the same set. Nothing outside the current
    // filter/page is ever included.
    function selectAllRows(){{
      document.querySelectorAll('#histTable tr[data-id]').forEach(function(tr){{
        setRowSelected(tr, true);
      }});
      updateSelCount();
    }}

    // Immediately clears every selection without exiting Select mode - the
    // user can keep tapping rows to build a new selection right away.
    function deselectAllRows(){{
      document.querySelectorAll('#histTable tr[data-id]').forEach(function(tr){{
        setRowSelected(tr, false);
      }});
      updateSelCount();
    }}

    async function deleteSelected(){{
      const n = selectedIds.size;
      if(n === 0) return;
      const ok = await openModal({{
        title: 'Delete ' + n + (n === 1 ? ' alert?' : ' alerts?'),
        message: 'This will permanently remove ' + (n === 1 ? 'this alert' : 'these alerts') +
                  ' and their saved media. This action cannot be undone.',
        confirmText: 'Delete',
        danger: true,
        icon: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
      }});
      if(!ok) return;
      const ids = Array.from(selectedIds);
      try{{
        const r = await fetch('/api/alerts/delete_many', {{
          method: 'POST', headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{ids: ids}})
        }});
        const d = await r.json();
        if(d && d.ok){{
          removeRowsFromDom(d.deleted || ids);
        }}
      }}catch(e){{}}
      exitSelectMode();
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

    // ---- live connection indicator + "new alerts" banner ----
    // History is a paginated/filtered table, so a brand-new alert must NOT
    // silently reload the page out from under you (that would blow away
    // your current filter/page). Instead: show a small banner you can click
    // to refresh when you're ready.
    (function(){{
      const es = new EventSource('/api/alerts/stream');
      es.onopen = function(){{
        const dot = document.getElementById('liveDot'); if(dot) dot.style.background = 'var(--green)';
      }};
      es.onerror = function(){{
        const dot = document.getElementById('liveDot'); if(dot) dot.style.background = 'var(--red)';
      }};
      es.onmessage = function(){{
        const banner = document.getElementById('newAlertBanner');
        if(banner) banner.style.display = 'block';
      }};
    }})();

    // Back/forward browser navigation still works correctly even though
    // filter/pagination clicks no longer do a real page load - reload the
    // same swapped-content path for whatever URL the browser now shows,
    // instead of a hard navigation.
    window.addEventListener('popstate', function(){{
      loadHistory(window.location.href);
    }});
    </script>
    <style>
      .phactions{{display:flex;align-items:center;gap:10px}}
      .livedot{{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;
        box-shadow:0 0 0 3px rgba(63,185,80,.12)}}
      .livetxt{{font-size:11px;color:var(--muted);letter-spacing:.5px;margin-right:2px}}
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
      .delbtn{{background:transparent;border:1px solid var(--line);color:var(--text);
        border-radius:7px;padding:8px 12px;font-size:14px;font-weight:600;cursor:pointer;
        transition:.15s;width:36px;height:36px;display:inline-flex;align-items:center;
        justify-content:center;vertical-align:middle}}
      .delbtn:hover{{color:var(--red);border-color:var(--red);background:rgba(229,72,77,.08)}}

      /* Select mode: checkboxes + row highlight for multi-select delete */
      .selcell{{width:36px;padding-right:0 !important}}
      .rowchk{{width:17px;height:17px;accent-color:var(--teal2);cursor:pointer}}
      #histTable tr[data-id]{{transition:background-color .12s}}
      #histTable.selectmode tr[data-id]{{cursor:pointer}}
      #histTable.selectmode tr[data-id]:hover{{background:rgba(63,215,196,.05)}}
      #histTable tr.rowselected{{background:rgba(63,215,196,.08)}}
      #histTable tr.rowselected td{{border-top-color:rgba(63,215,196,.25)}}
      #selectActions{{display:flex;align-items:center;gap:10px}}
      #selCountTxt{{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}}

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
                subtitle=f'<div>{total:,} Confirmed Alerts</div><div>View and manage previous alerts</div>')


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
    _now_ts = time.time()
    _open_workers = [w for w in workers if not w.paused]
    stalled = any((_now_ts - getattr(w, "_last_frame_ok", 0)) > 10
                  for w in _open_workers) if _open_workers else False
    G, T, M, R = "var(--green)", "var(--teal2)", "var(--muted)", "var(--red)"
    if not yolo_ok:
        _yolo_status, _yolo_color = "off", R
    elif stalled:
        _yolo_status, _yolo_color = "stalled", R
    else:
        _yolo_status, _yolo_color = "running", G
    mods = [("OpenCV capture", "running", G),
            ("Person Detection", _yolo_status, _yolo_color),
            ("Tier engine", "stalled" if stalled else "running", R if stalled else G),
            ("Night vision (CLAHE)", "engaged" if nv_on else "standby", T if nv_on else M),
            ("Flask API / MJPEG", "running", G),
            ("Database", "Connected", T),
            ("Cloud Sync", (f"offline &middot; queue {pending}" if pending else "Up to date"), R if pending else G),
            ("Push Notifications", "Connected" if fcm_on else "off", T if fcm_on else M)]
    modrows = "".join(
        f'<div class="modrow"><span><span class="dot" style="background:{c}"></span>{n}</span>'
        f'<span class="modtag" style="color:{c};border-color:{c}">{s}</span></div>' for n, s, c in mods)

    alert_color = "var(--red)" if alerts_24h else "var(--muted)"
    body = f"""
    <div class="grid2">
      <div class="panel logpanel">
        <div class="ph">
          <h2>Live Log</h2>
          <div class="logfilters">
            <button class="fbtn active" onclick="setFilter('ALL',this)">All</button>
            <button class="fbtn" onclick="setFilter('DETECT',this)">Detection</button>
            <button class="fbtn" onclick="setFilter('ALERT',this)">Alerts</button>
            <button class="fbtn" onclick="setFilter('SYNC',this)">Sync</button>
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
    return page("System Diagnostics", "/logs", body,
                subtitle="Detection, alerts, and sync activity")


SETTINGS_TABS = [("detection", "Detection"), ("cameras", "Cameras"), ("alerts", "Alerts"),
                 ("storage", "Storage &amp; Sync"), ("device", "Connect Phone"),
                 ("offline", "Offline Mode"), ("users", "Users"), ("about", "About")]


def _sw(name, on):
    return (f'<label class="sw"><input type="checkbox" name="{name}" {"checked" if on else ""}>'
            f'<span class="track"></span></label>')


def _irow(label, value):
    return f'<div class="irow"><span>{label}</span><b>{value}</b></div>'


def _fmt_hour(hour, fmt="24h"):
    """0-23 hour -> a display string in either 24h ('22:00') or 12h ('10:00 PM') format."""
    hour = int(hour) % 24
    if fmt == "12h":
        suffix = "AM" if hour < 12 else "PM"
        h12 = hour % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:00 {suffix}"
    return f"{hour:02d}:00"


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = Database(config.DB_PATH)
    if request.method == "POST":
        section = request.form.get("section", "detection")
        if section == "detection":
            if request.form.get("action") == "reset":
                sens, conf, t1, t3, night, auto = (1500, 0.5, config.TIER1_MIN_DIST,
                                                   config.TIER3_MAX_DIST, 0, 0)
                autoarm_start, autoarm_end, time_fmt = (
                    getattr(config, "AUTO_ARM_START_HOUR", 22),
                    getattr(config, "AUTO_ARM_END_HOUR", 6), "24h")
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
                time_fmt = request.form.get("time_format", "24h")
                # <input type="time"> always posts 24h "HH:MM" regardless of
                # the display format picked - we only need the hour to drive
                # the scheduler; the "12h/24h" choice is purely how it's shown.
                def _parse_hour(field, fallback):
                    raw = request.form.get(field, "")
                    try:
                        return int(raw.split(":")[0])
                    except (ValueError, IndexError):
                        return fallback
                autoarm_start = _parse_hour("autoarm_start", getattr(config, "AUTO_ARM_START_HOUR", 22))
                autoarm_end = _parse_hour("autoarm_end", getattr(config, "AUTO_ARM_END_HOUR", 6))
            db.conn.execute("UPDATE settings SET sensitivity=?, person_conf=?, auto_arm_night=?, night_vision=? WHERE setting_id=1",
                            (sens, conf, auto, night))
            db.conn.commit()
            # tier distances + auto-arm schedule have no DB column, so persist to prefs
            save_prefs({"tier1": float(t1), "tier3": float(t3),
                       "autoarm": bool(auto),
                       "autoarm_start_hour": int(autoarm_start),
                       "autoarm_end_hour": int(autoarm_end),
                       "time_format": time_fmt})
            for w in workers:
                w.update_settings(sens, conf, t1, t3, night, False)
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
        if section == "name":
            new_name = request.form.get("display_name", "").strip()
            uid = session.get("user_uid")
            ok = False
            if uid and new_name:
                try:
                    from firebase_admin import auth as fb_auth
                    from firebase_auth import init_firebase
                    init_firebase()
                    fb_auth.update_user(uid, display_name=new_name)
                    session["display_name"] = new_name
                    ok = True
                except Exception:
                    ok = False
            db.close()
            return redirect(url_for("settings", tab="users", name=("ok" if ok else "err")))
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
    night_on = bool(s["night_vision"])
    # Auto-Arm at Night is its own column now - it used to share the same
    # 'armed' column as the live arm/disarm state, which meant toggling this
    # setting would arm/disarm the system for real. See database.py migration.
    auto_on = bool(s["auto_arm_night"]) if "auto_arm_night" in s.keys() else False
    autoarm_start_h = prefs.get("autoarm_start_hour", getattr(config, "AUTO_ARM_START_HOUR", 22))
    autoarm_end_h = prefs.get("autoarm_end_hour", getattr(config, "AUTO_ARM_END_HOUR", 6))
    time_fmt = prefs.get("time_format", "24h")

    # ---- Detection ----
    sec_detection = f"""
      <form method="post">
        <input type="hidden" name="section" value="detection">
        <div class="sechead">Detection</div><div class="subd">Adjust how CAPHY detects people.</div>
        <div class="field">
          <div class="flabel"><b>Motion Sensitivity (Factor 1)</b><span class="val" id="motVal">{mot_pct}%</span></div>
          <div class="fdesc">Higher sensitivity detects smaller movements.</div>
          <input type="range" id="mot" name="motion" min="0" max="100" value="{mot_pct}"
                 oninput="document.getElementById('motVal').textContent=this.value+'%'">
        </div>
        <div class="field">
          <div class="flabel"><b>Person Confidence (Factor 2)</b><span class="val" id="pcVal">{pconf:.2f}</span></div>
          <div class="fdesc">Minimum confidence needed to confirm a person.</div>
          <input type="range" id="pc" name="person_conf" min="0.3" max="0.95" step="0.01" value="{pconf}"
                 oninput="document.getElementById('pcVal').textContent=(+this.value).toFixed(2)">
        </div>
        <div class="secheadsmall">THREE-TIER DISTANCE THRESHOLDS</div>
        <div class="tiercards">
          <div class="tiercard t1"><div class="tl">Tier 1 &middot; Far Away</div>
            <div class="tv">&gt; <input class="tin" name="tier1" value="{t1}"> m</div><div class="tsub">Record event only</div></div>
          <div class="tiercard t2"><div class="tl">Tier 2 &middot; Medium Distance</div>
            <div class="tv">{t3}&ndash;{t1} m</div><div class="tsub">Take a snapshot and notify</div></div>
          <div class="tiercard t3"><div class="tl">Tier 3 &middot; Close Range</div>
            <div class="tv">&lt; <input class="tin" name="tier3" value="{t3}"> m</div><div class="tsub">Record video, sound siren, and notify</div></div>
        </div>
        <div class="togglerow"><div><b>Software Night Vision (CLAHE)</b>
          <div class="fdesc" style="margin:2px 0 0">Brightens dark camera images automatically.</div></div>{_sw("night", night_on)}</div>
        <div class="togglerow"><div><b>Auto-Arm at Night</b>
          <div class="fdesc" style="margin:2px 0 0">Automatically arm the system during selected hours.</div></div>{_sw("autoarm", auto_on)}</div>
        <div class="field" id="autoarmSettings" style="display:{'block' if auto_on else 'none'};padding:12px;background:var(--panel);border-radius:8px;margin-top:8px">
          <div class="flabel"><b>Auto-Arm Schedule</b></div>
          <div class="fdesc">System arms automatically at start time and disarms at end time. Pick any time you want.</div>
          <div style="display:flex;gap:12px;margin-top:12px;align-items:center;flex-wrap:wrap">
            <div style="flex:1;min-width:120px">
              <label style="font-size:12px;color:var(--muted)">Start Time</label>
              <input type="time" name="autoarm_start" value="{autoarm_start_h:02d}:00" style="width:100%;padding:6px;border:1px solid var(--line);border-radius:4px">
            </div>
            <div style="flex:1;min-width:120px">
              <label style="font-size:12px;color:var(--muted)">End Time</label>
              <input type="time" name="autoarm_end" value="{autoarm_end_h:02d}:00" style="width:100%;padding:6px;border:1px solid var(--line);border-radius:4px">
            </div>
            <div style="min-width:140px">
              <label style="font-size:12px;color:var(--muted)">Time Format</label>
              <select name="time_format" style="width:100%;padding:6px;border:1px solid var(--line);border-radius:4px">
                <option value="24h" {"selected" if time_fmt == "24h" else ""}>24-Hour Format</option>
                <option value="12h" {"selected" if time_fmt == "12h" else ""}>12-Hour Format</option>
              </select>
            </div>
          </div>
          <div class="fdesc" style="margin-top:8px" id="autoarmPreview">
            Currently: arms at {_fmt_hour(autoarm_start_h, time_fmt)}, disarms at {_fmt_hour(autoarm_end_h, time_fmt)}
          </div>
        </div>
        <div class="setfoot">
          <button class="btn ghost" name="action" value="reset">Reset Settings</button>
          <button class="btn" name="action" value="save">Save Changes</button>
        </div>
      </form>"""

    # ---- Cameras ----
    cam_list = [(w.cam_id, w.name) for w in workers] or list(enumerate(getattr(config, "CAMERA_NAMES", ["Cam 0", "Cam 1"])))
    cam_inputs = "".join(
        f'<div class="field"><div class="flabel"><b>Camera {i}</b></div>'
        f'<div class="fdesc">Display name shown across the console &amp; saved with each alert</div>'
        f'<input name="cam_name_{i}" value="{nm}" style="width:100%"></div>' for i, nm in cam_list)
    sec_cameras = f"""
      <div class="sechead">Cameras</div><div class="subd">Select the cameras CAPHY will use.</div>

      <div class="secheadsmall">Detect Cameras</div>
      <div style="color:var(--muted);font-size:13px;margin-bottom:10px">
        CAPHY detects every camera device your operating system recognizes -
        built-in webcams and USB cameras included - and lists them in the
        dropdowns below. Pick a camera for each slot.
      </div>
      <div id="camPickStatus" style="color:var(--muted);font-size:13px;margin-bottom:10px">Detecting cameras&hellip;</div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">
        <div>
          <label style="font-size:12px;color:var(--muted);font-weight:600">Camera 1</label>
          <select id="camSlot0" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font-size:13px"></select>
        </div>
        <div>
          <label style="font-size:12px;color:var(--muted);font-weight:600">Camera 2</label>
          <select id="camSlot1" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font-size:13px"></select>
        </div>
      </div>

      <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap">
        <button class="btn" id="camApplyBtn" onclick="applyCameras()" disabled>Apply</button>
        <button class="btn ghost" onclick="loadCameras(true)">Rescan</button>
        <span id="camApplyMsg" style="font-size:12px;color:var(--muted)"></span>
      </div>
      <div style="color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6">
        Click Rescan after connecting a new camera.
      </div>

      <form method="post">
        <input type="hidden" name="section" value="cameras">
        <div class="secheadsmall" style="margin-top:22px">Camera Labels</div>
        {cam_inputs}
        <div class="secheadsmall">Capture Settings</div>
        {_irow("Resolution", f"{config.FRAME_WIDTH} &times; {config.FRAME_HEIGHT} (HD - sharp detail without lag)")}
        {_irow("Stream quality", f"{getattr(config,'JPEG_QUALITY',70)} / 100 (edit config.py to change)")}
        {_irow("Network cameras", "None connected. Add an IP/RTSP camera URL in config.py to use one.")}
        <div class="setfoot"><button class="btn" name="action" value="save">Save Names</button></div>
      </form>
    """ + _CAMERA_PICKER_JS

    # ---- Alerts (reflects config) ----
    fmt_t = lambda xs: ", ".join("Tier %s" % t for t in xs) or "none"
    sec_alerts = f"""
      <div class="sechead">Alerts &amp; Response</div>
      <div class="subd">Choose how CAPHY responds to threats.</div>

      <div class="secheadsmall">Threat Response (Three-Tier)</div>
      <div style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:18px">
        <b>Unarmed Mode (Default):</b> Natural threat response based on distance<br>
        <b>Armed Mode:</b> High-alert response on all tiers (siren + recording + push notification)
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:18px">
        <div style="padding:12px;background:var(--panel);border-radius:8px;border-left:3px solid #50c878">
          <div style="font-weight:600;margin-bottom:8px">Tier 1 - Far Distance</div>
          <div style="font-size:12px;color:var(--muted)">Snapshot<br>Notification</div>
        </div>
        <div style="padding:12px;background:var(--panel);border-radius:8px;border-left:3px solid #5b8dff">
          <div style="font-weight:600;margin-bottom:8px">Tier 2 - Medium Distance</div>
          <div style="font-size:12px;color:var(--muted)">Snapshot<br>5-second video<br>Notification</div>
        </div>
        <div style="padding:12px;background:var(--panel);border-radius:8px;border-left:3px solid #e5544e">
          <div style="font-weight:600;margin-bottom:8px">Tier 3 - Close Distance</div>
          <div style="font-size:12px;color:var(--muted)">Snapshot<br>Video until exit<br>Siren<br>Alert (requires acknowledgment)</div>
        </div>
      </div>

      <div class="secheadsmall">ALERT CONFIGURATION</div>
      {_irow("Save snapshot on", fmt_t(getattr(config,'SNAPSHOT_TIERS',[])))}
      {_irow("Record video on", fmt_t(getattr(config,'RECORD_TIERS',[])))}
      {_irow("Sound siren on", fmt_t(getattr(config,'SIREN_TIERS',[])))}
      {_irow("Push to phone from", "Tier %s up (or all tiers when armed)" % getattr(config,'PUSH_MIN_TIER',1))}
      {_irow("Detection Delay", f"{getattr(config,'ALERT_COOLDOWN_SEC',5)} s between detections")}
      {_irow("Recording Extension", f"{getattr(config,'PRESENCE_GRACE_SEC',1.5)} s after person leaves")}"""

    # ---- Storage & Sync ----
    sync_status = "All synced ✓" if pending == 0 else f"{pending} awaiting upload"
    is_online_now = internet_available()
    sec_storage = f"""
      <div class="sechead">Storage &amp; Sync</div>
      <div class="subd">Store alerts locally and sync them to the cloud.</div>

      <div class="secheadsmall">SYNC STATUS</div>
      {_irow("Internet connection", "Online" if is_online_now else "Offline")}
      {_irow("Saved Alerts", f"{total} total")}
      {_irow("Cloud Status", sync_status)}
      <div style="color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6">
        {"Alerts upload to the cloud automatically in the background - a new alert syncs within seconds, and any older ones still queued get retried every 30s while you're online. No manual action needed." if is_online_now else "You're offline right now, so alerts are queued locally. They'll upload automatically the moment internet comes back - nothing to do on your end."}
      </div>

      <div class="secheadsmall">STORAGE CONFIGURATION</div>
      {_irow("Captures folder", getattr(config,'CAPTURES_DIR','captures'))}
      {_irow("Compressed folder", getattr(config,'COMPRESSED_DIR','captures_compressed'))}
      {_irow("Upload image quality", f"{getattr(config,'IMAGE_QUALITY',60)} / 100 (balance size vs quality)")}
      {_irow("Sync retry interval", "Every 30 s while online")}
      {_irow("Delete alerts after", f"{getattr(config,'RETENTION_DAYS',30)} days")}"""
    # Firebase bucket name is intentionally not shown in the UI (hidden per
    # design spec) - the underlying config value above is still read/used
    # by the backend, it's just not displayed here anymore.

    # ---- Connect Phone (scan-to-connect, no phone login) ----
    sec_device = """
      <div class="sechead">Connect Your Phone</div>
      <div class="subd">Scan the QR code below using the CAPHY app.</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:18px">
        <b>Easy setup:</b> Open the CAPHY app on your phone and scan the QR code below.
        That's it &mdash; your phone is now connected and signed in. No passwords to type.
      </p>
      <div id="pairWrap" style="display:flex;flex-direction:column;align-items:center;gap:14px;padding:20px 0">
        <div id="pairQr" style="background:#fff;padding:16px;border-radius:12px"></div>
        <div id="pairStatus" style="color:var(--muted);font-size:13px">Generating code&hellip;</div>
        <button class="btn ghost" onclick="genPairCode()">Generate New Code</button>
      </div>
      <div style="color:var(--dim);font-size:11.5px;margin-top:12px;line-height:1.6;text-align:center">
        QR code expires in 3 minutes.<br>Can be scanned from any internet connection.
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
    msg = ('<div class="fdesc" style="color:var(--green)">✓ Password updated.</div>' if pw == "ok"
           else '<div class="fdesc" style="color:var(--red)">✗ Current password is incorrect.</div>' if pw == "err" else "")
    email = session.get("email","admin")
    display_name = session.get("display_name", "")
    # Check if user signed in via Google (no password set yet)
    google_prompt = ""
    if session.get("signed_in_with") == "google":
        google_prompt = '<div style="padding:12px;background:rgba(63,215,196,.1);border-radius:8px;border-left:3px solid var(--teal2);margin-bottom:16px;color:var(--muted);font-size:13px"><b>Tip:</b> You signed in with Google. To control CAPHY from the command line or use password-based login, you can optionally set a password below.</div>'

    name_status = request.args.get("name", "")
    name_msg = ('<div class="fdesc" style="color:var(--green)">✓ Name updated.</div>' if name_status == "ok"
                else '<div class="fdesc" style="color:var(--red)">✗ Couldn\'t update name. Try again.</div>' if name_status == "err" else "")

    sec_users = f"""
      <form method="post">
        <input type="hidden" name="section" value="name">
        <div class="sechead">User Account</div>
        <div class="subd">Manage your account</div>
        {_irow("Email", email)}
        {_irow("Account type", "Homeowner")}

        <div class="secheadsmall" style="margin-top:20px">Display Name</div>
        <div class="fdesc" style="margin-bottom:6px">Shown throughout CAPHY.</div>
        <div class="field">
          <input type="text" name="display_name" value="{display_name}" placeholder="Your name" style="width:100%">{name_msg}</div>
        <div class="setfoot"><button class="btn" name="action" value="save">Save Name</button></div>
      </form>

      <form id="changePwForm" onsubmit="return caphyChangePasswordSubmit(event)" style="margin-top:24px">
        <div class="secheadsmall" style="margin-top:20px">Change Password</div>
        {google_prompt}
        <div class="field"><div class="flabel"><b>Current password</b></div>
          <input type="password" id="cur_pw" style="width:100%"></div>
        <div class="field"><div class="flabel"><b>New Password</b></div>
          <div class="fdesc">Must contain at least 6 characters.</div>
          <input type="password" id="new_pw" style="width:100%">{msg}</div>
        <div id="changePwErr" class="fdesc" style="color:var(--red);display:none;margin-top:6px"></div>
        <div class="setfoot"><button class="btn" type="submit" id="changePwBtn">Change Password</button></div>
      </form>

      <!-- Loading overlay - shown the instant "Change Password" is clicked,
           while the current-password check + PIN email are in flight on
           the server, and disappears once that request settles (either
           into the OTP modal below on success, or an inline error). Also
           reused for the OTP-confirm step, since that call updates the
           real account password and should feel identically "in progress"
           to the user. -->
      <div id="pwLoadingOverlay" style="display:none;position:fixed;inset:0;background:rgba(6,6,16,.72);
           backdrop-filter:blur(2px);z-index:10000;align-items:center;justify-content:center">
        <div style="background:var(--card);border:1px solid var(--line);border-radius:16px;
             padding:32px 40px;text-align:center;min-width:220px">
          <div class="pwspinner" style="width:36px;height:36px;border-radius:50%;
               border:3px solid var(--line);border-top-color:var(--teal2);
               margin:0 auto 16px;animation:pwspin .8s linear infinite"></div>
          <div id="pwLoadingMsg" style="font-size:14px;color:var(--text);font-weight:600">Updating Password...</div>
        </div>
      </div>
      <style>@keyframes pwspin{{to{{transform:rotate(360deg)}}}}</style>

      <!-- OTP verification modal - shown after "Change Password" is clicked
           and the current password + new password have both already
           passed validation server-side; the account password itself is
           NOT changed until the code below is confirmed. -->
      <div id="pwOtpOverlay" style="display:none;position:fixed;inset:0;background:rgba(6,6,16,.72);
           backdrop-filter:blur(2px);z-index:9999;align-items:center;justify-content:center">
        <div style="background:var(--card);border:1px solid var(--line);border-radius:16px;
             padding:28px;max-width:360px;width:92%;text-align:center">
          <div style="font-size:16px;font-weight:700;margin-bottom:6px">Verify your email</div>
          <div style="color:var(--muted);font-size:13px;line-height:1.6;margin-bottom:18px">
            We sent a 6-digit code to <b id="pwOtpEmail"></b>. Enter it below to confirm
            your new password. The code expires in 5 minutes.
          </div>
          <input id="pwOtpInput" maxlength="6" inputmode="numeric" autocomplete="one-time-code"
                 style="width:100%;text-align:center;font-size:22px;letter-spacing:8px;
                 padding:10px 0;border-radius:10px;border:1px solid var(--line);
                 background:var(--panel);color:var(--text)" placeholder="------">
          <div id="pwOtpErr" class="fdesc" style="color:var(--red);display:none;margin-top:10px"></div>
          <div style="display:flex;gap:10px;margin-top:18px">
            <button type="button" class="btn" style="flex:1;background:transparent;border:1px solid var(--line)"
                    onclick="caphyCancelOtp()">Cancel</button>
            <button type="button" class="btn" style="flex:1" id="pwOtpConfirmBtn"
                    onclick="caphyConfirmOtp()">Confirm</button>
          </div>
          <div style="margin-top:14px;font-size:12.5px;color:var(--muted)">
            Didn't get it?
            <a id="pwOtpResend" href="javascript:void(0)" onclick="caphyResendOtp()"
               style="color:var(--teal2)">Resend code</a>
            <span id="pwOtpCooldown" style="display:none">Resend available in <b id="pwOtpCooldownN">60</b>s</span>
          </div>
        </div>
      </div>

      <script>
      (function(){{
        let pendingNewPw = null;   // held only in memory on this page, never persisted
        let cooldownTimer = null;

        function showLoading(msg){{
          document.getElementById('pwLoadingMsg').textContent = msg || 'Updating Password...';
          document.getElementById('pwLoadingOverlay').style.display = 'flex';
        }}
        function hideLoading(){{
          document.getElementById('pwLoadingOverlay').style.display = 'none';
        }}
        function setFormDisabled(disabled){{
          // Disables every input/button in the Change Password form AND the
          // OTP modal's input/buttons, so nothing can be double-submitted
          // while a request is in flight, regardless of which step is
          // currently showing.
          document.querySelectorAll('#changePwForm input, #changePwForm button').forEach(function(el){{ el.disabled = disabled; }});
          const otpInput = document.getElementById('pwOtpInput');
          const otpBtn = document.getElementById('pwOtpConfirmBtn');
          if(otpInput) otpInput.disabled = disabled;
          if(otpBtn) otpBtn.disabled = disabled;
        }}

        window.caphyChangePasswordSubmit = function(ev){{
          ev.preventDefault();
          const curPw = document.getElementById('cur_pw').value;
          const newPw = document.getElementById('new_pw').value;
          const errEl = document.getElementById('changePwErr');
          errEl.style.display = 'none';
          if(!newPw || newPw.length < 6){{
            errEl.textContent = 'Must contain at least 6 characters.';
            errEl.style.display = 'block';
            return false;
          }}
          setFormDisabled(true);
          showLoading('Updating Password...');
          fetch('/api/auth/change-password/request', {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{current_password: curPw, new_password: newPw}})
          }}).then(r => r.json()).then(d => {{
            setFormDisabled(false);
            hideLoading();
            if(!d.success){{
              errEl.textContent = d.error || 'Could not start password change.';
              errEl.style.display = 'block';
              return;
            }}
            pendingNewPw = newPw;
            document.getElementById('pwOtpEmail').textContent = "{email}";
            document.getElementById('pwOtpInput').value = '';
            document.getElementById('pwOtpErr').style.display = 'none';
            document.getElementById('pwOtpOverlay').style.display = 'flex';
            document.getElementById('pwOtpInput').focus();
            startCooldown();
          }}).catch(() => {{
            setFormDisabled(false);
            hideLoading();
            errEl.textContent = 'Could not reach server. Try again.';
            errEl.style.display = 'block';
          }});
          return false;
        }};

        window.caphyConfirmOtp = function(){{
          const pin = document.getElementById('pwOtpInput').value.trim();
          const errEl = document.getElementById('pwOtpErr');
          if(!/^\\d{{6}}$/.test(pin)){{
            errEl.textContent = 'Enter the 6-digit code.';
            errEl.style.display = 'block';
            return;
          }}
          // Hide the OTP modal while the loading overlay takes over, so
          // there's only ever one "thing happening" on screen at a time -
          // this is the step that actually updates the real password.
          document.getElementById('pwOtpOverlay').style.display = 'none';
          setFormDisabled(true);
          showLoading('Updating Password...');
          fetch('/api/auth/change-password/verify', {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{pin: pin}})
          }}).then(r => r.json()).then(d => {{
            setFormDisabled(false);
            hideLoading();
            if(!d.success){{
              // Failed - bring the OTP modal back so the user can retry
              // without having to re-enter their password from scratch.
              errEl.textContent = d.error || 'Incorrect code.';
              errEl.style.display = 'block';
              document.getElementById('pwOtpOverlay').style.display = 'flex';
              return;
            }}
            // Success - password updated, session untouched (still signed in).
            window.location.href = '/settings?tab=users&pw=ok';
          }}).catch(() => {{
            setFormDisabled(false);
            hideLoading();
            errEl.textContent = 'Could not reach server. Try again.';
            errEl.style.display = 'block';
            document.getElementById('pwOtpOverlay').style.display = 'flex';
          }});
        }};

        window.caphyResendOtp = function(){{
          if(document.getElementById('pwOtpCooldown').style.display !== 'none') return;
          const curPw = document.getElementById('cur_pw').value;
          if(!pendingNewPw) return;
          fetch('/api/auth/change-password/request', {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{current_password: curPw, new_password: pendingNewPw}})
          }}).then(r => r.json()).then(d => {{
            if(d.success) startCooldown();
          }});
        }};

        window.caphyCancelOtp = function(){{
          document.getElementById('pwOtpOverlay').style.display = 'none';
          pendingNewPw = null;
          if(cooldownTimer) clearInterval(cooldownTimer);
        }};

        function startCooldown(){{
          let n = 60;
          const resendLink = document.getElementById('pwOtpResend');
          const cooldownEl = document.getElementById('pwOtpCooldown');
          const cooldownN = document.getElementById('pwOtpCooldownN');
          resendLink.style.display = 'none';
          cooldownEl.style.display = 'inline';
          cooldownN.textContent = n;
          if(cooldownTimer) clearInterval(cooldownTimer);
          cooldownTimer = setInterval(function(){{
            n -= 1;
            cooldownN.textContent = n;
            if(n <= 0){{
              clearInterval(cooldownTimer);
              cooldownEl.style.display = 'none';
              resendLink.style.display = 'inline';
            }}
          }}, 1000);
        }}
      }})();
      </script>"""

    # ---- About ----
    ncam = len(workers)
    sec_about = f"""
      <div class="sechead">About CAPHY</div><div class="subd">AI Security System</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:18px">
        Detects motion first, then confirms a person using AI before sending alerts.</p>
      {_irow("Version", "Console v1.0")}
      {_irow("Detection", "OpenCV motion + YOLOv8-nano")}
      {_irow("Cameras online", f"{ncam}")}
      {_irow("Mobile alerts", "Firebase Cloud Messaging")}"""

    # ---- Offline / Local Mode ----
    lan_ip = _local_ip()
    sec_offline = f"""
      <div class="sechead">Offline Mode</div>
      <div class="subd">Continue using CAPHY without internet.</div>
      <p style="color:var(--muted);font-size:13px;line-height:1.7;margin-bottom:16px">
        <b>No internet?</b> Your phone and this laptop can still talk to each other
        over Wi-Fi. Just keep them on the same network, and everything works
        locally. When internet comes back, the app switches automatically &mdash;
        no setup needed.
      </p>

      <div class="secheadsmall">Laptop Address</div>
      {_irow("Local Wi-Fi address", f"http://{lan_ip}:5000")}
      <div style="color:var(--dim);font-size:11.5px;margin:6px 2px 18px;line-height:1.6">
        The app finds this automatically. Usually you won't need to type it.
      </div>

      <div class="secheadsmall">How to use Offline Mode</div>
      <div style="color:var(--muted);font-size:13px;line-height:1.9;padding:12px;background:var(--panel);border-radius:8px">
        ✓ Keep laptop and phone on the same Wi-Fi (or create a phone hotspot and connect the laptop)<br>
        ✓ Keep CAPHY running on this laptop<br>
        ✓ Open the CAPHY app on your phone &mdash; it switches automatically when offline<br>
        ✓ You'll see an "Offline mode" banner when using local Wi-Fi
      </div>

      <div class="secheadsmall" style="margin-top:18px">Available Offline</div>
      {_irow("Live camera", "✓ Yes")}
      {_irow("Arm, disarm, siren", "✓ Yes")}
      {_irow("Motion detection", "✓ Yes (always works)")}
      {_irow("Push notifications", "✗ No (needs internet)")}
      {_irow("Remote Viewing", "✗ No (needs internet)")}
      {_irow("Cloud Backup", "✗ No (resumes when online)")}
    """

    sections = {"detection": sec_detection, "cameras": sec_cameras, "alerts": sec_alerts,
                "storage": sec_storage, "device": sec_device,
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

    // Toggle auto-arm settings visibility
    document.addEventListener('change', function(e){
      if(e.target.name === 'autoarm'){
        const settings = document.getElementById('autoarmSettings');
        if(settings){
          settings.style.display = e.target.checked ? 'block' : 'none';
        }
      }
    });
    </script>""".replace("__NAV__", nav).replace("__SECS__", secs)
    return page("Settings", "/settings", body,
                subtitle="Manage your CAPHY settings.")


@app.route("/video_feed/<int:cam>")
@app.route("/video_feed")
def video_feed(cam=0):
    def gen():
        # The "no frame yet" placeholder image is identical every time
        # (same gray fill, same size) - encoding it fresh with cv2.imencode
        # on every single loop iteration whenever a camera has no frame was
        # pure wasted CPU, competing for the same cores/GIL time as the
        # Worker threads' actual detection work. Encode it once and reuse
        # the bytes - this is a small win on its own, but on a system doing
        # real-time YOLO inference every CPU cycle not spent on genuinely
        # new work helps keep video smooth.
        placeholder = None
        while True:
            jpg = workers[cam].get_jpeg() if 0 <= cam < len(workers) else None
            if jpg is None:
                if placeholder is None:
                    img = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)
                    placeholder = cv2.imencode(".jpg", img)[1].tobytes()
                jpg = placeholder
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.08)   # ~12 fps stream - lighter on the browser with multiple feeds
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
