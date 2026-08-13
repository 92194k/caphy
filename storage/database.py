"""Phase 4 - Local SQLite database.

Creates the five CAPHY tables and provides simple save/read helpers.
The database file (caphy.db) is created automatically on first run.
"""
import sqlite3
import hashlib
from datetime import datetime


def _now():
    """Current time as a readable timestamp string."""
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        # timeout=10: how long a connection waits on a lock before raising
        # "database is locked", instead of the sqlite3 default of 5s. This
        # file is opened fresh on nearly every Flask request (dashboard
        # polling /api/state, /api/stats, /api/logs every 1.5-2s from
        # multiple tabs, plus background loops), so lock contention is a
        # real, frequent scenario, not an edge case.
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row   # lets us read columns by name
        try:
            # WAL mode lets readers proceed while a write is in progress
            # (the default rollback-journal mode blocks ALL readers during
            # any write). With many polling tabs hitting mostly-read
            # endpoints (state/stats/logs) while a background thread
            # occasionally writes (arm/disarm, new alert, sync retry),
            # this is what actually prevents those reads from stalling
            # behind a write - the timeout= above only bounds how long a
            # stall lasts, WAL mode is what avoids causing one in the
            # first place for the common read-during-write case.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass  # older SQLite builds without WAL support still work, just without this improvement
        self._create()
        self._seed()

    def _create(self):
        """Create the five tables if they don't already exist."""
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE,
            password_hash TEXT,
            role          TEXT,
            firebase_uid  TEXT UNIQUE,
            email         TEXT,
            created_at    TEXT);

        CREATE TABLE IF NOT EXISTS alerts(
            alert_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_uid      TEXT,                -- Firebase UID (owner of this alert)
            device_id     TEXT,                -- CAPHY device_id (which laptop)
            tier          INTEGER,
            distance_m    REAL,
            confidence    REAL,
            snapshot_path TEXT,
            video_path    TEXT,
            camera        TEXT,                -- which camera fired the alert
            synced        INTEGER DEFAULT 0,   -- 0 = not uploaded yet (for Phase 6)
            timestamp     TEXT);

        CREATE TABLE IF NOT EXISTS threat_logs(
            log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id     INTEGER,
            motion_area  REAL,
            bbox_height  INTEGER,
            est_distance REAL,
            tier         INTEGER,
            timestamp    TEXT,
            FOREIGN KEY(alert_id) REFERENCES alerts(alert_id));

        -- EVALUATION DATA (thesis Chapter 4).
        -- alerts/threat_logs only record events that BECAME alerts. This table
        -- records every motion event, including the ones Factor 2 rejected -
        -- that rejected count is the evidence that two-factor validation cuts
        -- false alarms, and it was being thrown away.
        CREATE TABLE IF NOT EXISTS detection_events(
            event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            session      TEXT,     -- experiment label, e.g. "daylight-run1"
            ground_truth TEXT,     -- 'person' / 'no_person' / '' if unlabelled
            camera       TEXT,
            motion       INTEGER,  -- Factor 1 fired
            ran_yolo     INTEGER,  -- Factor 2 actually ran this frame
            person       INTEGER,  -- Factor 2 confirmed a person
            persons_n    INTEGER,
            motion_area  REAL,
            bbox_height  INTEGER,
            est_distance REAL,
            tier         INTEGER,
            confidence   REAL,
            fps          REAL,
            timestamp    TEXT);

        CREATE TABLE IF NOT EXISTS settings(
            setting_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            sensitivity  REAL,
            person_conf  REAL,
            armed        INTEGER,
            night_vision INTEGER);

        -- Push notifications that failed to send (e.g. no internet at the
        -- moment of the alert). Previously a failed push was just lost -
        -- storage/firebase_push.py's send_alert() returns False on failure
        -- but nothing captured that and retried later, unlike alerts (which
        -- already had the synced=0 retry pattern below). One row per failed
        -- attempt; start_cloud_sync_retry() in web/server.py retries these
        -- alongside unsynced alerts and deletes the row once it succeeds.
        CREATE TABLE IF NOT EXISTS pending_pushes(
            push_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id     INTEGER,
            user_uid     TEXT,
            device_id    TEXT,
            title        TEXT,
            body         TEXT,
            data_json    TEXT,
            tier         INTEGER,
            attempts     INTEGER DEFAULT 0,
            created_at   TEXT);

        -- Forgot-password PIN verification. One row per PIN request; a new
        -- request overwrites/adds rather than reusing, and rows are treated
        -- as dead once used=1 or expires_at has passed (checked in Python,
        -- not enforced by SQLite). uid identifies the Firebase account the
        -- PIN is for - looked up by email at request time.
        CREATE TABLE IF NOT EXISTS password_reset_pins(
            pin_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            uid          TEXT,
            email        TEXT,
            pin_hash     TEXT,     -- sha256 of the PIN, never store it raw
            attempts     INTEGER DEFAULT 0,
            used         INTEGER DEFAULT 0,
            created_at   TEXT,
            expires_at   TEXT);

        -- Signup email verification. No Firebase account exists yet when
        -- this row is created - the pending signup's email/password/name
        -- are held here (password only long enough to create the account
        -- right after PIN verification; not meant as long-term storage)
        -- until the PIN is confirmed, at which point the real Firebase
        -- Auth account is created and this row is marked used.
        CREATE TABLE IF NOT EXISTS signup_verify_pins(
            pin_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT,
            name         TEXT,
            password     TEXT,     -- cleared once used=1
            pin_hash     TEXT,
            attempts     INTEGER DEFAULT 0,
            used         INTEGER DEFAULT 0,
            created_at   TEXT,
            expires_at   TEXT);

        -- Change-password email verification (Settings > User Account,
        -- while already signed in - distinct from the forgot-password
        -- flow above, which is for a signed-OUT user who doesn't know
        -- their current password at all). The new password is only
        -- staged here after the current password has already been
        -- checked against Firebase Auth; it's applied for real only once
        -- the emailed PIN is confirmed, and the row is cleared right
        -- after, same lifecycle as signup_verify_pins.
        CREATE TABLE IF NOT EXISTS change_password_pins(
            pin_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            uid          TEXT,
            email        TEXT,
            new_password TEXT,     -- cleared once used=1
            pin_hash     TEXT,
            attempts     INTEGER DEFAULT 0,
            used         INTEGER DEFAULT 0,
            created_at   TEXT,
            expires_at   TEXT);
        """)
        # migration: add 'camera' column to alert databases created before this field existed.
        # Wrapped in try/except because two camera workers open the DB at the same moment on
        # startup and can race to add the column; the loser would otherwise crash its thread
        # with "duplicate column name" and take that camera offline.
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")]
        if "camera" not in cols:
            try:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN camera TEXT")
            except Exception:
                pass   # another connection already added it

        # migration: 'dismissed' marks an alert as handled. Dismissing HIDES an
        # alert from the UI but never deletes the row - the evidence stays in
        # the database for the thesis and for any later review.
        if "dismissed" not in cols:
            try:
                self.conn.execute(
                    "ALTER TABLE alerts ADD COLUMN dismissed INTEGER DEFAULT 0")
            except Exception:
                pass
        if "dismissed_at" not in cols:
            try:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN dismissed_at TEXT")
            except Exception:
                pass

        # migration: add Firebase UID columns for multi-user support
        if "firebase_uid" not in cols:
            try:
                self.conn.execute("ALTER TABLE users ADD COLUMN firebase_uid TEXT UNIQUE")
            except Exception:
                pass
        if "email" not in cols:
            try:
                self.conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
            except Exception:
                pass

        # migration: add user_uid and device_id to alerts (for multi-household scoping)
        alerts_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")]
        if "user_uid" not in alerts_cols:
            try:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN user_uid TEXT")
            except Exception:
                pass
        if "device_id" not in alerts_cols:
            try:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN device_id TEXT")
            except Exception:
                pass

        # migration: 'auto_arm_night' is the Settings > Detection "Auto-Arm at
        # Night" toggle. This used to be stored in the SAME 'armed' column as
        # the real live arm/disarm state, so flipping one silently flipped the
        # other (toggling the schedule setting would arm/disarm the system on
        # the spot, and arming the system would look like the schedule was
        # turned on). Now they're separate columns.
        settings_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(settings)")]
        if "auto_arm_night" not in settings_cols:
            try:
                self.conn.execute("ALTER TABLE settings ADD COLUMN auto_arm_night INTEGER DEFAULT 0")
            except Exception:
                pass

        self.conn.commit()

    def _seed(self):
        """Add a default admin user and a default settings row (once)."""
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            self.conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                ("admin", hashlib.sha256(b"admin").hexdigest(), "Homeowner", _now()))
        if self.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            self.conn.execute(
                "INSERT INTO settings(sensitivity, person_conf, armed, night_vision) VALUES(?,?,?,?)",
                (1500, 0.5, 1, 0))
        self.conn.commit()

    def add_alert(self, tier, distance_m, confidence, snapshot_path, video_path, camera=None,
                 user_uid=None, device_id=None):
        """Save one confirmed threat. Returns the new alert_id.

        user_uid/device_id tag who owns this alert (which account, which
        laptop) so cloud sync (storage/sync.py) and push notifications know
        where it belongs. Left NULL if not supplied - falls back to the
        SyncManager's default uploader when unset.
        """
        cur = self.conn.execute(
            "INSERT INTO alerts(tier, distance_m, confidence, snapshot_path, video_path, camera,"
            " user_uid, device_id, synced, timestamp) VALUES(?,?,?,?,?,?,?,?,0,?)",
            (tier, distance_m, confidence, snapshot_path, video_path, camera,
             user_uid, device_id, _now()))
        self.conn.commit()
        return cur.lastrowid

    def queue_pending_push(self, alert_id, user_uid, device_id, title, body, data, tier):
        """Records a push notification that failed to send, so
        start_cloud_sync_retry() can retry it once internet returns instead
        of it being silently lost (the old behavior - see pending_pushes
        table comment for why this exists)."""
        import json as _json
        self.conn.execute(
            "INSERT INTO pending_pushes(alert_id, user_uid, device_id, title, body,"
            " data_json, tier, attempts, created_at) VALUES(?,?,?,?,?,?,?,0,?)",
            (alert_id, user_uid, device_id, title, body, _json.dumps(data or {}), tier, _now()))
        self.conn.commit()

    def pending_pushes(self, limit=25):
        """Oldest-first, capped batch of not-yet-delivered pushes, same
        LIMIT-per-sweep pattern as the alerts synced=0 query."""
        return self.conn.execute(
            "SELECT * FROM pending_pushes ORDER BY push_id LIMIT ?", (limit,)).fetchall()

    def delete_pending_push(self, push_id):
        """Removes a pending push once it's been successfully delivered."""
        self.conn.execute("DELETE FROM pending_pushes WHERE push_id=?", (push_id,))
        self.conn.commit()

    def bump_pending_push_attempts(self, push_id):
        """Tracks retry attempts so a permanently-failing push (e.g. bad
        FCM token) can eventually be identified/pruned rather than retried
        forever - counting is enough for now; no auto-expiry added since
        that wasn't asked for and would risk silently dropping a real
        alert notification."""
        self.conn.execute(
            "UPDATE pending_pushes SET attempts = attempts + 1 WHERE push_id=?", (push_id,))
        self.conn.commit()

    def expired_alerts(self, retention_days):
        """Alerts older than retention_days, oldest first (FIFO order).

        Same rule for every tier - a Tier-3 alert is not treated specially,
        per the age-based retention policy.
        """
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM alerts WHERE timestamp < datetime('now', ?) ORDER BY alert_id",
            (f"-{int(retention_days)} days",)
        ).fetchall()]

    def delete_alert(self, alert_id):
        """Remove an alert row and its threat_logs rows (files are handled
        by the caller - this only cleans up the database side)."""
        self.conn.execute("DELETE FROM threat_logs WHERE alert_id=?", (alert_id,))
        self.conn.execute("DELETE FROM alerts WHERE alert_id=?", (alert_id,))
        self.conn.commit()

    def add_threat_log(self, alert_id, motion_area, bbox_height, est_distance, tier):
        """Save the detection detail behind an alert (used later for calibration)."""
        self.conn.execute(
            "INSERT INTO threat_logs(alert_id, motion_area, bbox_height, est_distance, tier, timestamp) "
            "VALUES(?,?,?,?,?,?)",
            (alert_id, motion_area, bbox_height, est_distance, tier, _now()))
        self.conn.commit()

    def log_detection_event(self, session, ground_truth, camera, motion, ran_yolo,
                            person, persons_n, motion_area, bbox_height,
                            est_distance, tier, confidence, fps):
        """Record one motion event and what Factor 2 decided about it.

        This is the raw data for the thesis evaluation. Rows where
        motion=1 and person=0 are the false alarms two-factor validation
        prevented - a motion-only system would have alerted on every one.
        """
        self.conn.execute(
            "INSERT INTO detection_events(session, ground_truth, camera, motion,"
            " ran_yolo, person, persons_n, motion_area, bbox_height,"
            " est_distance, tier, confidence, fps, timestamp)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session, ground_truth, camera, int(motion), int(ran_yolo),
             int(person), persons_n, motion_area, bbox_height, est_distance,
             tier, confidence, fps, _now()))
        self.conn.commit()

    def count_alerts(self, include_dismissed=True):
        q = "SELECT COUNT(*) FROM alerts"
        if not include_dismissed:
            q += " WHERE COALESCE(dismissed,0)=0"
        return self.conn.execute(q).fetchone()[0]

    def recent_alerts(self, limit=10, offset=0, include_dismissed=False):
        """Newest alerts. Dismissed ones are hidden by default but still exist."""
        q = "SELECT * FROM alerts"
        if not include_dismissed:
            q += " WHERE COALESCE(dismissed,0)=0"
        q += " ORDER BY alert_id DESC LIMIT ? OFFSET ?"
        rows = self.conn.execute(q, (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def dismiss_alert(self, alert_id):
        """Acknowledge one alert: hide it from the UI, keep the row."""
        self.conn.execute(
            "UPDATE alerts SET dismissed=1, dismissed_at=? WHERE alert_id=?",
            (_now(), alert_id))
        self.conn.commit()

    def restore_alert(self, alert_id):
        self.conn.execute(
            "UPDATE alerts SET dismissed=0, dismissed_at=NULL WHERE alert_id=?",
            (alert_id,))
        self.conn.commit()

    def dismiss_all_alerts(self):
        """'Clear alerts' = dismiss every visible alert. Nothing is deleted."""
        cur = self.conn.execute(
            "UPDATE alerts SET dismissed=1, dismissed_at=? "
            "WHERE COALESCE(dismissed,0)=0", (_now(),))
        self.conn.commit()
        return cur.rowcount

    def close(self):
        self.conn.close()