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
            created_at    TEXT);

        CREATE TABLE IF NOT EXISTS alerts(
            alert_id      INTEGER PRIMARY KEY AUTOINCREMENT,
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

    def add_alert(self, tier, distance_m, confidence, snapshot_path, video_path, camera=None):
        """Save one confirmed threat. Returns the new alert_id."""
        cur = self.conn.execute(
            "INSERT INTO alerts(tier, distance_m, confidence, snapshot_path, video_path, camera, synced, timestamp) "
            "VALUES(?,?,?,?,?,?,0,?)",
            (tier, distance_m, confidence, snapshot_path, video_path, camera, _now()))
        self.conn.commit()
        return cur.lastrowid

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

    def count_alerts(self):
        return self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    def recent_alerts(self, limit=10):
        rows = self.conn.execute(
            "SELECT * FROM alerts ORDER BY alert_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()