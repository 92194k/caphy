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
Run all commands **from the project root**.
```
python main.py        # camera window: detection + tiers + siren + voice + push
python app.py         # web dashboard at http://127.0.0.1:5000  (login: admin / admin)
python tools/run_sync.py    # cloud sync loop (offline-safe)
python tools/run_voice.py   # voice control only (standalone)
python tools/self-test.py   # camera-free proof the detection logic works
python tools/calibrate.py --distance 3.0   # calibrate real-metre distances
python tools/battery_status.py             # check battery / power-save
```
> Note: `main.py` and `app.py` both use the camera, so run only one at a time.

## Project layout
```
config.py            all settings
main.py              integrated system (camera window)
app.py               web dashboard entry
detection/           BACKEND — motion, person (YOLO), two-factor gate, tiers, night vision
storage/             DATABASE — database, alerts, cloud sync, push
web/                 API + FRONTEND — Flask dashboard (server.py), templates, static
voice/               BACKEND — Vosk engine + command logic
siren.py, power.py   siren + battery/power (shared, imported at root)
caphy_app/           FRONTEND — Flutter mobile app
cloud_sim/, models/  INFRASTRUCTURE — cloud simulator + AI/voice models

training/
├── data.yaml
├── dataset/
│   ├── images/
│   │   ├── train/
│   │   └── val/
│   ├── labels/
│   │   ├── train/
│   │   └── val/
├── train.py
└── runs/

tools/               standalone diagnostics & runners (run from project root)
docs/                ARCHITECTURE.md — how the folders map to system layers
```
See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full layer breakdown
(frontend / backend / database / API / infrastructure) and a diagram.

## Project Status

Current progress:

- ✅ Motion Detection
- ✅ YOLOv8 Person Detection
- ✅ Custom Dataset Collection
- ✅ Image Annotation
- ✅ YOLOv8 Training Pipeline
- ✅ Dataset Evaluation Plan
- ✅ Web Dashboard
- ✅ Mobile Notifications
- ✅ Voice Commands
- 🔄 Custom Model Optimization
- 🔄 Additional Dataset Collection


## AI Training Pipeline

CAPHY uses a custom-trained YOLOv8 Nano model to improve person detection
accuracy in real environments.

### Training Workflow

```
Collect Images
      ↓
Label Images
      ↓
Train YOLOv8
      ↓
Generate best.pt
      ↓
Deploy Model
```

### Dataset Structure

```
training/
└── dataset/
    ├── images/
    │   ├── train/
    │   └── val/
    │
    └── labels/
        ├── train/
        └── val/
```

Each image has a corresponding `.txt` annotation file using YOLO format.

Example:

```
0 0.512 0.483 0.211 0.654
```

where:

- `0` = Person class
- `x_center`
- `y_center`
- `width`
- `height`

(All values are normalized.)


### Dataset Collection

The dataset is collected using the laptop webcam in environments similar to the
target deployment.

Collected samples include:

- different lighting conditions
- multiple distances
- various body poses
- different camera angles
- indoor backgrounds

This improves the robustness and accuracy of the custom model.


### Current Dataset Information

- Object class: Person
- Annotation format: YOLO format
- Data source: Laptop webcam collection
- Dataset type: Custom CAPHY dataset
- Training framework: Ultralytics YOLOv8


## Data Collection Guidelines

Images should include:

- standing person
- walking person
- different distances
- different clothing
- different lighting conditions
- partial body visibility
- side view
- front view

Avoid:

- blurry images
- duplicate frames
- incorrect labels

A larger and more diverse dataset generally improves model performance.


## Image Labeling

Images are labeled using YOLO-compatible annotation tools.

Each image must have a corresponding label file with the same filename.

Example:

```
person001.jpg
person001.txt
```

Only one class is currently used:

| Class ID | Class Name |
|-----------|------------|
| 0 | person |


## Training

Train the custom YOLOv8 model using:

```bash
python training/train.py
```

The best trained model will be saved to:

```
training/runs/detect/caphy_person/weights/best.pt
```

To use the custom model, update:

```
config.py

YOLO_MODEL = "training/runs/detect/caphy_person/weights/best.pt"
```


## Model Evaluation

The trained model is evaluated using real-world testing sessions.

Evaluation scenarios include:

| Scenario | Expected Result |
|-----------|----------------|
| Daylight Person | Detect person |
| Daylight Non-human Motion | Ignore motion |
| Night Person | Detect person |
| Empty Scene | No detection |
| Different Distances | Stable detection |

Performance is monitored to reduce false positives while maintaining high person
detection accuracy.git diff README.md

## Team
Khemberly D. Alao · King Leonard V. Kidsolan · Mark Alphy R. Miasis · Aedrean Marl I. Nudalo
Taguig City University — BS Computer Science