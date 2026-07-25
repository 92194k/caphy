"""
cloud_alerts.py — publish alert metadata to Firestore so the phone can read
its alerts from ANYWHERE (mobile data included), not just over the laptop's
local Wi-Fi address.

Background: FirebaseUploader already pushes alert photos/videos to Firebase
Storage, but nothing wrote the alert's METADATA (tier, distance, time,
camera) anywhere the phone could query. So the phone's alert list only ever
worked by hitting the laptop's local /api/alerts endpoint - useless off the
LAN. This module closes that gap: on every alert, it uploads the snapshot to
Storage, mints a long-lived signed URL for it, and writes a small metadata
doc to the Firestore `alerts` collection, scoped by owner_uid so
firestore.rules lets only that account read it.

Everything here is best-effort and wrapped so a cloud failure (offline, no
bucket configured, etc.) never affects local detection/alerts/siren.
"""

import os
from datetime import timedelta

import firebase_admin
from firebase_admin import firestore, storage

import config

try:
    firebase_admin.get_app()
except ValueError:
    try:
        from firebase_auth import init_firebase
        init_firebase()
    except Exception as e:
        print(f"[CAPHY CloudAlerts] Firebase not initialized: {e}")


def _db():
    from storage.device_registry import _get_firestore
    return _get_firestore()


def _upload_snapshot(owner_uid, device_id, local_path):
    """Uploads the snapshot to Storage and returns a signed download URL (or
    None). The service-account key CAPHY already uses can sign v4 URLs
    offline, so no extra IAM setup is needed."""
    bucket_name = getattr(config, "FIREBASE_BUCKET", None)
    if not bucket_name or not local_path or not os.path.exists(local_path):
        return None
    try:
        cloud_path = f"{owner_uid}/{device_id}/alerts/{os.path.basename(local_path)}"
        bucket = storage.bucket(bucket_name)
        blob = bucket.blob(cloud_path)
        blob.upload_from_filename(local_path)
        # 7 days is the MAX a v4 signed URL is allowed to live (Google caps
        # it at 604800s); asking for more fails the whole upload. Alerts are
        # pruned by retention cleanup well within that window anyway.
        return blob.generate_signed_url(expiration=timedelta(days=7), version="v4")
    except Exception as e:
        print(f"[CAPHY CloudAlerts] snapshot upload/sign failed: {e}")
        return None


def publish_alert(owner_uid, device_id, alert_json, snapshot_local_path=None):
    """
    Writes one alert's metadata to Firestore so the phone can read it from
    anywhere. `alert_json` is the same dict web/server.py's _alert_json()
    builds. No-ops (returns False) if there's no owner yet - an unowned
    laptop has no account to scope the alert to.
    """
    if not owner_uid:
        return False
    try:
        snapshot_url = _upload_snapshot(owner_uid, device_id, snapshot_local_path)
        doc_id = f"{device_id}_{alert_json.get('id')}"
        _db().collection("alerts").document(doc_id).set({
            "owner_uid": owner_uid,
            "device_id": device_id,
            "alert_id": alert_json.get("id"),
            "tier": alert_json.get("tier"),
            "event": alert_json.get("event"),
            "distance_m": alert_json.get("distance_m"),
            "confidence": alert_json.get("confidence"),
            "camera": alert_json.get("camera"),
            "timestamp": alert_json.get("timestamp"),
            "snapshot_url": snapshot_url,
            "has_video": alert_json.get("has_video", False),
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        return True
    except Exception as e:
        print(f"[CAPHY CloudAlerts] publish failed: {e}")
        return False
