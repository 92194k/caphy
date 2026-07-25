"""Alert manager (redesigned tiers).

  Tier 1 -> snapshot + alert
  Tier 2 -> snapshot + record video UNTIL the person leaves the frame + alert
  Tier 3 -> snapshot + siren (handled in main) + record video until stopped + alert

Recording is presence-based: it starts when a person is confirmed at a
recording tier and keeps going until the person has been gone for a short grace
period. Video is written at the real measured FPS so playback speed is correct.
"""
import os
import time
from datetime import datetime

import cv2


def open_video_writer(path_no_ext, fps, size):
    """Open a VideoWriter trying several codecs. mp4v often fails silently on
    Windows OpenCV, so fall back to MJPG (.avi). Returns (writer, full_path)
    or (None, None) if nothing works."""
    wfps = max(min(fps, 30.0), 5.0)
    for fourcc, ext in (("mp4v", "mp4"), ("avc1", "mp4"), ("MJPG", "avi"), ("XVID", "avi")):
        full = f"{path_no_ext}.{ext}"
        writer = cv2.VideoWriter(full, cv2.VideoWriter_fourcc(*fourcc), wfps, size)
        if writer.isOpened():
            return writer, full
        writer.release()
    return None, None


class AlertManager:
    def __init__(self, db, captures_dir, cooldown_sec,
                 snapshot_tiers, record_tiers, presence_grace_sec, camera_name=None,
                 videos_dir=None):
        self.db = db
        self.dir = captures_dir
        self.videos_dir = videos_dir or captures_dir
        self.camera_name = camera_name
        self.cooldown = cooldown_sec
        # This laptop's permanent device_id (B1, identity.py), stamped on every
        # alert this camera worker creates - lets cloud sync/push know whose
        # device an alert came from. Never fails startup if identity.py is
        # missing for any reason (e.g. bare unit test import).
        try:
            from identity import get_device_identity
            self.device_id = get_device_identity()["device_id"]
        except Exception:
            self.device_id = None
        self.snapshot_tiers = set(snapshot_tiers)
        self.record_tiers = set(record_tiers)
        self.presence_grace = presence_grace_sec
        os.makedirs(captures_dir, exist_ok=True)
        os.makedirs(self.videos_dir, exist_ok=True)

        self.last_alert_time = 0.0
        self._writer = None
        self._video_path = None
        self._last_seen = 0.0
        self._stop_requested = False   # set by app/voice "stop siren/recording"

    def request_stop(self):
        """Called when the owner stops the Tier-3 event (app or voice)."""
        self._stop_requested = True

    def _owner_uid(self):
        """
        Firebase UID of the account this alert belongs to.

        AlertManager runs inside a background camera-detection thread, not a
        Flask request, so there's no session to read. In the common case
        (one household, one laptop, one signed-up account) we just use
        whichever account has a firebase_uid set, most-recently-created
        first. Falls back to None if no account exists yet (e.g. detection
        started before anyone signed up) - the alert still saves locally,
        it just won't have an owner to sync/push to until someone signs in.
        """
        try:
            row = self.db.conn.execute(
                "SELECT firebase_uid FROM users "
                "WHERE firebase_uid IS NOT NULL AND firebase_uid != '' "
                "ORDER BY user_id DESC LIMIT 1"
            ).fetchone()
            return row["firebase_uid"] if row else None
        except Exception:
            return None

    def _stamp(self):
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _cam_label(self):
        """Filesystem-safe camera name for filenames, e.g. 'Cam Phone' -> 'CamPhone'."""
        name = (self.camera_name or "Cam").strip()
        return "".join(c for c in name if c.isalnum()) or "Cam"

    def _finalize_video(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None

    def handle(self, frame, result, fps):
        """Call once per frame. `fps` = current measured loop FPS (for correct
        video speed). Returns a new alert_id when an alert row is saved, else None."""
        now = time.time()
        person = result["threat"]
        tier = result["tier"]

        # ---------- presence-based video recording ----------
        if self._writer is not None:
            self._writer.write(frame)
            if person and not self._stop_requested:
                self._last_seen = now
            elif now - self._last_seen > self.presence_grace or self._stop_requested:
                self._finalize_video()
                self._stop_requested = False

        if person and tier in self.record_tiers and self._writer is None and not self._stop_requested:
            h, w = frame.shape[:2]
            base = os.path.join(
                self.videos_dir, f"CAPHY_{self._cam_label()}_{self._stamp()}_t{tier}")
            self._writer, self._video_path = open_video_writer(base, fps, (w, h))
            self._last_seen = now

        # ---------- alert row + snapshot (rate-limited) ----------
        if not person:
            return None
        if now - self.last_alert_time < self.cooldown:
            return None
        self.last_alert_time = now

        p = max(result["persons"], key=lambda x: x["tier"])
        snapshot_path = None
        if tier in self.snapshot_tiers:
            snapshot_path = os.path.join(
                self.dir, f"CAPHY_{self._cam_label()}_{self._stamp()}_t{tier}.jpg")
            cv2.imwrite(snapshot_path, frame)
        video_path = self._video_path if tier in self.record_tiers else None
        owner_uid = self._owner_uid()
        alert_id = self.db.add_alert(tier, p["distance_m"], p["conf"], snapshot_path,
                                     video_path, self.camera_name,
                                     user_uid=owner_uid, device_id=self.device_id)
        self.db.add_threat_log(alert_id, result["motion_area"],
                               p["box"][3] - p["box"][1], p["distance_m"], tier)
        self._send_push(tier, p["distance_m"], owner_uid)
        return alert_id

    def _send_push(self, tier, distance_m, owner_uid):
        """
        Fire a Firebase push notification for this alert (B5). Best-effort:
        no internet, no Firebase configured, or no signed-in owner yet are
        all silently skipped - a push failure must never affect detection,
        recording, or the local siren, which all already happened above.
        """
        if not owner_uid or not self.device_id:
            return   # nobody paired to notify yet
        try:
            from storage.firebase_push import get_push_sender_for_alert
            sender = get_push_sender_for_alert(owner_uid, self.device_id)
            if not sender:
                return
            tier_label = {1: "Motion Detected", 2: "Person Approaching", 3: "Intruder Alert"}
            sender.send_alert(
                title=tier_label.get(tier, "CAPHY Alert"),
                body=f"{self.camera_name or 'Camera'} - person at ~{distance_m:.1f}m (Tier {tier})",
                tier=tier,
                data={"distance_m": f"{distance_m:.2f}"},
            )
        except Exception as e:
            # Never let a push failure interrupt detection.
            print(f"[CAPHY] Push notification skipped ({e})")