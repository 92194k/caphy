"""Firebase integration for alerts, events, and real-time updates.

Sends alerts to phone via FCM, logs events, stores snapshots.
Bridges local AI with remote access.

Install: pip install firebase-admin
Setup:
  1. Create project at https://console.firebase.google.com
  2. Download service account key (JSON)
  3. Place in project root as serviceAccountKey.json
"""
import firebase_admin
from firebase_admin import db, messaging, storage, credentials
import uuid
from datetime import datetime


class FirebaseManager:
    """Send alerts, update state, log events to Firebase."""

    def __init__(self, cred_path="serviceAccountKey.json"):
        """
        Args:
            cred_path: Path to serviceAccountKey.json from Firebase Console
        """
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {
                    'databaseURL': 'https://YOUR_PROJECT.firebaseio.com',
                    'storageBucket': 'YOUR_PROJECT.appspot.com'
                })
            self.db = db.reference()
            print("✓ Firebase connected")
        except FileNotFoundError:
            print(f"⚠️  {cred_path} not found. Firebase disabled.")
            self.db = None
        except Exception as e:
            print(f"⚠️  Firebase error: {e}")
            self.db = None

    def send_alert(self, detection_event):
        """
        Send alert to Firebase (lightweight JSON, NOT video).

        Args:
            detection_event: {
                "threat_level": "HIGH",
                "type": "person",
                "zone": "front_camera",
                "confidence": 0.95,
                "description": "Unknown person, 12s, tripwire crossed",
                "tier": 3,
                "snapshot_url": "gs://...",  # optional
            }

        Returns:
            alert_id (str)
        """
        if not self.db:
            return None

        alert = {
            "timestamp": datetime.now().isoformat(),
            "threat_level": detection_event.get("threat_level", "UNKNOWN"),
            "type": detection_event.get("type", "unknown"),
            "zone": detection_event.get("zone", "unknown"),
            "confidence": detection_event.get("confidence", 0),
            "description": detection_event.get("description", ""),
            "tier": detection_event.get("tier", 0),
            "snapshot_url": detection_event.get("snapshot_url", ""),
        }

        alert_id = str(uuid.uuid4())

        try:
            self.db.child("alerts").child(alert_id).set(alert)
            print(f"✓ Alert sent to Firebase: {alert_id}")

            # Also send FCM push notification
            self.send_fcm_notification(alert)

            return alert_id

        except Exception as e:
            print(f"⚠️  Firebase alert error: {e}")
            return None

    def send_fcm_notification(self, alert):
        """
        Push notification to user's phone.

        Args:
            alert: Alert dict with threat_level, description, etc.
        """
        try:
            # Get device tokens (stored when user logs in on phone)
            device_tokens = self.db.child("system").child("device_tokens").get().val()

            if not device_tokens:
                print("⚠️  No device tokens registered")
                return

            for token in device_tokens:
                message = messaging.Message(
                    notification=messaging.Notification(
                        title=f"🚨 {alert.get('threat_level')} Threat Detected",
                        body=alert.get("description", "Motion detected")[:100]
                    ),
                    data={
                        "alert_id": alert.get("timestamp", ""),
                        "zone": alert.get("zone", ""),
                        "threat_level": alert.get("threat_level", ""),
                        "tier": str(alert.get("tier", 0))
                    },
                    token=token,
                )

                response = messaging.send(message)
                print(f"✓ FCM sent to {token}: {response}")

        except Exception as e:
            print(f"⚠️  FCM error: {e}")

    def update_system_state(self, armed, camera_on, night_vision, threat_level):
        """
        Broadcast system state to all connected dashboards.

        Args:
            armed: bool
            camera_on: bool
            night_vision: bool
            threat_level: int (0, 1, 2, 3)
        """
        if not self.db:
            return

        try:
            self.db.child("system").child("state").set({
                "armed": armed,
                "camera_on": camera_on,
                "night_vision": night_vision,
                "threat_level": threat_level,
                "timestamp": datetime.now().isoformat()
            })
            print(f"✓ System state updated: armed={armed}, threat={threat_level}")

        except Exception as e:
            print(f"⚠️  State update error: {e}")

    def log_event(self, event_type, data):
        """
        Log event for historical playback/analysis.

        Args:
            event_type: "motion", "person_detected", "alarm_armed", etc.
            data: dict with event details
        """
        if not self.db:
            return

        try:
            event = {
                "type": event_type,
                "timestamp": datetime.now().isoformat(),
                "data": data
            }
            event_id = str(uuid.uuid4())
            self.db.child("events").child(event_id).set(event)
            print(f"✓ Event logged: {event_type}")

        except Exception as e:
            print(f"⚠️  Event log error: {e}")

    def store_snapshot(self, snapshot_bytes, zone, timestamp=None):
        """
        Upload snapshot to Cloud Storage.

        Args:
            snapshot_bytes: JPEG bytes
            zone: "front_camera", "backyard", etc.
            timestamp: ISO string (or auto-generated)

        Returns:
            public URL (str) or None
        """
        if not timestamp:
            timestamp = datetime.now().isoformat()

        try:
            bucket = storage.bucket()
            path = f"snapshots/{zone}/{timestamp}.jpg"
            blob = bucket.blob(path)
            blob.upload_from_string(snapshot_bytes, content_type="image/jpeg")

            # Make public (optional - depends on security rules)
            url = blob.public_url
            print(f"✓ Snapshot stored: {url}")
            return url

        except Exception as e:
            print(f"⚠️  Snapshot storage error: {e}")
            return None

    def get_alerts(self, limit=10):
        """
        Fetch recent alerts.

        Args:
            limit: Number of alerts to fetch

        Returns:
            List of alert dicts
        """
        if not self.db:
            return []

        try:
            data = self.db.child("alerts").order_by_child("timestamp").limit_to_last(
                limit).get().val()
            if data:
                return list(data.values())
            return []

        except Exception as e:
            print(f"⚠️  Fetch alerts error: {e}")
            return []

    def register_device_token(self, device_token):
        """
        Save device token so we can send FCM notifications.
        Call this when user logs in on phone.

        Args:
            device_token: FCM token from Firebase Messaging
        """
        if not self.db:
            return

        try:
            self.db.child("system").child("device_tokens").push().set(device_token)
            print("✓ Device token registered")

        except Exception as e:
            print(f"⚠️  Device token error: {e}")


if __name__ == "__main__":
    # Test
    firebase = FirebaseManager("serviceAccountKey.json")

    # Send alert
    firebase.send_alert({
        "threat_level": "HIGH",
        "type": "person",
        "zone": "front_camera",
        "confidence": 0.95,
        "description": "Unknown person detected near entrance",
        "tier": 3
    })

    # Update state
    firebase.update_system_state(armed=True, camera_on=True, night_vision=False, threat_level=0)

    # Log event
    firebase.log_event("person_detected", {"confidence": 0.95, "box": [100, 100, 200, 300]})

    # Fetch alerts
    alerts = firebase.get_alerts(limit=5)
    print(f"Recent alerts: {alerts}")
