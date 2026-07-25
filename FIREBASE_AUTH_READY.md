# Firebase Auth Ready to Test

All code is in place. Here's what to do next:

## Quick Start

### 1. Update config.py

Add these lines at the **end** of `C:\GitHub\CAPHY\config.py`:

```python
# ===== Firebase Configuration (B3+B4+B5) =====
FIREBASE_KEY_PATH = "firebase_key.json"
FIREBASE_BUCKET = "caphy-c6b77.appspot.com"
GOOGLE_CLIENT_ID = ""  # Leave empty for now
```

### 2. Install firebase-admin (if not already done)

```bash
pip install firebase-admin
```

### 3. Run the test

```bash
python test_firebase_auth.py
```

Should show:
```
✅ firebase_key.json found
✅ firebase-admin installed
✅ FIREBASE_KEY_PATH = firebase_key.json
✅ FIREBASE_BUCKET = caphy-c6b77.appspot.com
✅ login_dual_auth.html found
✅ Flask app imports successfully
✅ /login shows Google Sign-In button
✅ /login shows Email + Password form
✅ /login shows Sign Up option

✅ ALL CHECKS PASSED
```

### 4. Start the system

```bash
python app.py
```

### 5. Open browser

Go to: **http://127.0.0.1:5000/login**

You should see:
- CAPHY logo
- "Sign in with Google" button
- Email + Password login form
- "Sign up" tab with email/password signup

### 6. Test signup

1. Click "Sign up" tab
2. Enter: test@example.com, password123, confirm password123
3. Click "Create Account"
4. Should redirect to dashboard

### 7. Verify in Firebase

Go to: https://console.firebase.google.com/project/caphy-c6b77/authentication/users

Should show your new test@example.com user.

---

## What Changed

**Files modified:**
- `web/server.py` — Updated /login route to use Firebase auth, added /api/auth/signup and /api/auth/google
- `storage/database.py` — Added firebase_uid + email columns (auto-migration on startup)

**Files created:**
- `web/templates/login_dual_auth.html` — Dual auth UI
- `firebase_auth.py` — Firebase Auth manager
- `storage/firebase_uploader.py` — Cloud sync
- `storage/firebase_push.py` — FCM push
- `test_firebase_auth.py` — Verification script

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "No module named flask" | Your venv isn't activated. Run: `venv\Scripts\activate` (Windows) |
| "firebase_key.json not found" | Download from Firebase Console (Step 1 of Quick Start) |
| Login page blank | Check browser console for JS errors (F12) |
| "Database disk image is malformed" | Delete `caphy.db` and restart (it will recreate) |
| Signup fails with "already registered" | Use a different email (or delete user from Firebase Console) |

---

## Next Steps

Once signup/login works:

1. Test with real motion detection
2. Verify alerts appear in dashboard
3. Test cloud sync (watch Firebase Storage)
4. Pair phone + test FCM (when Flutter app is updated)

---

Questions? Check TRACK_B_COMPLETE.md or INTEGRATION_CHECKLIST.md
