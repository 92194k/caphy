"""
password_reset.py — Email PIN flows for CAPHY: forgot-password reset AND
signup email verification. Both send a themed 6-digit PIN via Gmail SMTP.

---- Forgot password ----
  1. User enters their email on the "Forgot password?" page.
  2. request_reset_pin(email) looks up the Firebase account, generates a
     6-digit PIN, stores its SHA-256 hash (never the raw PIN) with a 10
     minute expiry, and emails it.
  3. User enters the PIN + new password on the next page.
  4. verify_and_reset(email, pin, new_password) checks the PIN and, if
     correct, updates the Firebase Auth password via the Admin SDK.
  This intentionally does NOT reveal whether an email exists (always
  "succeeds" from the caller's point of view) - the route shows the same
  generic message either way, to avoid leaking which emails have accounts.

---- Signup verification ----
  1. User fills the signup form (name/email/password).
  2. request_signup_pin(email, name, password) checks the email isn't
     already registered, stashes the pending signup + PIN hash, and emails
     the PIN. No Firebase account exists yet.
  3. User enters the PIN.
  4. verify_signup_pin(email, pin) checks it and, only if correct, creates
     the real Firebase Auth account - so an unverified email can never
     result in a live account.

Security notes (both flows):
  - PINs are never stored or logged in plaintext, only their SHA-256 hash.
  - A PIN expires after 10 minutes and can only be used once.
  - Up to 5 wrong attempts are allowed per PIN before it's locked out,
    to blunt brute-forcing a 6-digit code.
"""

import hashlib
import os
import random
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

import secrets_config

PIN_TTL_MINUTES = 10
MAX_ATTEMPTS = 5

# Change-password verification (Settings, while already signed in) uses its
# own shorter expiry per spec - 5 minutes instead of the 10 used by the
# forgot-password/signup flows above. Same attempt cap.
CHANGE_PW_PIN_TTL_MINUTES = 5

# The real CAPHY eye-shield logo (same artwork used for the app icon/
# favicon), embedded in PIN emails as an inline attachment (cid:) instead
# of the plain "C" letter badge - email clients don't run CSS/SVG
# animations, so this is a static PNG of the same mark shown elsewhere.
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "web", "static", "img", "caphy_icon_256.png")
_LOGO_CID = "caphy_logo_icon"


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def _generate_pin() -> str:
    return f"{random.randint(0, 999999):06d}"


def _pin_email_html(pin: str, heading: str, body_line: str) -> str:
    """Themed HTML body shared by both the password-reset and signup-
    verification emails - dark/purple CAPHY palette matching the login and
    forgot-password web pages, PIN shown as boxed monospace digits."""
    pin_digits = "".join(
        f'<span style="display:inline-block;width:38px;height:46px;line-height:46px;'
        f'margin:0 4px;border-radius:8px;background:#15152f;border:1px solid rgba(140,120,255,.35);'
        f'color:#e7e6f5;font-size:22px;font-weight:700;font-family:Consolas,Menlo,monospace;'
        f'text-align:center">{d}</span>'
        for d in pin
    )
    return f"""\
<div style="background:#07060f;padding:40px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
  <div style="max-width:420px;margin:0 auto;background:#141228;border:1px solid rgba(140,120,255,.18);
      border-radius:16px;padding:32px;color:#e7e6f5">
    <div style="text-align:center;margin-bottom:22px">
      <img src="cid:{_LOGO_CID}" width="56" height="56" alt="CAPHY"
        style="display:inline-block;width:56px;height:56px;border-radius:14px">
      <div style="margin-top:10px;font-size:15px;font-weight:700;letter-spacing:.5px;color:#e7e6f5">CAPHY</div>
      <div style="font-size:11.5px;color:#9a97b8;letter-spacing:1px;text-transform:uppercase">
        AI-Intelligent Security System</div>
    </div>
    <h1 style="font-size:18px;margin:0 0 8px;text-align:center;color:#e7e6f5">{heading}</h1>
    <p style="font-size:13.5px;color:#9a97b8;text-align:center;line-height:1.6;margin:0 0 24px">
      {body_line}
    </p>
    <div style="text-align:center;margin-bottom:22px">{pin_digits}</div>
    <p style="font-size:12.5px;color:#9a97b8;text-align:center;line-height:1.6;margin:0 0 4px">
      Expires in {PIN_TTL_MINUTES} minutes &middot; can only be used once
    </p>
    <p style="font-size:12px;color:#6b6890;text-align:center;line-height:1.6;margin-top:20px">
      If you didn't request this, you can safely ignore this email.
    </p>
  </div>
</div>"""


