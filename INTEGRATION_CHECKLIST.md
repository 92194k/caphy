# Track B Integration Checklist

**All code is written. Here's how to integrate and test.**

---

## Phase 1: Setup (30 min)

- [ ] **Download firebase_key.json**
  - Firebase Console → Project Settings → Service Accounts → Generate Key
  - Save to: `C:\GitHub\CAPHY\firebase_key.json`
  - Verify it exists: `ls -la firebase_key.json`

- [ ] **Update config.py** (add at end of file)
  ```python
  # Firebase Configuration (B3+B4+B5)
  FIREBASE_KEY_PATH = "firebase_key.json"
  FIREBASE_BUCKET = "caphy-c6b77.appspot.com"
  GOOGLE_CLIENT_ID = ""  # Leave empty for now; optional for Google Sign-In
  ```

- [ ] **Install firebase-admin**
  ```bash
  pip install firebase-admin
  ```

- [ ] **Verify setup**
  ```bash
  python -c "from firebase_auth import FirebaseAuthManager; m = FirebaseAuthManager(); print('✅ Firebase Auth ready')"
  ```

---

## Phase 2: Test B3 (Auth) — 30 min

- [ ] **Start the system**
  ```bash
  python app.py
  ```

- [ ] **Open login page**
  - Browser: http://127.0.0.1:5000/login
  - Should see: "CAPHY" logo, email/password form, "Sign in with Google" button

- [ ] **Sign up (email+password)**
  - Click "Sign up" tab
  - Enter: test@example.com, password123, confirm password
  - Click "Create Account"
  - Should redirect to dashboard

- [ ] **Verify user in Firebase**
  - Firebase Console → Authentication → Users
  - Should see test@example.com (or show as local if no Firebase yet)

- [ ] **Sign in (test existing user)**
  - Log out: http://127.0.0.1:5000/logout
  - Login: test@example.com, password123
  - Should redirect to dashboard

- [ ] **Verify session**
  - Browser DevTools → Application → Cookies
  - Should see `session` cookie with auth data

---

## Phase 3: Test B4 (Cloud Sync) — 20 min

- [ ] **Trigger a motion alert**
  - Wave in front of camera
  - Wait for YOLOv8 to confirm person
  - Should see alert in dashboard

- [ ] **Check cloud upload**
  - Firebase Console → Storage → caphy-c6b77.appspot.com
  - Navigate: `<user_id>/<device_id>/`
  - Should see: `snapshot.jpg`, `video.mp4`, etc.

- [ ] **Test offline → online sync**
  - Disconnect internet (unplug cable or disable Wi-Fi)
  - Trigger another alert (should queue locally)
  - Reconnect internet
  - Verify queued alert uploads to cloud

---

## Phase 4: Test B5 (FCM Push) — 1 hour

**Prerequisites:** Flutter app installed on Android phone

- [ ] **Set up phone**
  - Install CAPHY Flutter app
  - Sign in with same account (test@example.com)

- [ ] **Get phone's FCM token**
  - In Flutter app, find Settings or About screen
  - Copy FCM token (or check Flutter console logs)
  - Save for next step

- [ ] **Register phone with device**
  - Laptop: POST to `/api/fcm/subscribe` with phone's FCM token
  - (Or add UI button to register in phone app Settings)

- [ ] **Trigger alert on laptop**
  - Wave in front of camera again
  - Alert fires

- [ ] **Verify push received on phone**
  - Phone should get notification: "Motion Detected"
  - Tap notification → opens CAPHY app → shows snapshot

- [ ] **Verify scoping (multi-household test)**
  - Create 2nd Firebase account: alice@, bob@
  - Alice's laptop: alice signs in, triggers alert
  - Bob's laptop: bob signs in, triggers alert
  - **Alice's phone gets only alice's alerts**
  - **Bob's phone gets only bob's alerts**
  - ✅ If true, isolation is working

---

## Phase 5: Integration Tasks for Team

### Backend Dev
- [ ] Update `web/server.py`:
  - Import `auth_routes.py`
  - Call `register_auth_routes(app)`
  - Replace old login logic
  - Add `@require_auth` decorator to protected routes

- [ ] Update `storage/sync.py`:
  - Replace `LocalCloudUploader` with `FirebaseUploader`
  - Pass `user_uid, device_id` from session/config

- [ ] Update `storage/alerts.py` or wherever alerts are created:
  - Add `user_uid` and `device_id` when saving alerts

### Mobile Dev (Flutter)
- [ ] Add Firebase Auth to Flutter app
  - Import `firebase_auth` package
  - Create login/signup screens
  - Wire to new `/api/auth/signup` and `/api/auth/google` endpoints

- [ ] Add FCM setup
  - Import `firebase_messaging`
  - Get FCM token on app startup
  - Subscribe to topic: `caphy-{user_uid}-{device_id}`
  - Display push notifications

- [ ] Update API calls
  - Pass Firebase ID token in headers
  - Scope API requests by device_id

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `FileNotFoundError: firebase_key.json` | Download from Firebase Console (Phase 1) |
| `FIREBASE_BUCKET not set` | Add to config.py (Phase 1) |
| Login page blank | Check browser console for JS errors |
| Push not received | Check phone's FCM token is subscribed to correct topic |
| User sees other user's alerts | Verify `user_uid` scoping in alert queries |
| Cloud storage shows wrong path | Check device_id matches (from identity.py) |

---

## Success Criteria

✅ **B3 (Auth) passed** — users can sign up/in with email or Google  
✅ **B4 (Cloud sync) passed** — alerts upload to Firebase Storage  
✅ **B5 (FCM) passed** — push notifications reach paired phone only  
✅ **Isolation verified** — User A's phone never gets User B's data  

If all four above pass → **Ready for thesis panel defense** 🎓

---

## Estimated Timeline

| Phase | Time | Owner |
|-------|------|-------|
| 1. Setup | 30 min | Anyone |
| 2. Auth test | 30 min | Backend + QA |
| 3. Cloud sync test | 20 min | Backend |
| 4. FCM test | 1 hour | Mobile + Backend |
| 5. Team integration | 2-3 hours | Full team |
| **Total** | ~4-5 hours | |

---

## Files to Review Before Starting

1. **FIREBASE_SETUP.md** — detailed Firebase configuration
2. **TRACK_B_COMPLETE.md** — architecture + what was built
3. **Code comments** — each file has inline documentation

---

## Questions?

- Check FIREBASE_SETUP.md for config issues
- Check code comments in each file
- Reference memory.md for project context
- Ask Kem for design decisions

---

Good luck! 🚀
