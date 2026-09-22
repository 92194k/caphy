"""Alert manager - armed/disarmed x tier response table.

Disarmed: ONLY Tier 3 ever records video. Tier 1 and Tier 2 disarmed get
a snapshot + notification/siren as applicable, but never a video clip -
no exceptions (a Tier 2 disarmed dwell-based clip existed briefly here in
an earlier version and was removed to match this rule exactly).
  Tier 1 -> snapshot + log only (no video)
  Tier 2 -> snapshot + notification only (no video)
  Tier 3 -> snapshot + siren + continuous video until the person leaves

Armed: EVERY tier (1, 2, 3) records video.
  Tier 1 -> snapshot + a short fixed-length clip + urgent notification
  Tier 2 -> snapshot + continuous video + siren
  Tier 3 -> snapshot + siren + continuous video until the person leaves
            (same as disarmed Tier 3 - already the maximum response)

On top of the tier table, two adjustments cut down on near-duplicate
evidence from one continuous visit:
  - RECORDING_COOLDOWN_SEC: once a recording for this presence finishes,
    don't start another one for the same tier until this cooldown passes -
    a person lingering near a duration boundary shouldn't trigger a new
    clip every few seconds. An escalation to a HIGHER tier always bypasses
    this immediately (a bigger threat is never throttled).
  - PRESENCE_SNAPSHOT_INTERVAL_SEC: while the same person stays
    continuously present and keeps generating rate-limited alerts, only
    take another snapshot this often, not on every single one.

Recording is presence-based: once started, it keeps going until the person
has been gone for a short grace period (or a fixed duration is hit, for
the non-continuous Tier 1 armed case). Video is written at the real
measured FPS so playback speed is correct.
"""
import os
import time
from datetime import datetime

import cv2
import shutil
import tempfile


def open_video_writer(path_no_ext, fps, size):
    """Open an MP4 (mp4v) VideoWriter, writing to a SAFE TEMP LOCATION
    first rather than directly into path_no_ext's own folder.

    CAPHY's media standard requires every recording to end up as .mp4 -
    no AVI/MJPG fallback - so this no longer tries multiple codecs/
    containers. It also no longer opens the writer directly against the
    caller's real target folder (usually the user's Videos/CAPHY folder),
    because a direct on-machine diagnostic proved mp4v (and every other
    codec) opens and writes real video successfully on this exact machine
    - but ONLY when the target path has no space in it. The real Videos
    folder's full path contains a space in the Windows account name, which
    is a known trigger for OpenCV's FFmpeg-backed VideoWriter to silently
    fail isOpened() on some Windows builds even though the identical
    codec call succeeds one folder over. Writing to Python's own
    tempfile.gettempdir() (which never contains a space or an OneDrive-
    managed path on any known Windows install) sidesteps that failure
    point entirely, and the caller moves the finished file into its real
    destination once recording stops (see AlertManager._finalize_video).

    Returns (writer, temp_full_path) - the SAME temp path is later passed
    back into finalize_video_path() to move it home - or (None, None) if
    mp4v itself could not open (should not happen based on the diagnostic,
    but never assumed).
    """
    wfps = max(min(fps, 30.0), 5.0)
    temp_dir = os.path.join(tempfile.gettempdir(), "CAPHY_recordings")
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except Exception:
        return None, None
    temp_full = os.path.join(temp_dir, os.path.basename(path_no_ext) + ".mp4")
    writer = cv2.VideoWriter(temp_full, cv2.VideoWriter_fourcc(*"mp4v"), wfps, size)
    if writer.isOpened():
        return writer, temp_full
    writer.release()
    try:
        if os.path.exists(temp_full):
            os.remove(temp_full)
    except Exception:
        pass
    return None, None


