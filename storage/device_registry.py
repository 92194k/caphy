"""
device_registry.py — CAPHY cloud device registry, pairing, and remote command queue.

This is the piece that turns CAPHY from "type the laptop's LAN IP into the
phone" into a real product: a centralized, free (Firestore free tier)
source of truth for "which desktop installations does this account own,
and how do I reach them right now."

Firestore layout:

    devices/{device_id}
        owner_uid       str   — Firebase UID that owns this desktop install
        hostname        str
        device_secret   str   — proves THIS process is the real owner when
                                 writing heartbeats (never sent to phones)
        created_at      ts
        last_seen       ts    — updated by the desktop's heartbeat thread
        last_lan_ip     str   — best-effort LAN address, so a phone on the
        last_lan_port   int     same WiFi can skip the cloud relay entirely
        online          bool  — derived: last_seen within a short window

    pairing_codes/{code}
        device_id       str
        created_at      ts
        expires_at      ts    — short-lived, single-use
        claimed_by      str|None

    commands/{device_id}/queue/{command_id}
        type            str   — "arm" | "disarm" | "snapshot" | ...
        args            dict
        requested_by    str   — uid of the phone that asked
        created_at      ts
        status          str   — "pending" | "done" | "error"
        result          dict|None

Everything here is scoped by owner_uid at write AND read time — a phone can
only ever see/command devices whose devices/{id}.owner_uid equals its own
Firebase UID (enforced both here and in firestore.rules, defense in depth).

This module is used from BOTH sides:
  - the desktop (web/server.py, desktop_launcher.py) — registers itself,
    heartbeats, listens for commands
  - nothing on the Flask side serves this to the phone directly; the phone
    talks to Firestore directly via the Flutter SDK using the SAME rules,
    so an unreachable desktop still lets the phone read pairing/onboarding
    state (that's the whole point — auth/pairing must not depend on the
    laptop being on).
"""

import time
import secrets
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import firestore

_db = None


def _get_firestore():
    """Lazy Firestore client, reusing whatever firebase_admin App is already
    initialized (firebase_auth.py / firebase_push.py init it first for auth
    and push — Firestore is just another API on the same credentials)."""
    global _db
    if _db is not None:
        return _db

    try:
        firebase_admin.get_app()
    except ValueError:
        from firebase_auth import init_firebase
        init_firebase()

    _db = firestore.client()
    return _db


PAIRING_TTL_SEC = 300          # QR code lifetime
ONLINE_WINDOW_SEC = 45         # heartbeat must land within this to count "online"


# ------------------------------------------------------------- device registration

def register_device(device_id: str, device_secret: str, hostname: str, owner_uid: str = None):
    """
    Called once at desktop startup (and safe to call every startup — it's
    an upsert). Does NOT require owner_uid: a freshly installed desktop
    that hasn't logged in yet still gets a row, so it can display "Ready to
    Pair" once someone signs in and pairing writes owner_uid in.

    If owner_uid IS known (desktop already has a logged-in session), pass
    it so the device is immediately claimed by that account — this is the
    "Register this Desktop" step in the desktop setup flow, distinct from
    phone-side QR pairing (a laptop doesn't need a QR code to claim
    itself; it already proved who it is via its own Firebase login).
    """
    db = _get_firestore()
    ref = db.collection("devices").document(device_id)
    snap = ref.get()

    now = firestore.SERVER_TIMESTAMP
    if snap.exists:
        update = {
            "hostname": hostname,
            "last_seen": now,
        }
        if owner_uid and not snap.to_dict().get("owner_uid"):
            update["owner_uid"] = owner_uid
        ref.update(update)
    else:
        ref.set({
            "owner_uid": owner_uid,
            "hostname": hostname,
            "device_secret": device_secret,
            "created_at": now,
            "last_seen": now,
            "last_lan_ip": None,
            "last_lan_port": None,
        })


