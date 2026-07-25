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
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row   # lets us read columns by name
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

        CREATE TABLE IF NOT EXISTS voice_commands(
            cmd_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            command_text TEXT,
            language     TEXT,
            confirmed    INTEGER,
            timestamp    TEXT);

        CREATE TABLE IF NOT EXISTS settings(
            setting_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            sensitivity  REAL,
            person_conf  REAL,
            armed        INTEGER,
            night_vision INTEGER);
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