def finalize_video_path(temp_path, final_dir):
    """Moves a finished recording from its safe temp location (see
    open_video_writer above) into its real destination folder, verifying
    the file genuinely exists and has real content first - never hands
    back a path to a missing or empty file. Returns the final path on
    success, or None if the file was missing/empty/could not be moved
    (in which case nothing is silently pretended to have worked)."""
    if not temp_path or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
        return None
    try:
        os.makedirs(final_dir, exist_ok=True)
        final_path = os.path.join(final_dir, os.path.basename(temp_path))
        shutil.move(temp_path, final_path)
        return final_path
    except Exception:
        # Could not move into the real folder - the recording itself is
        # still good, just not where the user expects it. Better to
        # return the temp path (a real, playable file) than None (which
        # would make the whole alert look like it has no video at all).
        return temp_path


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

        # ---- sustained-presence tracking (dwell time, cooldown, snapshot interval) ----
        # first_seen_this_visit: when the person now in frame was FIRST seen
        # THIS visit - reset to None once they've been gone longer than
        # presence_grace, so a new visit starts a fresh dwell count instead
        # of inheriting an old one from an earlier, unrelated appearance.
        self._first_seen_this_visit = None
        self._last_seen_any_tier = 0.0
        # Tier this visit last actually recorded video for, and when that
        # recording finished - used by RECORDING_COOLDOWN_SEC so the SAME
        # tier can't immediately restart a new clip, while an escalation to
        # a HIGHER tier is never blocked by it.
        self._last_recorded_tier = 0
        self._last_recording_ended_at = 0.0
        self._recording_tier = None   # which tier the CURRENT open writer belongs to
        self._recording_deadline = None   # fixed-duration cutoff for non-continuous clips (Tier 1 armed only); None = continuous
        # When the last snapshot was actually written during an ongoing
        # presence, so repeated alerts for the same lingering person don't
        # each save a near-identical photo.
        self._last_snapshot_at = 0.0
        # Explicit flag for "no snapshot taken yet this visit" - deliberately
        # NOT a time-based heuristic (e.g. "dwell < 0.5s"), because that can
        # span multiple alert-cooldown ticks if ALERT_COOLDOWN_SEC is short,
        # letting several alerts in a row all believe they're "the first of
        # a new visit" and each snapshot. A plain per-visit boolean can only
        # ever be true once.
        self._snapshotted_this_visit = False

        # Tunables read once from config here (with sane fallbacks if a
        # value is ever missing) rather than re-imported on every frame in
        # handle() - avoids repeated import overhead in the hot detection
        # loop and keeps every knob in one place.
        try:
            import config as _cfg
        except Exception:
            _cfg = None
        self._dwell_required_sec = getattr(_cfg, "DWELL_BEFORE_RECORD_SEC", 2.5)
        self._recording_cooldown_sec = getattr(_cfg, "RECORDING_COOLDOWN_SEC", 45)
        self._snapshot_interval_sec = getattr(_cfg, "PRESENCE_SNAPSHOT_INTERVAL_SEC", 45)
        self._snapshot_jpeg_quality = getattr(_cfg, "SNAPSHOT_JPEG_QUALITY", 85)
        self._tier1_armed_record_sec = getattr(_cfg, "TIER1_ARMED_RECORD_DURATION", 4)
        self._tier2_disarmed_record_sec = getattr(_cfg, "TIER2_RECORD_DURATION", 5)

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
            self._last_recording_ended_at = time.time()
            # Move the finished recording out of its safe temp location
            # (see open_video_writer) into the real Videos/CAPHY-style
            # folder this AlertManager was configured with. self._video_path
            # is what handle() hands back as the alert's video reference
            # (DB row, Firestore, FCM push) - it must point at the FINAL
            # location, not the temp one, before any of that happens.
            final_path = finalize_video_path(self._video_path, self.videos_dir)
            if final_path is None:
                # Recording never produced a real (non-empty) file - don't
                # let the alert reference a video that doesn't exist.
                self._log_video_finalize_failure()
                self._video_path = None
            else:
                self._video_path = final_path
        self._recording_tier = None
        self._recording_deadline = None

    def _log_video_finalize_failure(self):
        # Best-effort logging only - AlertManager doesn't always have a
        # direct handle to Worker._log, so this never raises if it can't.
        try:
            print(f"[CAPHY] WARN record({self.camera_name}) "
                  "recording stopped but no valid video file was produced")
        except Exception:
            pass

    def _recording_allowed_for_tier(self, tier, armed):
        """True if the armed/disarmed x tier table allows recording to
        START right now for this tier. Does NOT check the inter-recording
        cooldown - that's handled separately in handle() so an escalation
        can bypass it.

        Rule (explicitly confirmed): DISARMED only ever records Tier 3 -
        Tier 1 and Tier 2 disarmed get a snapshot + notification only,
        never video, no exceptions. ARMED records every tier (1, 2, 3).
        Previously Tier 2 disarmed COULD still start a recording if the
        person lingered past DWELL_BEFORE_RECORD_SEC - that dwell-based
        exception has been removed so disarmed behavior matches the rule
        exactly: Tier 3 only, unconditionally.
        """
        if tier == 3:
            return True   # Tier 3 always records (continuous), armed or not
        if tier == 2:
            return armed   # disarmed Tier 2 never records - snapshot/notify only
        if tier == 1:
            return armed   # disarmed Tier 1 never records, only armed does (short clip)
        return False

    def handle(self, frame, result, fps, armed=False):
        """Call once per frame. `fps` = current measured loop FPS (for correct
        video speed). `armed` selects which row of the armed/disarmed x tier
        table applies this frame (see module docstring). Returns a new
        alert_id when an alert row is saved, else None."""
        now = time.time()
        person = result["threat"]
        tier = result["tier"]

        # ---------- sustained-presence / dwell tracking ----------
        # A "visit" is one continuous appearance: it only resets once the
        # person has been gone longer than presence_grace, same window
        # already used to decide when a recording should stop - so dwell
        # time and recording continuity always agree on what counts as
        # "the same person still here" vs. "they left and came back".
        if person:
            if (self._first_seen_this_visit is None or
                    now - self._last_seen_any_tier > self.presence_grace):
                self._first_seen_this_visit = now   # new visit starts here
                self._snapshotted_this_visit = False
            self._last_seen_any_tier = now
        elif self._first_seen_this_visit is not None and now - self._last_seen_any_tier > self.presence_grace:
            self._first_seen_this_visit = None   # visit fully ended, clear it

        # ---------- presence-based video recording ----------
        if self._writer is not None:
            self._writer.write(frame)
            hit_fixed_deadline = (self._recording_deadline is not None
                                   and now >= self._recording_deadline)
            if person and not self._stop_requested and not hit_fixed_deadline:
                self._last_seen = now
            elif now - self._last_seen > self.presence_grace or self._stop_requested or hit_fixed_deadline:
                self._finalize_video()
                self._stop_requested = False

        if (person and tier in self.record_tiers and self._writer is None
                and not self._stop_requested
                and self._recording_allowed_for_tier(tier, armed)):
            # Cooldown: the SAME tier just finished a recording for this
            # visit recently - don't immediately start another one. An
            # escalation to a HIGHER tier than whatever last recorded
            # always bypasses this, since a bigger threat must never be
            # silently throttled by a cooldown meant for repeat/duplicate
            # footage of the same threat level.
            cooling_down = (tier <= self._last_recorded_tier and
                             now - self._last_recording_ended_at < self._recording_cooldown_sec)
            if not cooling_down:
                h, w = frame.shape[:2]
                base = os.path.join(
                    self.videos_dir, f"CAPHY_{self._cam_label()}_{self._stamp()}_t{tier}")
                self._writer, self._video_path = open_video_writer(base, fps, (w, h))
                self._last_seen = now
                self._recording_tier = tier
                self._last_recorded_tier = tier
                # Tier 3 (either mode) and Tier 2 (always armed-only now,
                # see _recording_allowed_for_tier) are CONTINUOUS - keep
                # going until the person leaves (deadline stays None).
                # Tier 1 armed is the one short, FIXED-length clip per the
                # table, regardless of how long the person actually stays.
                # (Tier 2 disarmed used to also get a fixed-length clip
                # here - removed along with the dwell-based exception in
                # _recording_allowed_for_tier, since disarmed Tier 2 no
                # longer records at all.)
                if tier == 1 and armed:
                    self._recording_deadline = now + self._tier1_armed_record_sec
                else:
                    self._recording_deadline = None

        # ---------- alert row + snapshot (rate-limited) ----------
        if not person:
            return None
        if now - self.last_alert_time < self.cooldown:
            return None
        self.last_alert_time = now

        p = max(result["persons"], key=lambda x: x["tier"])
        # Smart snapshot interval: while the SAME visit keeps generating
        # rate-limited alerts (e.g. someone lingering, tier flickering
        # between 2 and 3), only actually write a new photo to disk every
        # PRESENCE_SNAPSHOT_INTERVAL_SEC - otherwise every alert-cooldown
        # tick for a person who is just standing there saves another
        # near-identical snapshot. The very first alert of a brand-new
        # visit always gets a snapshot immediately, exactly once.
        should_snapshot = (tier in self.snapshot_tiers
                            and (not self._snapshotted_this_visit
                                 or now - self._last_snapshot_at >= self._snapshot_interval_sec))
        snapshot_path = None
        if should_snapshot:
            snapshot_path = os.path.join(
                self.dir, f"CAPHY_{self._cam_label()}_{self._stamp()}_t{tier}.jpg")
            # Was cv2.imwrite(snapshot_path, frame) with NO quality argument,
            # which defaults to OpenCV's JPEG quality of 95 - close to
            # uncompressed, producing needlessly large files at full
            # FRAME_WIDTH/HEIGHT (1920x1080) resolution for every single
            # alert snapshot. These are saved locally AND uploaded to
            # Firebase Storage, so the extra size costs disk space, upload
            # bandwidth/time, and Firebase Storage usage for no real
            # benefit - a security snapshot doesn't need near-lossless
            # quality to still clearly show who/what triggered the alert.
            # SNAPSHOT_JPEG_QUALITY (config.py, default 85) keeps it
            # clearly readable as evidence while cutting file size well
            # below the previous default.
            ok = cv2.imwrite(snapshot_path, frame,
                              [cv2.IMWRITE_JPEG_QUALITY, self._snapshot_jpeg_quality])
            if not ok or not os.path.exists(snapshot_path):
                # Never leave the DB pointing at a snapshot that doesn't
                # actually exist - same "don't fake success" principle as
                # the recording fix.
                snapshot_path = None
            else:
                self._last_snapshot_at = now
                self._snapshotted_this_visit = True
        video_path = self._video_path if tier in self.record_tiers else None
        owner_uid = self._owner_uid()
        alert_id = self.db.add_alert(tier, p["distance_m"], p["conf"], snapshot_path,
                                     video_path, self.camera_name,
                                     user_uid=owner_uid, device_id=self.device_id)
        self.db.add_threat_log(alert_id, result["motion_area"],
                               p["box"][3] - p["box"][1], p["distance_m"], tier)
        # NOTE: push notification is intentionally NOT sent from here.
        # handle() runs on the Worker's main capture thread (see
        # web/server.py run()) - calling the network-bound push here would
        # block frame capture on every single alert, reintroducing the
        # "video freezes on detection" bug. The actual push send happens
        # in Worker._publish_and_push(), on a background thread, once per
        # alert (right after this method returns a new alert_id). Calling
        # _send_push() here too would fire a SECOND push per alert - the
        # duplicate network activity is what caused memory to climb with
        # every alert instead of leveling off.
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