def claim_device(device_id: str, owner_uid: str):
    """
    Force this device to be owned by owner_uid, OVERWRITING any previous
    owner. This is what scan-to-connect calls: whoever is signed in on the
    laptop and shows the QR is, by definition, the legitimate owner (the
    laptop is the root of trust). Without this, a device claimed by an
    earlier test account would keep rejecting the new account's phone -
    every cloud feature (live video, remote commands) checks
    devices/{id}.owner_uid against the phone's uid, so a stale owner breaks
    all of them with PERMISSION_DENIED.
    """
    db = _get_firestore()
    ref = db.collection("devices").document(device_id)
    ref.set({"owner_uid": owner_uid, "last_seen": firestore.SERVER_TIMESTAMP},
            merge=True)


def heartbeat(device_id: str, lan_ip: str = None, lan_port: int = None,
              owner_uid: str = None, state: dict = None, turn_servers: list = None):
    """Called periodically (every ~20s) by the desktop so the phone can
    tell 'is my laptop online right now' and knows the current LAN address
    to try for the fast path. Also re-claims owner_uid if it was set after
    initial registration (e.g. user just logged in on the desktop).

    `state` (optional) mirrors the laptop's current armed/camera/emergency
    status into the device doc so a phone that's OFF the LAN (mobile data /
    traveling) can still show the real system state over the internet -
    without this the remote view would have to guess, and arm/disarm
    toggles would display the wrong position. It's just a small status
    snapshot, never a source of truth: the laptop's own local state remains
    authoritative, this is only its last published copy."""
    db = _get_firestore()
    ref = db.collection("devices").document(device_id)
    update = {"last_seen": firestore.SERVER_TIMESTAMP}
    if lan_ip:
        update["last_lan_ip"] = lan_ip
    if lan_port:
        update["last_lan_port"] = lan_port
    if owner_uid:
        update["owner_uid"] = owner_uid
    if state:
        update["state"] = state
    if turn_servers is not None:
        # Published so the phone can build its peer connection WITH relay
        # servers from the start (see webrtc_call.dart) - critical for
        # mobile-data calls that can't connect any other way.
        update["turn_servers"] = turn_servers
    try:
        ref.update(update)
    except Exception:
        # Document might not exist yet on a very first heartbeat race —
        # fall back to a full set so heartbeats never crash the app.
        ref.set(update, merge=True)


def is_online(device_doc: dict) -> bool:
    last_seen = device_doc.get("last_seen")
    if not last_seen:
        return False
    try:
        age = (datetime.now(timezone.utc) - last_seen).total_seconds()
        return age <= ONLINE_WINDOW_SEC
    except Exception:
        return False


def devices_for_owner(owner_uid: str) -> list:
    """All desktop installs this account owns (a user can have more than
    one CAPHY laptop)."""
    db = _get_firestore()
    q = db.collection("devices").where("owner_uid", "==", owner_uid).stream()
    out = []
    for doc in q:
        d = doc.to_dict()
        d["device_id"] = doc.id
        d["online"] = is_online(d)
        d.pop("device_secret", None)   # never leave the room, not even toward the owner's own phone
        out.append(d)
    return out


