"""Send a push notification (and upload the snapshot) to the CAPHY phone app.

Uploads the alert snapshot to Firebase Storage, gets a shareable URL, and
includes it in the push so the phone shows the real photo. The storage bucket
name is read from config.FIREBASE_BUCKET. Safe no-op if key/library/bucket missing.
"""
import threading

# One Firebase app is shared by every camera worker. This lock makes the
# "check if initialized, else initialize" step atomic so multiple worker
# threads starting at once don't race and trip "default app already exists".
_INIT_LOCK = threading.Lock()


class PushSender:
    def __init__(self, key_path, topic="caphy_alerts"):
        self.topic = topic
        self.enabled = False
        self._messaging = None
        self._bucket = None
        try:
            import os
            import firebase_admin
            from firebase_admin import credentials, messaging
            import config
            bucket = getattr(config, "FIREBASE_BUCKET", None)
            if not os.path.exists(key_path):
                print(f"[CAPHY] Push off (no key at {key_path}).")
                return
            with _INIT_LOCK:
                if not firebase_admin._apps:
                    opts = {"storageBucket": bucket} if bucket else None
                    firebase_admin.initialize_app(credentials.Certificate(key_path), opts)
            self._messaging = messaging
            if bucket:
                try:
                    from firebase_admin import storage
                    self._bucket = storage.bucket(bucket)
                except Exception as e:
                    print(f"[CAPHY] Storage off ({e}).")
            self.enabled = True
            print("[CAPHY] Push notifications ready." + (" (photos on)" if self._bucket else ""))
        except Exception as e:
            print(f"[CAPHY] Push off ({e}).")

    def _upload(self, path):
        if not self._bucket or not path:
            return None
        try:
            import os
            from datetime import timedelta
            blob = self._bucket.blob("snapshots/" + os.path.basename(path))
            blob.upload_from_filename(path)
            return blob.generate_signed_url(expiration=timedelta(days=7))
        except Exception as e:
            print(f"[CAPHY] Snapshot upload failed ({e}).")
            return None

    def send_async(self, tier, distance, snapshot_path=None, camera="Front Gate", alert_id=None):
        """Fire-and-forget so the detection loop never blocks on the network."""
        if not self.enabled:
            return
        threading.Thread(target=self.send,
                         args=(tier, distance, snapshot_path, camera, alert_id),
                         daemon=True).start()

    def send(self, tier, distance, snapshot_path=None, camera="Front Gate", alert_id=None):
        if not self.enabled:
            return
        m = self._messaging
        title = f"CAPHY - Tier {tier} alert"
        body = f"Person detected {distance} m from {camera}"
        data = {"tier": str(tier), "distance": str(distance), "camera": str(camera)}
        if alert_id is not None:
            data["alert_id"] = str(alert_id)
        # 1) fire the TEXT notification immediately - this is the real-time part
        #    (no waiting on the photo upload). The app shows the photo when opened.
        try:
            m.send(m.Message(topic=self.topic,
                             notification=m.Notification(title=title, body=body),
                             data=data))
            print("[CAPHY] Push sent -> phone (instant).")
        except Exception as e:
            print(f"[CAPHY] Push failed ({e}).")
            return
        # 2) upload the snapshot + send a silent follow-up with the photo URL
        #    (background, non-critical - never delays the alert)
        if self._bucket and snapshot_path:
            threading.Thread(target=self._photo_followup,
                             args=(snapshot_path, dict(data)), daemon=True).start()

    def _photo_followup(self, snapshot_path, data):
        url = self._upload(snapshot_path)
        if not url:
            return
        try:
            data["image_url"] = url
            self._messaging.send(self._messaging.Message(topic=self.topic, data=data))
        except Exception:
            pass
