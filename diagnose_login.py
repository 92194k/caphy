"""
diagnose_login.py — one-shot check for "sign in / sign up does nothing".

Run this with the SAME python that runs desktop_launcher.py:
    .\\venv\\Scripts\\python.exe diagnose_login.py

It exercises the exact same code path /api/auth/signup uses, without any
browser/JS/pywebview involved - so whatever breaks here is proven to be a
backend problem, not a UI one, and the printed error is the real one
instead of a generic "nothing happens".
"""

import sys
import traceback

print("=" * 60)
print("1. Can we import firebase_admin at all?")
print("=" * 60)
try:
    import firebase_admin
    print(f"OK - firebase_admin installed at: {firebase_admin.__file__}")
except Exception as e:
    print(f"FAIL: {e}")
    print("\nFix: pip install firebase-admin")
    sys.exit(1)

print()
print("=" * 60)
print("2. Does firebase_key.json exist and parse as a service account?")
print("=" * 60)
import os
import json
import config

key_path = getattr(config, "FIREBASE_KEY_PATH", "firebase_key.json")
if not os.path.exists(key_path):
    print(f"FAIL: {key_path} does not exist in {os.getcwd()}")
    print("Fix: download it from Firebase Console -> Project Settings -> "
          "Service Accounts -> Generate new private key, save it as "
          f"{key_path} in the CAPHY folder.")
    sys.exit(1)
try:
    with open(key_path, "r", encoding="utf-8") as f:
        keydata = json.load(f)
    print(f"OK - {key_path} exists, type={keydata.get('type')}, "
          f"project_id={keydata.get('project_id')}")
    if keydata.get("project_id") != "caphy-c6b77":
        print(f"WARNING: project_id is '{keydata.get('project_id')}', "
              f"expected 'caphy-c6b77' - wrong Firebase project's key?")
except Exception as e:
    print(f"FAIL: {key_path} exists but won't parse as JSON: {e}")
    sys.exit(1)

print()
print("=" * 60)
print("3. Can the Firebase Admin SDK actually initialize with this key?")
print("=" * 60)
try:
    from firebase_auth import init_firebase
    init_firebase()
    print("OK - Firebase Admin SDK initialized successfully")
except Exception as e:
    print(f"FAIL: {e}")
    traceback.print_exc()
    print("\nThis usually means: wrong key file, key was revoked/deleted "
          "in Firebase Console, or no internet connection.")
    sys.exit(1)

print()
print("=" * 60)
print("4. Can we actually create a test user via the Admin SDK?")
print("=" * 60)
try:
    from firebase_admin import auth as fb_auth
    test_email = "caphy_diagnose_test@example.com"
    try:
        existing = fb_auth.get_user_by_email(test_email)
        print(f"Test user already exists (uid={existing.uid}) - deleting it first...")
        fb_auth.delete_user(existing.uid)
    except fb_auth.UserNotFoundError:
        pass

    user = fb_auth.create_user(email=test_email, password="testpass123")
    print(f"OK - created test user, uid={user.uid}")
    fb_auth.delete_user(user.uid)
    print("OK - cleaned up test user")
except Exception as e:
    print(f"FAIL: {e}")
    traceback.print_exc()
    print("\nThis usually means: Email/Password sign-in provider is not "
          "enabled in Firebase Console -> Authentication -> Sign-in method, "
          "or the service account lacks Firebase Authentication Admin "
          "permission in Google Cloud IAM.")
    sys.exit(1)

print()
print("=" * 60)
print("5. Testing the FIREBASE_WEB_API_KEY used by /login (email+password sign-in)")
print("=" * 60)
try:
    import urllib.request
    import urllib.error

    api_key = getattr(config, "FIREBASE_WEB_API_KEY", "")
    if not api_key:
        print("FAIL: config.FIREBASE_WEB_API_KEY is empty")
        sys.exit(1)
    print(f"Using key: {api_key[:12]}...")

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    body = json.dumps({"email": "nonexistent@example.com", "password": "x",
                        "returnSecureToken": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("Unexpected: request succeeded for a nonexistent user")
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode())
        msg = payload.get("error", {}).get("message", "")
        if msg == "EMAIL_NOT_FOUND":
            print("OK - Web API key is valid and reachable (got the expected "
                  "EMAIL_NOT_FOUND for a made-up email)")
        elif "API key not valid" in msg or msg == "API_KEY_INVALID":
            print(f"FAIL: The Web API key itself is invalid: {msg}")
            print("Fix: get the correct Web API key from Firebase Console -> "
                  "Project Settings -> General -> Web API Key, put it in "
                  "config.FIREBASE_WEB_API_KEY")
            sys.exit(1)
        else:
            print(f"Got response: {msg} (this is probably fine - it means "
                  f"the key works, just an unexpected error code)")
except Exception as e:
    print(f"FAIL: {e}")
    traceback.print_exc()
    sys.exit(1)

print()
print("=" * 60)
print("ALL CHECKS PASSED")
print("=" * 60)
print("The backend auth pipeline works correctly when called directly.")
print("If signup/login still does nothing in the browser or desktop app,")
print("the problem is in the HTTP layer (Flask not running, wrong port,")
print("or a browser/JS-side issue) - not in Firebase itself.")
print()
print("Next step: with desktop_launcher.py running, open a REAL browser")
print("(not the desktop app window) to http://127.0.0.1:5000/login,")
print("press F12 for DevTools, go to the Network tab, and try signing up.")
print("Look for the /api/auth/signup request and report its status code")
print("and response body.")
