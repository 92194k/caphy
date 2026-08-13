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
    return _upload_media(owner_uid, device_id, local_path, "alerts")


def _upload_video(owner_uid, device_id, local_path):
    """Same as _upload_snapshot but for the recorded video clip, if any.
    Kept as a separate function name (even though it's a thin wrapper) so
    call sites read clearly, and so recordings could get their own storage
    folder/retention policy later without touching the snapshot path."""
    return _upload_media(owner_uid, device_id, local_path, "recordings")


def _upload_media(owner_uid, device_id, local_path, subfolder):
    bucket_name = getattr(config, "FIREBASE_BUCKET", None)
    if not bucket_name or not local_path or not os.path.exists(local_path):
        return None
    try:
        cloud_path = f"{owner_uid}/{device_id}/{subfolder}/{os.path.basename(local_path)}"
        bucket = storage.bucket(bucket_name)
        blob = bucket.blob(cloud_path)
        blob.upload_from_filename(local_path)
        # 7 days is the MAX a v4 signed URL is allowed to live (Google caps
        # it at 604800s); asking for more fails the whole upload. Alerts are
        # pruned by retention cleanup well within that window anyway.
        return blob.generate_signed_url(expiration=timedelta(days=7), version="v4")
    except Exception as e:
        print(f"[CAPHY CloudAlerts] {subfolder} upload/sign failed: {e}")
        return None


def delete_alert(device_id, alert_id):
    """Removes one alert's cloud copy (Firestore metadata doc) so a phone
    reading off-LAN stops seeing it too.

    This was MISSING entirely - web/server.py's /api/alert/<id>/delete only
    ever ran `DELETE FROM alerts` against the laptop's local SQLite DB. That
    deletes the alert everywhere the laptop itself looks (its own dashboard,
    same-LAN phone requests), but the phone's cross-network fallback reads
    from THIS Firestore collection instead (see api.dart's
    _alertsFromCloud()), which nothing ever cleaned up - so a "deleted"
    alert kept reappearing forever for anyone reading it off-LAN. Uses the
    same deterministic doc_id publish_alert() writes with
    (f"{device_id}_{alert_id}"), so no extra lookup/query is needed to find
    the right doc. Best-effort, like the rest of this module - a failure
    here must never block the local delete from succeeding.
    """
    try:
        doc_id = f"{device_id}_{alert_id}"
        _db().collection("alerts").document(doc_id).delete()
        return True
    except Exception as e:
        print(f"[CAPHY CloudAlerts] delete failed for alert {alert_id}: {e}")
        return False


def delete_alerts(device_id, alert_ids):
    """Same as delete_alert() but for several ids at once (bulk delete) -
    one batched Firestore commit instead of N sequential deletes."""
    if not alert_ids:
        return
    try:
        batch = _db().batch()
        for aid in alert_ids:
            batch.delete(_db().collection("alerts").document(f"{device_id}_{aid}"))
        batch.commit()
    except Exception as e:
        print(f"[CAPHY CloudAlerts] batch delete failed: {e}")


def publish_alert(owner_uid, device_id, alert_json, snapshot_local_path=None,
                   video_local_path=None):
    """
    Writes one alert's metadata to Firestore so the phone can read it from
    anywhere. `alert_json` is the same dict web/server.py's _alert_json()
    builds. No-ops (returns False) if there's no owner yet - an unowned
    laptop has no account to scope the alert to.

    video_local_path: optional - the recorded clip for this alert (Tier 2/3
    alerts may have one). Previously only the snapshot was ever uploaded
    here, so recordings never made it to the cloud even after the retry
    sync marked the alert as "synced" - the alert row/snapshot would sync
    but the video stayed laptop-only forever. Now both are uploaded
    together in the same publish call, so a synced=1 alert really does mean
    everything about it (metadata, snapshot, AND video if present) reached
    the cloud, matching what the phone's "has_video" flag implies.
    """
    if not owner_uid:
        return False
    try:
        snapshot_url = _upload_snapshot(owner_uid, device_id, snapshot_local_path)
        video_url = _upload_video(owner_uid, device_id, video_local_path)
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
            "video_url": video_url,
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        return True
    except Exception as e:
        print(f"[CAPHY CloudAlerts] publish failed: {e}")
        return False