def get_device(device_id: str) -> dict:
    db = _get_firestore()
    snap = db.collection("devices").document(device_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d["device_id"] = device_id
    d["online"] = is_online(d)
    d.pop("device_secret", None)
    return d


# ------------------------------------------------------------- QR pairing

def create_pairing_code(device_id: str) -> dict:
    """
    Desktop (web Settings → Connect Device) calls this to mint a fresh QR
    payload. The code lives in Firestore (not just process memory like the
    old LAN-only version) so pairing works even if the phone can't reach
    the laptop's LAN yet — the phone confirms pairing by writing to
    Firestore directly, not by calling the laptop.
    """
    db = _get_firestore()
    code = secrets.token_hex(8)
    db.collection("pairing_codes").document(code).set({
        "device_id": device_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + PAIRING_TTL_SEC,
        "claimed_by": None,
    })
    return {"code": code, "device_id": device_id, "expires_in": PAIRING_TTL_SEC}


def confirm_pairing(code: str, phone_uid: str) -> dict:
    """
    Phone calls this right after scanning the QR. Runs as a transaction so
    two phones racing on the same short-lived code can't both "win" it.

    On success, permanently sets devices/{device_id}.owner_uid = phone_uid
    (first pairing claims the device) OR, if the device is already owned
    by this same account, just confirms it (re-pairing a second phone to
    an account you already own is allowed; claiming someone ELSE's device
    is not).
    """
    db = _get_firestore()
    code_ref = db.collection("pairing_codes").document(code)

    @firestore.transactional
    def _txn(transaction):
        snap = code_ref.get(transaction=transaction)
        if not snap.exists:
            return {"success": False, "error": "Code expired or invalid"}
        data = snap.to_dict()
        if data.get("claimed_by"):
            return {"success": False, "error": "Code already used"}
        if data.get("expires_at", 0) < time.time():
            return {"success": False, "error": "Code expired"}

        device_id = data["device_id"]
        device_ref = db.collection("devices").document(device_id)
        device_snap = device_ref.get(transaction=transaction)
        if not device_snap.exists:
            return {"success": False, "error": "Desktop not found"}

        device_data = device_snap.to_dict()
        current_owner = device_data.get("owner_uid")
        if current_owner and current_owner != phone_uid:
            # This desktop already belongs to a DIFFERENT account. This is
            # the hard security boundary: a QR code alone can never move a
            # device between accounts.
            return {"success": False, "error": "This CAPHY desktop is already linked to another account"}

        transaction.update(device_ref, {"owner_uid": phone_uid})
        transaction.update(code_ref, {"claimed_by": phone_uid})
        return {"success": True, "device_id": device_id}

    transaction = db.transaction()
    result = _txn(transaction)
    return result


# ------------------------------------------------------- remote pairing confirmation
#
# Lets a phone redeem a QR pairing code without ever reaching the laptop's
# LAN address - the phone writes a request here (see firestore.rules:
# pairing_confirm_requests/{id} - any signed-in phone may create ONE,
# stamped with its own uid), and each desktop's poll loop
# (desktop_launcher.py's run_remote_command_listener) checks whether any
# pending request's code belongs to ITS device_id before touching it, so
# a laptop only ever processes requests actually meant for it.

def pending_pairing_requests_for_device(device_id: str) -> list:
    """
    Desktop's poll loop calls this every few seconds. Filters down to
    requests whose pairing code was minted for THIS device_id - a laptop
    must never blindly process every pending request, since those belong
    to whichever desktop each phone is actually trying to pair with.
    """
    db = _get_firestore()
    q = (db.collection("pairing_confirm_requests")
         .where("status", "==", "pending").stream())
    out = []
    for doc in q:
        d = doc.to_dict()
        code = d.get("code", "")
        code_snap = db.collection("pairing_codes").document(code).get()
        if not code_snap.exists:
            continue
        if code_snap.to_dict().get("device_id") != device_id:
            continue
        d["id"] = doc.id
        out.append(d)
    return out


def complete_pairing_request(request_id: str, result: dict, ok: bool = True):
    db = _get_firestore()
    ref = db.collection("pairing_confirm_requests").document(request_id)
    ref.update({
        "status": "done" if ok else "error",
        "result": result,
    })


# -------------------------------------------------- scan-to-connect link tokens
#
# Powers the "no login screen, just scan the laptop's QR" flow. The laptop
# (already signed in) mints a Firebase CUSTOM TOKEN for its own account and
# stashes it here under an unguessable nonce; the QR carries only that nonce.
# The phone - which is NOT signed in yet, that's the whole point - reads this
# doc by nonce (firestore.rules allows an unauthenticated read of exactly
# this collection, because knowing the 32-byte random nonce IS the
# capability), calls signInWithCustomToken(), and is now authenticated as
# the SAME account with zero typing. Short TTL + the value being a one-shot
# sign-in credential is why it must be treated like a password and expire
# fast.

LINK_TOKEN_TTL_SEC = 180


def store_link_token(nonce: str, custom_token: str, device_id: str, owner_uid: str):
    db = _get_firestore()
    db.collection("phone_link_tokens").document(nonce).set({
        "custom_token": custom_token,
        "device_id": device_id,
        "owner_uid": owner_uid,
        "created_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + LINK_TOKEN_TTL_SEC,
    })


def consume_link_token(nonce: str) -> dict:
    """Best-effort cleanup: the phone calls this (it can, it's now signed in)
    right after using the token, so the one-shot credential doesn't linger in
    Firestore any longer than it must. Expiry is the real backstop; this just
    tidies up promptly on the happy path."""
    db = _get_firestore()
    try:
        db.collection("phone_link_tokens").document(nonce).delete()
    except Exception:
        pass


# ------------------------------------------------------------- remote command queue
#
# Used only when the phone can't reach the desktop's LAN address directly
# (different network / no local WiFi). On the same network, the phone
# should always prefer talking straight to the laptop's Flask server —
# faster, and works even if the laptop is offline from the internet
# entirely, which is core to CAPHY staying useful without connectivity.

def push_command(device_id: str, requested_by_uid: str, cmd_type: str, args: dict = None) -> str:
    """Phone (when remote) enqueues a command for the desktop to pick up
    on its next poll. Returns the command_id so the phone can watch for
    the result doc."""
    db = _get_firestore()
    ref = db.collection("commands").document(device_id).collection("queue").document()
    ref.set({
        "type": cmd_type,
        "args": args or {},
        "requested_by": requested_by_uid,
        "created_at": firestore.SERVER_TIMESTAMP,
        "status": "pending",
        "result": None,
    })
    return ref.id


def pending_commands(device_id: str) -> list:
    """Desktop's poll loop calls this every few seconds."""
    db = _get_firestore()
    q = (db.collection("commands").document(device_id).collection("queue")
         .where("status", "==", "pending").stream())
    out = []
    for doc in q:
        d = doc.to_dict()
        d["id"] = doc.id
        out.append(d)
    return out


def complete_command(device_id: str, command_id: str, result: dict, ok: bool = True):
    db = _get_firestore()
    ref = db.collection("commands").document(device_id).collection("queue").document(command_id)
    ref.update({
        "status": "done" if ok else "error",
        "result": result,
    })


def claim_command(device_id: str, command_id: str) -> bool:
    """Atomically flips a command from 'pending' to 'executing'. Returns True
    if THIS call won the claim, False if it was already claimed/finished by
    someone else (a duplicate snapshot event, a reconnect replaying the same
    doc, etc). Must be called before executing a command, and is what makes
    the realtime listener safe to fire more than once for the same doc -
    Firestore listeners are explicitly allowed to redeliver events (e.g. on
    reconnect), so "receive the event" and "execute the command" must not be
    the same step."""
    db = _get_firestore()
    ref = db.collection("commands").document(device_id).collection("queue").document(command_id)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return False
        if snap.to_dict().get("status") != "pending":
            return False
        transaction.update(ref, {
            "status": "executing",
            "claimed_at": firestore.SERVER_TIMESTAMP,
        })
        return True

    transaction = db.transaction()
    try:
        return _txn(transaction)
    except Exception:
        return False


def watch_pending_commands(device_id: str, on_command):
    """Realtime replacement for pending_commands()'s poll loop. Attaches a
    Firestore on_snapshot listener to the pending-commands query and calls
    on_command(cmd_dict) for each newly-pending doc as soon as Firestore
    pushes the change - no fixed poll interval, so the laptop reacts within
    the underlying Firestore watch stream's own latency (typically well
    under a second) instead of waiting for the next 1s tick.

    Returns the Watch object; call .unsubscribe() on it to stop listening.

    NOTE: on_snapshot delivers the FULL current result set on every change,
    not just the diff, and can redeliver documents (e.g. right after
    reconnecting). on_command is expected to call claim_command() itself
    before doing any real work, so redelivery is a no-op rather than a
    double-execution."""
    db = _get_firestore()
    q = (db.collection("commands").document(device_id).collection("queue")
         .where("status", "==", "pending"))

    def _cb(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name in ("ADDED", "MODIFIED"):
                doc = change.document
                d = doc.to_dict()
                if d is None:
                    continue
                d["id"] = doc.id
                try:
                    on_command(d)
                except Exception as e:
                    print(f"[CAPHY] Command listener callback error: {e}")

    return q.on_snapshot(_cb)