def _send_pin_email(to_email: str, pin: str, subject: str, heading: str, body_line: str, text_line: str):
    """Sends a themed PIN email via Gmail SMTP (SSL, port 465) using an App
    Password. Raises on failure so the caller can decide how to surface
    it. Shared by password-reset and signup-verification PINs - only the
    subject/copy differs."""
    sender = secrets_config.get_smtp_email()
    app_password = secrets_config.get_smtp_app_password()
    if not sender or not app_password:
        raise RuntimeError("SMTP not configured (missing smtp_email/smtp_app_password)")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"CAPHY Security <{sender}>"
    msg["To"] = to_email

    # Plain-text fallback (shown by clients that can't render HTML).
    msg.set_content(
        f"{text_line}\n\nYour CAPHY PIN is: {pin}\n\n"
        f"This code expires in {PIN_TTL_MINUTES} minutes and can only be used once.\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    msg.add_alternative(_pin_email_html(pin, heading, body_line), subtype="html")

    # Attach the logo PNG inline (Content-ID) to the HTML part just added,
    # so the <img src="cid:..."> in _pin_email_html resolves to it instead
    # of a broken image icon. Best-effort - if the file's missing for some
    # reason, the email still sends fine, just without the logo image.
    try:
        with open(_LOGO_PATH, "rb") as f:
            logo_bytes = f.read()
        html_part = msg.get_payload()[-1]  # the HTML alternative just added
        html_part.add_related(logo_bytes, maintype="image", subtype="png", cid=f"<{_LOGO_CID}>")
    except Exception as e:
        print(f"[CAPHY Auth] Could not attach logo to PIN email (non-fatal): {e}")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(sender, app_password)
        server.send_message(msg)


def request_reset_pin(email: str) -> tuple:
    """
    Looks up the account by email, generates + stores + emails a PIN.

    Returns (sent: bool, debug_reason: str). debug_reason is always a short
    machine-readable string ("no_account", "smtp_error: ...", "ok") so the
    caller/logs can tell exactly why a PIN didn't go out - the UI itself
    should still show a generic message regardless of `sent`, to avoid
    leaking account existence to an attacker.
    """
    import config
    from storage.database import Database

    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        user = fb_auth.get_user_by_email(email)
        uid = user.uid
    except Exception as e:
        print(f"[CAPHY Auth] Reset PIN requested for unknown/unreachable email {email}: {e}")
        return False, f"no_account: {e}"

    pin = _generate_pin()
    now = datetime.now()
    expires = now + timedelta(minutes=PIN_TTL_MINUTES)

    db = Database(config.DB_PATH)
    db.conn.execute(
        "INSERT INTO password_reset_pins (uid, email, pin_hash, attempts, used, created_at, expires_at) "
        "VALUES (?, ?, ?, 0, 0, ?, ?)",
        (uid, email, _hash_pin(pin),
         now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    db.conn.commit()
    db.close()

    try:
        _send_pin_email(
            email, pin,
            subject="Your CAPHY password reset PIN",
            heading="Password Reset PIN",
            body_line="Use this code to reset your CAPHY account password.",
            text_line="Your CAPHY password reset PIN is below.")
        print(f"[CAPHY Auth] Reset PIN emailed to {email}")
        return True, "ok"
    except Exception as e:
        print(f"[CAPHY Auth] Failed to send reset PIN email to {email}: {e}")
        return False, f"smtp_error: {e}"


def verify_and_reset(email: str, pin: str, new_password: str) -> tuple:
    """
    Verifies the most recent unexpired/unused PIN for this email, and if
    it matches, updates the Firebase Auth password.

    Returns (ok: bool, error: str) - error is a short user-facing reason
    when ok is False (e.g. "expired", "incorrect", "too many attempts").
    """
    import config
    from storage.database import Database

    if not new_password or len(new_password) < 6:
        return False, "Password must be at least 6 characters."

    db = Database(config.DB_PATH)
    row = db.conn.execute(
        "SELECT * FROM password_reset_pins WHERE email=? AND used=0 "
        "ORDER BY pin_id DESC LIMIT 1", (email,)).fetchone()

    if not row:
        db.close()
        return False, "No pending reset request. Please request a new PIN."

    if row["attempts"] >= MAX_ATTEMPTS:
        db.close()
        return False, "Too many incorrect attempts. Please request a new PIN."

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now() > expires_at:
        db.close()
        return False, "This PIN has expired. Please request a new one."

    if _hash_pin(pin) != row["pin_hash"]:
        db.conn.execute(
            "UPDATE password_reset_pins SET attempts=attempts+1 WHERE pin_id=?",
            (row["pin_id"],))
        db.conn.commit()
        db.close()
        return False, "Incorrect PIN."

    # PIN correct - update the actual Firebase Auth password.
    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        fb_auth.update_user(row["uid"], password=new_password)
    except Exception as e:
        db.close()
        return False, f"Couldn't update password: {e}"

    db.conn.execute("UPDATE password_reset_pins SET used=1 WHERE pin_id=?", (row["pin_id"],))
    db.conn.commit()
    db.close()
    return True, ""


# ==================== Signup email verification ====================
# No Firebase account exists until the PIN below is confirmed - the
# pending signup (email/name/password) is held in signup_verify_pins only
# long enough to create the real account right after verification.

def request_signup_pin(email: str, name: str, password: str) -> tuple:
    """
    Stores a pending signup + emails a 6-digit confirmation PIN.

    Returns (sent: bool, error: str). Unlike password reset, this DOES
    tell the caller if the email is already a registered account (a
    signup form needs to say "email already in use" - that's not a
    security leak the way password-reset account-existence would be).
    """
    import config
    from storage.database import Database

    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        fb_auth.get_user_by_email(email)
        return False, "An account with this email already exists."
    except Exception:
        pass  # good - no existing account, free to proceed

    pin = _generate_pin()
    now = datetime.now()
    expires = now + timedelta(minutes=PIN_TTL_MINUTES)

    db = Database(config.DB_PATH)
    db.conn.execute(
        "INSERT INTO signup_verify_pins (email, name, password, pin_hash, attempts, used, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
        (email, name, password, _hash_pin(pin),
         now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    db.conn.commit()
    db.close()

    try:
        _send_pin_email(
            email, pin,
            subject="Confirm your CAPHY account",
            heading="Confirm Your Email",
            body_line="Enter this code to finish creating your CAPHY account.",
            text_line="Your CAPHY signup confirmation PIN is below.")
        print(f"[CAPHY Auth] Signup PIN emailed to {email}")
        return True, ""
    except Exception as e:
        print(f"[CAPHY Auth] Failed to send signup PIN email to {email}: {e}")
        return False, "Couldn't send the confirmation email. Please try again."


def verify_signup_pin(email: str, pin: str) -> tuple:
    """
    Verifies the most recent unexpired/unused signup PIN for this email,
    and if it matches, creates the real Firebase Auth account.

    Returns (uid_or_None, error). On success, error is "" and the first
    element is the new Firebase UID - the caller should log the session in
    exactly like /api/auth/signup does today.
    """
    import config
    from storage.database import Database

    db = Database(config.DB_PATH)
    row = db.conn.execute(
        "SELECT * FROM signup_verify_pins WHERE email=? AND used=0 "
        "ORDER BY pin_id DESC LIMIT 1", (email,)).fetchone()

    if not row:
        db.close()
        return None, "No pending signup. Please start over."

    if row["attempts"] >= MAX_ATTEMPTS:
        db.close()
        return None, "Too many incorrect attempts. Please request a new PIN."

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now() > expires_at:
        db.close()
        return None, "This PIN has expired. Please request a new one."

    if _hash_pin(pin) != row["pin_hash"]:
        db.conn.execute(
            "UPDATE signup_verify_pins SET attempts=attempts+1 WHERE pin_id=?",
            (row["pin_id"],))
        db.conn.commit()
        db.close()
        return None, "Incorrect PIN."

    # PIN correct - now actually create the Firebase Auth account.
    try:
        from firebase_auth import FirebaseAuthManager
        mgr = FirebaseAuthManager()
        uid = mgr.create_user_email_password(row["email"], row["password"], display_name=row["name"])
    except Exception as e:
        db.close()
        return None, f"Couldn't create account: {e}"

    # Clear the password now that it's served its purpose - no reason to
    # keep it sitting in this table any longer than necessary.
    db.conn.execute(
        "UPDATE signup_verify_pins SET used=1, password='' WHERE pin_id=?",
        (row["pin_id"],))
    db.conn.commit()
    db.close()
    return uid, ""


# ==================== Change-password email verification ====================
# Used by Settings > User Account > Change Password, for a user who is
# ALREADY signed in and already knows their current password (the route in
# web/server.py checks the current password against Firebase Auth first,
# exactly as before) - this adds a second factor (a PIN sent to their
# registered email) before the new password actually takes effect. Unlike
# the forgot-password flow, this never signs the user out and never touches
# their session - it only gates whether update_user(..., password=...) is
# allowed to run.

def request_change_pin(uid: str, email: str, new_password: str) -> tuple:
    """
    Stores the pending new password (already validated: current password
    correct, new password meets length requirements - both checked by the
    caller before this is ever called) + emails a 6-digit PIN to the
    account's registered email.

    Returns (sent: bool, error: str).
    """
    import config
    from storage.database import Database

    if not new_password or len(new_password) < 6:
        return False, "Password must be at least 6 characters."

    pin = _generate_pin()
    now = datetime.now()
    expires = now + timedelta(minutes=CHANGE_PW_PIN_TTL_MINUTES)

    db = Database(config.DB_PATH)
    db.conn.execute(
        "INSERT INTO change_password_pins (uid, email, new_password, pin_hash, attempts, used, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
        (uid, email, new_password, _hash_pin(pin),
         now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    db.conn.commit()
    db.close()

    try:
        _send_pin_email(
            email, pin,
            subject="Confirm your CAPHY password change",
            heading="Confirm Password Change",
            body_line="Enter this code to confirm your new CAPHY account password.",
            text_line="Your CAPHY password-change confirmation PIN is below.")
        print(f"[CAPHY Auth] Change-password PIN emailed to {email}")
        return True, ""
    except Exception as e:
        print(f"[CAPHY Auth] Failed to send change-password PIN email to {email}: {e}")
        return False, "Couldn't send the verification email. Please try again."


def verify_change_pin(uid: str, pin: str) -> tuple:
    """
    Verifies the most recent unexpired/unused change-password PIN for this
    account, and if it matches, updates the Firebase Auth password to the
    new password staged in request_change_pin().

    Returns (ok: bool, error: str). Never touches the caller's Flask
    session - the user stays signed in exactly as they were, with the same
    session token, before and after a successful change.
    """
    import config
    from storage.database import Database

    db = Database(config.DB_PATH)
    row = db.conn.execute(
        "SELECT * FROM change_password_pins WHERE uid=? AND used=0 "
        "ORDER BY pin_id DESC LIMIT 1", (uid,)).fetchone()

    if not row:
        db.close()
        return False, "No pending password change. Please start over."

    if row["attempts"] >= MAX_ATTEMPTS:
        db.close()
        return False, "Too many incorrect attempts. Please request a new code."

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now() > expires_at:
        db.close()
        return False, "This code has expired. Please request a new one."

    if _hash_pin(pin) != row["pin_hash"]:
        db.conn.execute(
            "UPDATE change_password_pins SET attempts=attempts+1 WHERE pin_id=?",
            (row["pin_id"],))
        db.conn.commit()
        db.close()
        return False, "Incorrect code."

    new_password = row["new_password"]

    # PIN correct - now actually update the Firebase Auth password. This is
    # the ONLY point at which the password actually changes; everything
    # before this (current-password check, PIN request/send) left the real
    # account untouched.
    try:
        from firebase_admin import auth as fb_auth
        from firebase_auth import init_firebase
        init_firebase()
        fb_auth.update_user(uid, password=new_password)
    except Exception as e:
        db.close()
        return False, f"Couldn't update password: {e}"

    # Clear the staged password now that it's served its purpose, same as
    # signup_verify_pins does - no reason to keep a plaintext password
    # sitting in this table any longer than necessary.
    db.conn.execute(
        "UPDATE change_password_pins SET used=1, new_password='' WHERE pin_id=?",
        (row["pin_id"],))
    db.conn.commit()
    db.close()
    return True, ""
