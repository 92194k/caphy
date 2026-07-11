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

    def add_alert(self, tier, distance_m, confidence, snapshot_path, video_path):
        """Save one confirmed threat. Returns the new alert_id."""
        cur = self.conn.execute(
            "INSERT INTO alerts(tier, distance_m, confidence, snapshot_path, video_path, synced, timestamp) "
            "VALUES(?,?,?,?,?,0,?)",
            (tier, distance_m, confidence, snapshot_path, video_path, _now()))
        self.conn.commit()
        return cur.lastrowid

    def add_threat_log(self, alert_id, motion_area, bbox_height, est_distance, tier):
        """Save the detection detail behind an alert (used later for calibration)."""
        self.conn.execute(
            "INSERT INTO threat_logs(alert_id, motion_area, bbox_height, est_distance, tier, timestamp) "
            "VALUES(?,?,?,?,?,?)",
            (alert_id, motion_area, bbox_height, est_distance, tier, _now()))
        self.conn.commit()

    def count_alerts(self):
        return self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    def recent_alerts(self, limit=10):
        rows = self.conn.execute(
            "SELECT * FROM alerts ORDER BY alert_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()