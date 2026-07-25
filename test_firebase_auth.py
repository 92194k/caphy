"""
Quick test script to verify Firebase Auth setup and login page.

Run this before testing in browser:
    python test_firebase_auth.py
"""

import sys
import os

print("[TEST] Checking prerequisites...\n")

# 1. Check firebase_key.json
if not os.path.exists("firebase_key.json"):
    print("❌ firebase_key.json not found")
    print("   Download from Firebase Console → Project Settings → Service Accounts")
    sys.exit(1)
print("✅ firebase_key.json found")

# 2. Check firebase-admin installed
try:
    import firebase_admin
    print("✅ firebase-admin installed")
except ImportError:
    print("❌ firebase-admin NOT installed")
    print("   Run: pip install firebase-admin")
    sys.exit(1)

# 3. Check config.py has Firebase settings
import config
if not hasattr(config, "FIREBASE_KEY_PATH"):
    print("⚠️  FIREBASE_KEY_PATH not in config.py")
    print("   Add to config.py: FIREBASE_KEY_PATH = 'firebase_key.json'")
else:
    print(f"✅ FIREBASE_KEY_PATH = {config.FIREBASE_KEY_PATH}")

if not hasattr(config, "FIREBASE_BUCKET"):
    print("⚠️  FIREBASE_BUCKET not in config.py")
    print("   Add to config.py: FIREBASE_BUCKET = 'caphy-c6b77.appspot.com'")
else:
    print(f"✅ FIREBASE_BUCKET = {config.FIREBASE_BUCKET}")

# 4. Check login_dual_auth.html exists
if not os.path.exists("web/templates/login_dual_auth.html"):
    print("❌ login_dual_auth.html not found")
    sys.exit(1)
print("✅ login_dual_auth.html found")

# 5. Try to import Flask app
try:
    from web.server import app
    print("✅ Flask app imports successfully")
except Exception as e:
    print(f"❌ Failed to import Flask app: {e}")
    sys.exit(1)

# 6. Test /login route
print("\n[TEST] Testing /login route...\n")
try:
    with app.test_client() as client:
        response = client.get("/login")
        html = response.get_data(as_text=True)

        if response.status_code != 200:
            print(f"❌ /login returned {response.status_code}")
            sys.exit(1)

        if "Email" not in html or "Password" not in html:
            print("❌ /login NOT showing email/password form")
            print("   First 500 chars:", html[:500])
            sys.exit(1)

        if "Email" in html and "Password" in html:
            print("✅ /login shows Email + Password form")
        if "Sign" in html and "up" in html.lower():
            print("✅ /login shows Sign Up option")
        if "CAPHY" in html:
            print("✅ /login shows CAPHY branding")

except Exception as e:
    print(f"❌ Error testing /login: {e}")
    sys.exit(1)

print("\n" + "="*50)
print("✅ ALL CHECKS PASSED")
print("="*50)
print("\nYou're ready to test. Run:")
print("  python app.py")
print("\nThen open browser:")
print("  http://127.0.0.1:5000/login")
