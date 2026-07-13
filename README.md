# CAPHY — An AI-Intelligent Security System with Two-Factor Motion Validation and Monitoring Protocols

CAPHY turns an ordinary laptop into a resilient home-security server. Its core
idea is **two-factor validation**: cheap pixel-change motion detection (Factor 1)
gates a YOLOv8 person check (Factor 2), so shadows, pets, and wind never raise
false alarms. Confirmed people are sorted into a **3-tier threat level** by
distance, and alerts reach the owner through a dedicated app — working locally
first (built for brownouts) and syncing to the cloud when internet is available.

## Features
- **Two-factor detection** — OpenCV motion (MOG2, shadow-aware) + YOLOv8-nano person check
- **3-tier threat engine** — far → snapshot+alert, medium → record until person leaves, close → siren + video + alert
- **Highest-Security mode** — treat any confirmed person as the top tier
- **SQLite database** — every alert with tier, distance, confidence, snapshot, video, timestamp
- **Web dashboard** (6 screens) — login, dashboard, live camera (MJPEG), alert history, system logs, settings
- **Cloud sync** — offline queue + compression, auto-uploads when online (Firebase Storage)
- **Mobile app** (Flutter) — push alerts with the real snapshot, alert history
- **Bilingual voice** — offline Vosk commands in English **and** Tagalog, with confirmation
- **Software night vision** (CLAHE) and a **software siren**
- **Battery monitoring** + power-save, and a **distance-calibration** tool

## Requirements
- Python 3.10+  ·  a webcam
- Install libraries:
  ```
  pip install -r requirements.txt
  pip install psutil firebase-admin
  ```

## ⚠️ Private files to add after cloning (NOT in the repo)
These are intentionally excluded by `.gitignore` (secrets + large models), so a
fresh clone won't have them. Add them to make everything work:

| File / folder | Where it goes | Enables |
|---|---|---|
| `firebase_key.json` | project root | phone push notifications |
| `models/vosk-en/` | project root | English voice commands |
| `models/vosk-tl/` | project root | Tagalog voice (vosk-model-tl-ph-generic-0.6) |
| `caphy_app/android/app/google-services.json` | in the Flutter app | building the mobile app |

Voice models: https://alphacephei.com/vosk/models
Firebase key: Firebase Console → Project settings → Service accounts → Generate new private key
Then set your Storage bucket in `config.py`:  `FIREBASE_BUCKET = "your-bucket.appspot.com"`

## How to run
```
python main.py        # camera window: detection + tiers + siren + voice + push
python app.py         # web dashboard at http://127.0.0.1:5000  (login: admin / admin)
python run_sync.py    # cloud sync loop (offline-safe)
python run_voice.py   # voice control only (standalone)
python self-test.py   # camera-free proof the detection logic works
python calibrate.py --distance 3.0   # calibrate real-metre distances
python battery_status.py              # check battery / power-save
```
> Note: `main.py` and `app.py` both use the camera, so run only one at a time.

## Project layout
```
config.py            all settings
main.py              integrated system (camera window)
app.py               web dashboard entry
detection/           motion, person (YOLO), two-factor gate, tiers, night vision
storage/             database, alerts, cloud sync, push
web/                 Flask dashboard (server.py)
voice/               Vosk engine + command logic
siren.py, power.py, calibrate.py
caphy_app/           Flutter mobile app
training/            custom YOLO training kit (collect → label → train)
```

## Custom model (optional)
See `training/README.md` to fine-tune YOLOv8 on your own images, then set
`YOLO_MODEL` in `config.py` to your `best.pt`.

## Team
Khemberly D. Alao · King Leonard V. Kidsolan · Mark Alphy R. Miasis · Aedrean Marl I. Nudalo
Taguig City University — BS Computer Science