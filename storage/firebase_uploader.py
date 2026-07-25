"""
firebase_uploader.py — Firebase Cloud Storage uploader for CAPHY.

Replaces LocalCloudUploader. Uploads queued alerts (photos/videos) to
Firebase Storage under the user's account folder.

Usage:
    from storage.firebase_uploader import FirebaseUploader
    uploader = FirebaseUploader(user_uid, device_id)
    success = uploader.upload_file(local_path, remote_name)
"""

import os
import firebase_admin
from firebase_admin import storage
import config

# Initialize Firebase (should be done by firebase_auth.py first)
try:
    firebase_admin.get_app()
except ValueError:
    # App not initialized, try to init
    try:
        from firebase_auth import init_firebase
        init_firebase()
    except Exception as e:
        print(f"[Firebase Uploader] Warning: Firebase not initialized: {e}")


class FirebaseUploader:
    """Upload files to Firebase Cloud Storage."""

    def __init__(self, user_uid, device_id):
        """
        Args:
            user_uid: Firebase UID (organizes uploads per user)
            device_id: CAPHY device_id (organizes uploads per device)
        """
        self.user_uid = user_uid
        self.device_id = device_id
        self.bucket_name = getattr(config, "FIREBASE_BUCKET", None)

        if not self.bucket_name:
            raise ValueError(
                "FIREBASE_BUCKET not set in config.py. "
                "Set it to your Firebase Storage bucket (e.g., 'caphy-c6b77.appspot.com')"
            )

    def upload(self, path):
        """
        Matches the interface SyncManager expects (same shape as the old
        LocalCloudUploader.upload(path)). Returns the cloud path on success,
        or None on failure - SyncManager doesn't inspect the return value
        closely, but keeping it truthy/falsy makes future checks easy.
        """
        ok = self.upload_file(path)
        if ok:
            return f"{self.user_uid}/{self.device_id}/{os.path.basename(path)}"
        return None

    def upload_file(self, local_path, remote_name=None):
        """
        Upload a file to Firebase Storage.

        Args:
            local_path: Path to file on disk (str or Path)
            remote_name: Name in cloud (e.g., 'snapshot.jpg'). If None, uses basename.

        Returns:
            True if successful, False otherwise
        """
        if not os.path.exists(local_path):
            print(f"[Firebase] File not found: {local_path}")
            return False

        if not remote_name:
            remote_name = os.path.basename(local_path)

        # Organize by user → device → filename
        cloud_path = f"{self.user_uid}/{self.device_id}/{remote_name}"

        try:
            bucket = storage.bucket(self.bucket_name)
            blob = bucket.blob(cloud_path)
            blob.upload_from_filename(local_path)
            print(f"[Firebase] Uploaded: {cloud_path}")
            return True
        except Exception as e:
            print(f"[Firebase] Upload failed ({cloud_path}): {e}")
            return False

    def upload_bytes(self, data, remote_name):
        """
        Upload raw bytes (e.g., in-memory image).

        Args:
            data: Bytes to upload
            remote_name: Name in cloud (e.g., 'snapshot.jpg')

        Returns:
            True if successful, False otherwise
        """
        cloud_path = f"{self.user_uid}/{self.device_id}/{remote_name}"

        try:
            bucket = storage.bucket(self.bucket_name)
            blob = bucket.blob(cloud_path)
            blob.upload_from_string(data)
            print(f"[Firebase] Uploaded: {cloud_path}")
            return True
        except Exception as e:
            print(f"[Firebase] Upload failed ({cloud_path}): {e}")
            return False

    def list_files(self):
        """List all files for this user + device."""
        prefix = f"{self.user_uid}/{self.device_id}/"
        try:
            bucket = storage.bucket(self.bucket_name)
            blobs = bucket.list_blobs(prefix=prefix)
            return [blob.name for blob in blobs]
        except Exception as e:
            print(f"[Firebase] List failed: {e}")
            return []

    def delete_file(self, remote_name):
        """Delete a file from cloud storage."""
        cloud_path = f"{self.user_uid}/{self.device_id}/{remote_name}"
        try:
            bucket = storage.bucket(self.bucket_name)
            blob = bucket.blob(cloud_path)
            blob.delete()
            print(f"[Firebase] Deleted: {cloud_path}")
            return True
        except Exception as e:
            print(f"[Firebase] Delete failed ({cloud_path}): {e}")
            return False
