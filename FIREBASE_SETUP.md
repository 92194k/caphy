# Firebase Setup Guide for CAPHY

**Goal:** Configure Firebase for multi-user authentication, cloud storage, and push notifications.

---

## What You Need (Prerequisites)

1. **Google Cloud Account** — https://cloud.google.com/
2. **Firebase Project** — Already created: `caphy-c6b77` (you have access)
3. **Python 3.10+** with pip

---

## Step 1: Download Firebase Service Account Key

This key lets your CAPHY laptop authenticate with Firebase.

**In Firebase Console:**
1. Go to https://console.firebase.google.com/project/caphy-c6b77/settings/serviceaccounts/adminsdk
2. Click "Generate New Private Key"
3. Save as `firebase_key.json` in your CAPHY project root
4. **NEVER commit this file** (already in `.gitignore`)

**Verify:**
```
ls -la firebase_key.json
```

Should show a JSON file with `type: "service_account"`.

---

## Step 2: Set Up Google OAuth (for "Sign in with Google")

This is optional. If users only want email+password, skip this step.

**In Google Cloud Console:**
1. Go to https://console.cloud.google.com/apis/credentials?project=caphy-c6b77
2. Click "Create Credentials" → "OAuth 2.0 Client ID"
3. Choose "Web application"
4. Add Authorized redirect URIs:
   - `http://localhost:5000/callback`
   - `http://<your-laptop-ip>:5000/callback`
   - `http://127.0.0.1:5000/callback`
5. Copy the Client ID

**Add to config.py:**
```python
GOOGLE_CLIENT_ID = "YOUR_CLIENT_ID_HERE.apps.googleusercontent.com"
```

---

## Step 3: Install Firebase Admin SDK

```bash
pip install firebase-admin
```

Verify:
```bash
python -c "import firebase_admin; print('Firebase SDK installed')"
```

---

## Step 4: Update config.py

Add these settings:

```python
# ===== Firebase Configuration =====
FIREBASE_KEY_PATH = "firebase_key.json"
FIREBASE_BUCKET = "caphy-c6b77.appspot.com"  # Your Firebase Storage bucket
GOOGLE_CLIENT_ID = "YOUR_CLIENT_ID_HERE.apps.googleusercontent.com"  # (optional)
```

---

## Step 5: Test Firebase Connection

Run this from the CAPHY project root:

```bash
python -c "
from firebase_auth import FirebaseAuthManager
auth = FirebaseAuthManager()
print('Firebase Auth initialized successfully!')
"
```

If you get an error about the key file, check Step 1 again.

---

## Step 6: Verify Firebase Firestore / Storage

In Firebase Console:

1. **Storage** → https://console.firebase.google.com/project/caphy-c6b77/storage
   - Verify bucket exists: `caphy-c6b77.appspot.com`
   - Click "Rules" → should allow uploads by authenticated users (default is restrictive, we'll configure this in production)

2. **Authentication** → https://console.firebase.google.com/project/caphy-c6b77/authentication/users
   - This is where users will appear after they sign up

3. **Firestore** (optional, for advanced features later)

---

## Database Schema Changes

CAPHY now expects these columns:

**users table:**
- `firebase_uid` — Firebase UID (unique per user)
- `email` — Email address (for display)
- `password_hash` — Only if using email+password locally (optional with Google)

**alerts table:**
- `user_uid` — Firebase UID (which user owns this alert)
- `device_id` — CAPHY device_id (which laptop generated this alert)

These columns are auto-created on first run (migrations in `storage/database.py`).

---

## How It Works

### Email+Password Auth
1. User enters email + password on login page
2. CAPHY creates a Firebase user (if new) or validates (if existing)
3. Firebase returns a unique UID
4. CAPHY stores this UID in the session

### Google Sign-In
1. User clicks "Sign in with Google"
2. Google OAuth popup appears
3. User authenticates
4. Google returns a token
5. Firebase verifies the token
6. CAPHY stores the UID in the session

### Multi-User Isolation
- **Alerts** are scoped by `user_uid` — only that user sees their alerts
- **Push notifications** are scoped by FCM topic — only that user's phone gets notified
- **Cloud storage** is organized per-user → per-device

---

## Firestore Security Rules (Production)

When you're ready for production, add these security rules:

**In Firebase Console → Firestore → Rules:**

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{uid}/alerts/{document=**} {
      allow read, write: if request.auth.uid == uid;
    }
  }
}
```

This ensures users can only access their own data.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `FileNotFoundError: firebase_key.json` | Download from Firebase Console (Step 1) |
| `Invalid Google Client ID` | Copy the full string from Google Cloud Console, including `.apps.googleusercontent.com` |
| `Firebase not initialized` | Make sure `firebase_key.json` is in project root, not in a subfolder |
| `User can see other user's alerts` | Implement `user_uid` scoping in alert queries (see B3 auth_routes.py) |
| `Push notification not received` | Verify phone's FCM token is subscribed to the right topic |

---

## Next Steps

1. ✅ Download `firebase_key.json` (Step 1)
2. ✅ Install Firebase SDK (Step 3)
3. ✅ Update `config.py` (Step 4)
4. ✅ Test connection (Step 5)
5. Run CAPHY: `python app.py`
6. Open browser: http://127.0.0.1:5000/login
7. Try "Sign up" or "Sign in with Google"

---

## Files Touched by Firebase

- `firebase_auth.py` — Authentication logic
- `web/auth_routes.py` — Login/signup endpoints
- `web/templates/login_dual_auth.html` — Dual auth UI
- `storage/firebase_uploader.py` — Cloud file uploads
- `storage/firebase_push.py` — Push notifications
- `storage/database.py` — User/alert schema
- `config.py` — Firebase settings
- `requirements.txt` — firebase-admin dependency

---

## For Your Team

**Share these with team members:**
1. The Google Client ID (if using Google Sign-In)
2. Instructions to download their own `firebase_key.json` (not shared, per-machine)
3. Point them to this guide

Each machine's `firebase_key.json` is tied to that specific deployment. They do NOT need to commit it to Git.
