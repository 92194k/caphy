"""
firebase_push.py — Firebase Cloud Messaging (FCM) for CAPHY.

Sends push notifications to paired phones via FCM, scoped per user + device.

A phone can only receive alerts from the ONE device it's paired to. This
ensures User A's phone never gets User B's alerts even if they know a device_id.

Usage:
    from storage.firebase_push import FirebasePushSender
    sender = FirebasePushSender(user_uid, device_id)
    sender.send_alert(title, body, snapshot_path, tier)
"""

import firebase_admin
from firebase_admin import messaging
import config
import json

# Initialize Firebase (should be done by firebase_auth.py first)
try:
    firebase_admin.get_app()
except ValueError:
    try:
        from firebase_auth import init_firebase
        init_firebase()
    except Exception as e:
        print(f"[Firebase Push] Warning: Firebase not initialized: {e}")


class FirebasePushSender:
    """Send FCM notifications to paired devices."""

    def __init__(self, user_uid, device_id):
        """
        Args:
            user_uid: Firebase UID (identifies the user account)
            device_id: CAPHY device_id (identifies the laptop)
        """
        self.user_uid = user_uid
        self.device_id = device_id

    def send_alert(self, title, body, data=None, tier=None):
        """
        Send a push notification to all devices paired with this user+device combo.

        Args:
            title: Notification title (e.g., "Motion Detected")
            body: Notification body (e.g., "Person at front door")
            data: Dict of extra data (e.g., {"tier": "3", "distance": "1.5m"})
            tier: Threat tier (1, 2, or 3) — used to determine priority

        Returns:
            True if sent successfully, False otherwise
        """
        if not data:
            data = {}

        # Build the notification
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "user_uid": self.user_uid,
                "device_id": self.device_id,
                "tier": str(tier or 0),
                **data  # add any extra fields
            },
            # Topic: scoped to user_uid + device_id, so only the right phone gets it
            topic=self._get_topic_name(),
        )

        try:
            response = messaging.send(message)
            print(f"[Firebase Push] Sent: {response}")
            return True
        except Exception as e:
            print(f"[Firebase Push] Failed to send: {e}")
            return False

    def subscribe_device_to_topic(self, registration_token):
        """
        Subscribe a phone (by FCM token) to receive alerts from this device.

        Called on the phone when it pairs with a laptop.

        Args:
            registration_token: FCM token from the phone (from Flutter app)

        Returns:
            True if successful, False otherwise
        """
        topic = self._get_topic_name()

        try:
            # firebase-admin >=6 exposes subscribe_to_topic /
            # unsubscribe_to_topic; the old make_topic_management_request was
            # removed, which is exactly the AttributeError this replaces.
            messaging.subscribe_to_topic([registration_token], topic)
            print(f"[Firebase Push] Subscribed token to {topic}")
            return True
        except Exception as e:
            print(f"[Firebase Push] Subscription failed: {e}")
            return False

    def unsubscribe_device_from_topic(self, registration_token):
        """
        Unsubscribe a phone from this device's alerts (e.g., unpair).

        Args:
            registration_token: FCM token from the phone

        Returns:
            True if successful, False otherwise
        """
        topic = self._get_topic_name()

        try:
            messaging.unsubscribe_from_topic([registration_token], topic)
            print(f"[Firebase Push] Unsubscribed token from {topic}")
            return True
        except Exception as e:
            print(f"[Firebase Push] Unsubscribe failed: {e}")
            return False

    def _get_topic_name(self):
        """Generate the FCM topic name (scoped to user + device)."""
        # FCM topic names must be lowercase alphanumeric + hyphens
        # Replace underscores and colons with hyphens
        clean_uid = self.user_uid.replace("_", "-").replace(":", "-").lower()
        clean_device = self.device_id.replace("_", "-").replace(":", "-").lower()
        return f"caphy-{clean_uid}-{clean_device}"


# ===== API Endpoint Helper =====

def get_push_sender_from_session(session):
    """
    Create a FirebasePushSender from current session.

    Args:
        session: Flask session dict

    Returns:
        FirebasePushSender instance, or None if not authenticated
    """
    user_uid = session.get("user_uid")
    try:
        from identity import get_device_identity
        device_id = get_device_identity()["device_id"]
    except Exception:
        device_id = None

    if not user_uid or not device_id:
        return None

    return FirebasePushSender(user_uid, device_id)


def get_push_sender_for_alert(user_uid, device_id):
    """
    Create a FirebasePushSender directly from an alert's owner fields
    (storage/alerts.py already stamps user_uid + device_id on every alert -
    see B4). Used from the background detection thread, which has no Flask
    session to read.

    Returns None if either value is missing (e.g. alert fired before anyone
    signed in yet) - caller should just skip sending a push in that case.
    """
    if not user_uid or not device_id:
        return None
    return FirebasePushSender(user_uid, device_id)
