"""
firebase_auth.py — Firebase Authentication for CAPHY.

Handles:
- Email+password signup/login (Firebase Auth native)
- Google Sign-In (OAuth 2.0)
- Token verification (validate Firebase ID tokens)
- User creation/lookup in local database

All users get a Firebase UID (unique, never changes). This UID is used to scope
all data: alerts, logs, settings, device pairing.

Usage:
    from firebase_auth import FirebaseAuthManager
    auth = FirebaseAuthManager()

    # Verify a token (from client)
    uid, email = auth.verify_token(firebase_id_token)

    # Lookup or create user
    user = auth.get_or_create_user(uid, email)
"""

import os
import json
from datetime import datetime
import firebase_admin
from firebase_admin import auth, credentials
import config

# ===== Firebase initialization =====

# Path to service account key (from Firebase Console → Service Accounts).
# Resolve so it's found both from source and when packaged into the .exe:
# prefer the path as-is if it exists (dev), else the bundled copy.
FIREBASE_KEY_PATH = getattr(config, "FIREBASE_KEY_PATH", "firebase_key.json")
if not os.path.exists(FIREBASE_KEY_PATH):
    try:
        from resource_path import resource_path
        _bundled = resource_path(os.path.basename(FIREBASE_KEY_PATH))
        if os.path.exists(_bundled):
            FIREBASE_KEY_PATH = _bundled
    except Exception:
        pass

# Google OAuth Client ID (from Google Cloud Console)
# Non-secret, safe to put in config
GOOGLE_CLIENT_ID = getattr(config, "GOOGLE_CLIENT_ID", "")

_app = None
_initialized = False


def init_firebase():
    """Initialize Firebase Admin SDK (once per process).

    Idempotent AND race-safe: multiple background threads (heartbeat, WebRTC
    listener, push) can call this at nearly the same moment on startup. If
    the default app already exists - either because we initialized it or
    another thread just did - reuse it instead of raising. This is what the
    'default Firebase app already exists' error on startup was."""
    global _app, _initialized
    if _initialized:
        return

    # Someone (another thread, or firebase_push/uploader importing) may have
    # already created the default app - adopt it rather than double-init.
    try:
        _app = firebase_admin.get_app()
        _initialized = True
        return
    except ValueError:
        pass   # not initialized yet - we'll do it below

    if not os.path.exists(FIREBASE_KEY_PATH):
        raise FileNotFoundError(
            f"Firebase key not found at {FIREBASE_KEY_PATH}. "
            f"Download from Firebase Console → Project Settings → Service Accounts."
        )

    try:
        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        _app = firebase_admin.initialize_app(cred)
        _initialized = True
        print(f"[CAPHY Auth] Firebase initialized from {FIREBASE_KEY_PATH}")
    except ValueError:
        # Lost a race - another thread initialized it between our check and
        # now. Adopt the existing app; this is success, not failure.
        _app = firebase_admin.get_app()
        _initialized = True
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Firebase: {e}")


class FirebaseAuthManager:
    """Handle Firebase Auth: email+password + Google Sign-In."""

    def __init__(self):
        init_firebase()
        self.db = None  # lazy-load on first use

    def _get_db(self):
        """Lazy-load database connection (avoid circular imports)."""
        if self.db is None:
            from storage.database import Database
            self.db = Database(config.DB_PATH)
        return self.db

    def verify_token(self, id_token: str) -> tuple:
        """
        Verify a Firebase ID token (from client login).

        Returns: (uid, email) if valid, raises Exception otherwise.

        Args:
            id_token: Firebase ID token from client

        Returns:
            Tuple of (firebase_uid, email_address)
        """
        try:
            decoded = auth.verify_id_token(id_token)
            uid = decoded.get("uid")
            email = decoded.get("email", "")
            return uid, email
        except Exception as e:
            raise ValueError(f"Invalid or expired token: {e}")

    def create_user_email_password(self, email: str, password: str) -> str:
        """
        Create a new Firebase user (email+password).

        Args:
            email: User email
            password: User password (min 6 chars)

        Returns:
            Firebase UID
        """
        try:
            user = auth.create_user(email=email, password=password)
            return user.uid
        except auth.EmailAlreadyExistsError:
            raise ValueError(f"Email {email} already registered")
        except Exception as e:
            raise RuntimeError(f"Failed to create user: {e}")

    def get_or_create_local_user(self, uid: str, email: str) -> dict:
        """
        Lookup or create a local database record for this Firebase user.

        Local database maps Firebase UID → alerts/logs/settings.
        This is kept minimal: just uid + email + created_at.

        Args:
            uid: Firebase UID
            email: Email address (for display)

        Returns:
            Dict with user info
        """
        db = self._get_db()

        # Lookup existing
        row = db.conn.execute(
            "SELECT * FROM users WHERE firebase_uid=?", (uid,)
        ).fetchone()
        if row:
            return dict(row)

        # Create new
        now = str(datetime.now().isoformat(timespec="seconds"))
        db.conn.execute(
            "INSERT INTO users (firebase_uid, email, created_at) VALUES (?, ?, ?)",
            (uid, email, now)
        )
        db.conn.commit()

        row = db.conn.execute(
            "SELECT * FROM users WHERE firebase_uid=?", (uid,)
        ).fetchone()
        return dict(row) if row else {}

    def get_user_by_uid(self, uid: str) -> dict:
        """Lookup a user by Firebase UID."""
        db = self._get_db()
        row = db.conn.execute(
            "SELECT * FROM users WHERE firebase_uid=?", (uid,)
        ).fetchone()
        return dict(row) if row else None


# ===== Usage example (for testing) =====

if __name__ == "__main__":
    from datetime import datetime

    # Initialize
    auth_mgr = FirebaseAuthManager()

    # Example: Create a user (email+password)
    # uid = auth_mgr.create_user_email_password("test@example.com", "password123")
    # print(f"Created user: {uid}")

    # Example: Get or create local record
    # user = auth_mgr.get_or_create_local_user(uid, "test@example.com")
    # print(f"Local user: {user}")

    print("[CAPHY Auth] Firebase auth module loaded (awaiting init)")
