"""Phase 7 - send a push notification to the CAPHY phone app via Firebase.

Uses the Firebase Admin SDK + your service-account key to send a message to the
'caphy_alerts' topic that the app subscribes to. Safe no-op if the key or
library is missing (so the PC system still runs without a phone connected).
"""


class PushSender:
    def __init__(self, key_path, topic="caphy_alerts"):
        self.topic = topic
        self.enabled = False
        self._messaging = None
        try:
            import os
            import firebase_admin
            from firebase_admin import credentials, messaging
            if not os.path.exists(key_path):
                print(f"[CAPHY] Push off (no key at {key_path}).")
                return
            if not firebase_admin._apps:                 # init once
                firebase_admin.initialize_app(credentials.Certificate(key_path))
            self._messaging = messaging
            self.enabled = True
            print("[CAPHY] Push notifications ready.")
        except Exception as e:
            print(f"[CAPHY] Push off ({e}).")

    def send(self, tier, distance, camera="Front Gate", image_url=None):
        if not self.enabled:
            return
        m = self._messaging
        title = f"TIER {tier} - Threat detected"
        body = f"Person detected {distance} m from {camera}"
        data = {"tier": str(tier), "distance": str(distance), "camera": camera}
        if image_url:
            data["image_url"] = image_url
        try:
            m.send(m.Message(
                topic=self.topic,
                notification=m.Notification(title=title, body=body),
                data=data,
            ))
            print(f"[CAPHY] Push sent -> phone: {title}")
        except Exception as e:
            print(f"[CAPHY] Push failed ({e}).")