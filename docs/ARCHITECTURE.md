# CAPHY — System Architecture

This document maps CAPHY's folders onto the standard layers of a software system
(**frontend, backend, database, APIs, infrastructure**) so anyone can see how the
pieces fit together. The code was already organised this way by module — this is
the map, not a change to how it runs.

## The layers at a glance

```
                         ┌──────────────────────────────┐
                         │          FRONTEND            │
                         │  What users see & interact w/ │
                         │                              │
                         │  web/templates/  (dashboard)  │
                         │  web/static/     (CSS)        │
                         │  caphy_app/      (Flutter app)│
                         └───────────────┬──────────────┘
                                         │  HTTP / MJPEG / push
                                         ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                            API LAYER                          │
   │        The bridge between the frontend and the backend        │
   │                                                              │
   │   web/server.py   → Flask routes, live MJPEG stream, login    │
   │   storage/push.py → sends push notifications to the phone app  │
   └───────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                           BACKEND                            │
   │              The "brain" — business logic & AI               │
   │                                                              │
   │   detection/  motion_detector  (Factor 1: pixel change)       │
   │              person_detector   (Factor 2: YOLOv8 person)      │
   │              two_factor        (gate: motion AND person)      │
   │              tier_engine       (3-tier threat by distance)    │
   │              night_vision      (CLAHE low-light)              │
   │   voice/     engine, commands  (offline Vosk voice control)   │
   │   siren.py   power.py          (alarm + battery/power-save)   │
   │   main.py                      (integrated runtime loop)      │
   └───────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                          DATABASE                            │
   │                  Where all the data lives                     │
   │                                                              │
   │   storage/database.py  → SQLite (caphy.db): users, alerts     │
   │   storage/alerts.py    → writes snapshots + video clips       │
   │   storage/sync.py      → offline queue, uploads when online   │
   │   caphy.db             → the SQLite file                      │
   │   captures/            → saved alert images & video           │
   └───────────────────────────────┬──────────────────────────────┘
                                    │
                                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                        INFRASTRUCTURE                        │
   │           Where it runs & the outside services it uses        │
   │                                                              │
   │   cloud_sim/           → local stand-in for cloud storage     │
   │   firebase_key.json    → Firebase (cloud storage + push)      │
   │   models/vosk-en/      → offline speech model                 │
   │   yolov8n.pt           → YOLOv8 model weights                 │
   │   venv/, requirements.txt → Python runtime & dependencies     │
   └──────────────────────────────────────────────────────────────┘
```

## Folder-by-folder mapping

| Layer | Folder / file | Role |
|---|---|---|
| **Frontend** | `web/templates/`, `web/static/` | The 6-screen web dashboard (HTML + CSS) |
| **Frontend** | `caphy_app/` | Flutter mobile app (push alerts, history) |
| **API** | `web/server.py` | Flask routes, live camera stream (MJPEG), login |
| **API** | `storage/push.py` | Sends push notifications out to the phone |
| **Backend** | `detection/` | The AI: motion, person (YOLO), two-factor gate, tiers, night vision |
| **Backend** | `voice/` | Offline voice commands (Vosk engine + command parsing) |
| **Backend** | `main.py`, `siren.py`, `power.py` | Integrated runtime loop, siren, battery/power |
| **Database** | `storage/database.py`, `storage/alerts.py`, `storage/sync.py` | SQLite access, alert writing, cloud sync queue |
| **Database** | `caphy.db`, `captures/`, `captures_compressed/` | The data itself (records + media) |
| **Infrastructure** | `cloud_sim/`, `firebase_key.json` | Cloud storage/push (Firebase) and a local simulator |
| **Infrastructure** | `models/`, `yolov8n.pt`, `runs/` | AI models + training output |
| **Infrastructure** | `venv/`, `requirements.txt`, `config.py` | Runtime, dependencies, all settings |
| **Support** | `tools/` | Standalone scripts you run occasionally (see below) |
| **Support** | `training/` | Custom YOLO training kit (collect → label → train) |

## The `tools/` folder

These are utility and diagnostic scripts — not part of the always-on system, but
handy to run by hand. They were moved out of the project root to keep it clean.
Each one that needs project modules has a small "path bootstrap" at the top so it
still imports correctly.

> Run them **from the project root**, e.g. `python tools/self-test.py`.

| Script | What it does |
|---|---|
| `tools/self-test.py` | Camera-free proof the detection logic works |
| `tools/calibrate.py` | Calibrate real-metre distances for the tier engine |
| `tools/battery_status.py` | Check battery / power-save state |
| `tools/cam_test.py`, `tools/list_cameras.py` | Find and test webcams |
| `tools/mic_test.py` | Test the microphone + voice recognition |
| `tools/multicam.py` | Multi-camera detection preview |
| `tools/view_db.py` | Dump the alerts database to the terminal |
| `tools/run_sync.py` | Cloud sync loop (offline-safe), standalone |
| `tools/run_voice.py` | Voice control only, standalone |

## How a single alert flows through the layers

1. **Backend** — `main.py` reads the camera. `motion_detector` sees pixel change
   (Factor 1); `person_detector` (YOLOv8) confirms it's a person (Factor 2). The
   `two_factor` gate requires both, then `tier_engine` assigns a threat tier by
   distance.
2. **Database** — `storage/alerts.py` saves the snapshot/video to `captures/` and
   `storage/database.py` records the alert in `caphy.db`.
3. **API** — `storage/push.py` pushes a notification; `web/server.py` serves the
   dashboard and live stream.
4. **Frontend** — the web dashboard and the `caphy_app` phone app show the alert.
5. **Infrastructure** — `storage/sync.py` uploads to Firebase when internet is
   available (queued offline for brownouts).

## Why the core folders were **not** physically merged into `backend/` `frontend/`

CAPHY is a running Python project whose modules import each other assuming they
sit at the repo root (`import config`, `from detection... import`,
`from siren import Siren`). Moving `detection/`, `storage/`, `web/`, etc. into new
parent folders would break every one of those imports. So the layers are expressed
through **this map + clear module names**, which is how mature Python projects are
usually organised, rather than by relocating working packages.
