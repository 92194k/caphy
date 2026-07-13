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


class AlertManager:
    def __init__(self, db, captures_dir, cooldown_sec,
                 snapshot_tiers, record_tiers, presence_grace_sec):
        self.db = db
        self.dir = captures_dir
        self.cooldown = cooldown_sec
        self.snapshot_tiers = set(snapshot_tiers)
        self.record_tiers = set(record_tiers)
        self.presence_grace = presence_grace_sec
        os.makedirs(captures_dir, exist_ok=True)

        self.last_alert_time = 0.0
        self._writer = None
        self._video_path = None
        self._last_seen = 0.0
        self._stop_requested = False   # set by app/voice "stop siren/recording"

    def request_stop(self):
        """Called when the owner stops the Tier-3 event (app or voice)."""
        self._stop_requested = True

    def _stamp(self):
        return datetime.now().strftime("%Y%m%d_%H%M%S")

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
            self._video_path = os.path.join(self.dir, f"alert_{self._stamp()}_t{tier}.mp4")
            real_fps = max(min(fps, 30.0), 1.0)          # clamp to a sane range
            self._writer = cv2.VideoWriter(
                self._video_path, cv2.VideoWriter_fourcc(*"mp4v"), real_fps, (w, h))
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
            snapshot_path = os.path.join(self.dir, f"alert_{self._stamp()}_t{tier}.jpg")
            cv2.imwrite(snapshot_path, frame)
        video_path = self._video_path if tier in self.record_tiers else None
        alert_id = self.db.add_alert(tier, p["distance_m"], p["conf"], snapshot_path, video_path)
        self.db.add_threat_log(alert_id, result["motion_area"],
                               p["box"][3] - p["box"][1], p["distance_m"], tier)
        return alert_id