"""
clear_local_users.py — wipes ONLY the local users table mirror in caphy.db.

This does NOT touch Firebase Authentication (the real accounts) - delete
those separately at:
    https://console.firebase.google.com/project/caphy-c6b77/authentication/users

This also does NOT touch alerts, captures, or any detection history -
only the local `users` table, which just caches email/uid for display
(e.g. the "Signed in as" line in Settings). Safe to run any time; the
table repopulates itself automatically the next time someone signs in.

Usage:
    python clear_local_users.py
"""

import config
from storage.database import Database

db = Database(config.DB_PATH)
before = db.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
db.conn.execute("DELETE FROM users")
db.conn.commit()
after = db.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
db.close()

print(f"Cleared local users table: {before} row(s) removed, {after} remain.")
print("Reminder: this did NOT delete the real accounts in Firebase Auth.")
print("Delete those at: https://console.firebase.google.com/project/caphy-c6b77/authentication/users")
