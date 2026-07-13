"""Send a push notification (and upload the snapshot) to the CAPHY phone app.

Uploads the alert snapshot to Firebase Storage, gets a shareable URL, and
includes it in the push so the phone shows the real photo. The storage bucket
name is read from config.FIREBASE_BUCKET. Safe no-op if key/library/bucket missing.
"""


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

    def send(self, tier, distance, snapshot_path=None, camera="Front Gate"):
        if not self.enabled:
            return
        m = self._messaging
        image_url = self._upload(snapshot_path)
        title = f"TIER {tier} - Threat detected"
        body = f"Person detected {distance} m from {camera}"
        data = {"tier": str(tier), "distance": str(distance), "camera": camera}
        if image_url:
            data["image_url"] = image_url
        try:
            msg = m.Message(
                topic=self.topic,
                notification=m.Notification(title=title, body=body, image=image_url),
                data=data)
            m.send(msg)
            print("[CAPHY] Push sent -> phone" + (" (with photo)" if image_url else ""))
        except Exception as e:
            print(f"[CAPHY] Push failed ({e}).")