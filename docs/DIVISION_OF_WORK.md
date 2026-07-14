# CAPHY — Division of Work

This splits the CAPHY codebase into clear modules so the team can work in parallel
without stepping on each other. Each module lists its files, what it does, and a
suggested owner. Fill in the **Owner** column with real names, then branch per module
(e.g. `feature/mobile-app`) and open pull requests into `main`.

> Rule of thumb: one person owns the **detection core + integration** (the thesis
> backbone), the rest split the surfaces (mobile, docs, training, voice) around it.

---

## Module map

| # | Module | Files / folders | What it covers | Suggested owner |
|---|--------|-----------------|----------------|-----------------|
| 1 | **Detection core** (thesis backbone) | `detection/` (motion_detector, two_factor, tier_engine, night_vision, person_detector), `config.py` | Two-factor validation pipeline, 3-tier distance logic, CLAHE night vision | **Kem** (lead) |
| 2 | **Web console + API** | `web/server.py`, `web/templates/`, `web/static/` | Flask dashboard (Dashboard, Live, Alerts, History, Logs, Settings) + phone JSON API | **Kem** (lead) |
| 3 | **Storage & sync** | `storage/` (database, alerts, push, sync) | SQLite schema, alert saving, Firebase push, cloud sync | Teammate A |
| 4 | **Mobile app** | `caphy_app/lib/` (main, api, home_tab, alerts_tab, live_tab, me_tab, theme) | Flutter app: login, Home, Alerts, Live, Me + controls | Teammate A |
| 5 | **Model training & data** | `training/` (collect_data, auto_label, split_dataset, data.yaml), `runs/` | Dataset collection, labeling, YOLOv8 fine-tuning | Teammate B |
| 6 | **Calibration** | `tools/calibrate.py`, distance constants in `config.py` | Daylight distance calibration for the 3 tiers | Teammate B |
| 7 | **Voice control** | `voice/`, `tools/run_voice.py`, Vosk models | Bilingual (EN/TL) offline voice commands | Teammate C |
| 8 | **Peripherals** | `siren.py`, `power.py`, `tools/` (cam_test, mic_test, battery_status, list_cameras) | Software siren, battery/power-save, diagnostics | Teammate C |
| 9 | **Docs & thesis** | `docs/`, `README.md`, Chapters 1–3 | Architecture docs, setup guide, thesis write-up | Teammate D |

*(Adjust owners to your actual team size — merge rows if fewer people.)*

---

## Status snapshot (as of this push)

**Working**
- Detection pipeline (two-factor + 3-tier + night vision) — live
- Web console — all 6 screens rebuilt and wired to real data
- Phone JSON API — login, alerts, cameras, controls, voice, live frames
- Mobile app — login + Home + Alerts + Live + Me tabs functional over LAN

**Deferred / to do**
- Push notifications (Firebase) — removed from the app until the Android/Gradle
  Firebase config + package name (`com.caphy.app`) are fixed
- Real hold-to-talk mic — currently command-based voice; needs `speech_to_text`
- Tagalog voice model — not downloaded yet
- Daylight tier calibration
- YOLOv8 dataset fine-tuning
- Packaging: desktop `.exe` (PyInstaller) + mobile `.apk` (Flutter build) — **last step**

---

## Deployment plan

- **Now (development/demo):** run over the local network. Backend `python app.py`
  on the PC (listens on `0.0.0.0:5000`); web console at `http://<PC-IP>:5000`;
  phone app points at the same `http://<PC-IP>:5000`. On isolated Wi-Fi (dorm),
  use a phone hotspot so the phone and PC can see each other.
- **Final:** package the backend + console into a Windows **`.exe`** (PyInstaller),
  and build the mobile app into an installable **`.apk`** (`flutter build apk`).

---

## Git workflow (suggested)

1. `main` stays deployable. No direct commits.
2. One branch per module: `feature/<module>` (e.g. `feature/mobile-app`).
3. Open a pull request; at least one teammate reviews before merge.
4. Never commit secrets — `firebase_key.json` and `google-services.json` are
   already in `.gitignore`. Each developer keeps their own local copy.
