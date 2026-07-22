# CAPHY — Teammate Testing Guide (for the evaluation)

Run the system from source. No `.exe` or `.apk` needed for testing — the
laptop runs with `python app.py`, the phone with `flutter run`.

---

## 0. What you need BEFORE you start

- **Windows laptop** with a working **webcam**
- **Python 3.12** (NOT 3.13/3.14 — some packages don't have wheels for those)
- **Flutter** installed, and an **Android phone** (USB debugging on) or emulator
- **Two secret files** (they are NOT in GitHub on purpose):
  - `firebase_key.json`
  - `caphy_keys.json`

  👉 Get these from **[YOUR NAME]** and place BOTH in the CAPHY project root
  (same folder as `app.py`). Without them, login and cloud features won't work.

---

## 1. Get the latest code

```
cd C:\GitHub
git clone <YOUR_GITHUB_REPO_URL> CAPHY      # first time
# OR if you already cloned it:
cd C:\GitHub\CAPHY
git pull
```

Then drop `firebase_key.json` and `caphy_keys.json` into the CAPHY folder.

---

## 2. Run the desktop system

```
cd C:\GitHub\CAPHY
py -3.12 -m venv venv           # first time only
venv\Scripts\activate
pip install -r requirements.txt # first time only (takes a while)
python app.py
```

You should see, in the console:
```
[CAPHY] Starting cloud services ...
[CAPHY] WebRTC live streaming ready (aiortc + av OK).
[CAPHY] Cloud heartbeat OK - this laptop is now reachable ...
 * Running on http://127.0.0.1:5000
```

Open the dashboard on the laptop: **http://127.0.0.1:5000** and sign in
(Google or email). Leave `python app.py` running.

---

## 3. Run the phone app

Plug in the phone (USB debugging on), then:

```
cd C:\GitHub\CAPHY\caphy_app
flutter pub get
flutter run
```

The CAPHY app opens on the phone.

---

## 4. Connect the phone to the laptop (one scan)

1. On the **laptop** dashboard: **Settings → Connect Phone** (shows a QR code).
2. On the **phone**: tap **Scan QR code** and point it at the laptop screen.
3. The phone signs in and connects automatically — no password to type.

After this, the phone is paired to that laptop's account.

---

## 5. Evaluation checklist — test each of these

**Detection (the core thesis feature)**
- [ ] Stand in front of the camera → **Factor 1 (Motion)** and **Factor 2
      (Person)** both light up; a distance + threat tier appears
- [ ] Empty room → it does NOT flag a person (few false alarms)
- [ ] When armed, a person triggers an alert (and siren on Tier 3)

**Phone app**
- [ ] Live view works on **same Wi-Fi** as the laptop
- [ ] Live view works on **mobile data** (away from the laptop)
- [ ] A new alert sends a **push notification** and shows a **photo**
- [ ] Arm / Disarm / Camera / Siren buttons work from the phone
- [ ] Multiple laptops (if any) show under **Me → My Laptops**

**Offline / Local mode**
- [ ] Turn off the laptop's internet but stay on the same Wi-Fi → the app
      shows an amber "Offline mode · Local Wi-Fi" banner and live view still
      works locally

---

## 6. Troubleshooting

- **"Firebase not initialized" / login fails** → `firebase_key.json` is missing
  from the CAPHY folder. Ask [YOUR NAME] for it.
- **A package fails to install** → make sure you're on **Python 3.12** and the
  venv is activated (`(venv)` shows in the prompt).
- **Live view spins then shows video** → that's normal on Wi-Fi; it tries the
  fast local stream for ~5s, then uses WebRTC.
- **Phone can't connect after reinstalling the app** → re-scan the Connect
  Phone QR (a fresh install clears the saved connection).
- **Cloud features error about "index"** → the repo owner needs to run
  `firebase deploy --only firestore:rules,firestore:indexes` once.

---

## 7. Reporting results back

For each checklist item, note: ✅ works / ❌ fails. If something fails, copy
the **laptop console** lines from that moment — that's what pinpoints the fix.
