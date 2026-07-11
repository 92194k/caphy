"""Phase 4 - Alert manager.

When the two-factor engine confirms a threat, this decides what to save:
  - Tier 2 and 3: a snapshot image
  - Tier 3: also a short video clip
  - every saved alert: a row in the database (with timestamp)

A cooldown stops it from saving on every single frame - otherwise one person
standing there would create hundreds of alerts per second.
"""
import os
import time
from datetime import datetime

import cv2


class AlertManager:
    def __init__(self, db, captures_dir, cooldown_sec, video_seconds,
                 video_fps, snapshot_tiers, video_tiers):
        self.db = db
        self.dir = captures_dir
        self.cooldown = cooldown_sec
        self.video_seconds = video_seconds
        self.fps = video_fps
        self.snapshot_tiers = set(snapshot_tiers)
        self.video_tiers = set(video_tiers)
        os.makedirs(captures_dir, exist_ok=True)

        self.last_time = 0.0     # when we last saved an alert
        self._writer = None      # active video writer (None if not recording)
        self._frames_left = 0    # how many more frames to record

    def _stamp(self):
        """A filename-safe timestamp like 20260711_202140."""
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def handle(self, frame, result):
        """Call this once per frame (after drawing the HUD).
        Returns the new alert_id if it saved one, else None."""

        # if a video is being recorded, keep writing frames until it's done
        if self._writer is not None:
            self._writer.write(frame)
            self._frames_left -= 1
            if self._frames_left <= 0:
                self._writer.release()
                self._writer = None

        if not result["threat"]:
            return None

        # cooldown: don't save again too soon
        now = time.time()
        if now - self.last_time < self.cooldown:
            return None
        self.last_time = now

        # use the closest person (highest tier) as the alert subject
        p = max(result["persons"], key=lambda x: x["tier"])
        tier = p["tier"]
        dist = p["distance_m"]
        conf = p["conf"]
        x1, y1, x2, y2 = p["box"]
        bbox_h = y2 - y1
        stamp = self._stamp()

        snapshot_path = None
        video_path = None

        # save a snapshot for the tiers that need one
        if tier in self.snapshot_tiers:
            snapshot_path = os.path.join(self.dir, f"alert_{stamp}_t{tier}.jpg")
            cv2.imwrite(snapshot_path, frame)

        # start a short video for the tiers that need one
        if tier in self.video_tiers and self._writer is None:
            video_path = os.path.join(self.dir, f"alert_{stamp}_t{tier}.mp4")
            h, w = frame.shape[:2]
            self._writer = cv2.VideoWriter(
                video_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
            self._frames_left = int(self.video_seconds * self.fps)

        # record the alert and its detection detail in the database
        alert_id = self.db.add_alert(tier, dist, conf, snapshot_path, video_path)
        self.db.add_threat_log(alert_id, result["motion_area"], bbox_h, dist, tier)
        return alert_id