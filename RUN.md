# How to run CAPHY

## 0. One-time setup

Open PowerShell in `C:\GitHub\CAPHY` and activate the virtual environment:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
```

You should see `(venv)` at the start of the prompt. **Everything below assumes
`(venv)` is showing.**

```powershell
python -m pip install -r requirements.txt
```

> **Use `python -m pip`, not plain `pip`.** This venv was created while the
> project lived at `OneDrive\Desktop\CAPHY`, so the `.exe` launchers in
> `venv\Scripts` still point at that old path and fail. `python.exe` is a real
> copy and works fine.

---

## 1. Run the system

```powershell
python app.py
```

Open **http://127.0.0.1:5000** and log in (`admin` / `admin`).
From the phone on the same Wi-Fi: `http://<your-PC-IP>:5000`

Camera options:

```powershell
python app.py          # auto-detect connected cameras
python app.py 0        # laptop webcam only
python app.py 1        # phone camera only (Iriun)
python app.py 0 1      # both, side by side
```

Desktop window version, no dashboard:

```powershell
python main.py             # camera window + detection
python main.py --no-yolo   # motion only
```

---

## 2. Run the phone app

```powershell
cd caphy_app
flutter run
```

In the app, set the server address to `http://<your-PC-IP>:5000` and log in.

---

## Where voice lives

**Voice runs only in the phone app, and only as fixed commands.**
CAPHY does not chat. The PC never listens and never speaks.

| Step | Where |
|---|---|
| Hearing you | Phone - device speech-to-text (`speech_to_text`) |
| Understanding the words | PC - `POST /api/voice`, matched against `voice/intents.json` |
| Doing the action | PC - `_do_action()` in `web/server.py` |
| Speaking the reply | Phone - device text-to-speech (`flutter_tts`) |

There is **one** voice endpoint: `POST /api/voice`. Anything that isn't a known
command comes back as "Sorry, I did not understand that command" (or the
Tagalog equivalent), which the app speaks.

The PC has no microphone loop, no Vosk, no pyttsx3. Those packages are not in
`requirements.txt` and are not needed.

> `sounddevice` IS still required - but for `siren.py`, which generates the
> alarm tone. That is not speech.

### No conversation, on purpose

There used to be a "Talk with CAPHY" mode backed by Google Gemini. It is gone.
Commands only. This matters for the thesis: **controlling the house now needs
no internet at all.** Recognition happens on the phone, matching happens on the
PC, and no request ever leaves the local network.

### Using voice in the app

Live tab → mic button. Tap it and speak; the words appear as you say them.
Tap the list icon for every command CAPHY understands, grouped, with the exact
phrases in English and Tagalog. Tap any phrase to run it.

### Changing what CAPHY understands

All phrases live in **`voice\intents.json`**. Edit there, never in Python or
Dart. The app fetches the list from `GET /api/intents`, so it updates itself -
no app rebuild needed, just restart `app.py`.

Rules:

- English phrases in `en`, Tagalog in `tl`. Don't mix.
- Avoid phrases that differ only by "on" vs "off" - they sound nearly
  identical. Use `enable`/`disable`, `buksan`/`patayin`.

---

## Threat tiers

Tier comes from the YOLO bounding-box height, used as a distance proxy.

```python
DISTANCE_K      = 900.0
TIER1_MIN_DIST  = 4.5     # farther than this -> Tier 1
TIER3_MAX_DIST  = 2.5     # closer than this  -> Tier 3

TIER_SMOOTHING  = 0.35
TIER_HYSTERESIS = 0.12
```

**Why smoothing and hysteresis exist.** A YOLO box jitters a few percent every
frame even when the person is standing still. Reading raw thresholds gave 13
tier changes in 20 frames for a motionless person - and near the Tier 2/3 line
that flicker reached Tier 3, firing the siren at random.

The distance is now a moving average, and a threshold must be crossed by a
clear margin before the tier changes.

| Symptom | Fix |
|---|---|
| Tier still wobbles | Raise `TIER_HYSTERESIS` (0.12 → 0.18) |
| Tier 3 / siren triggers too late | Lower `TIER_HYSTERESIS` (0.12 → 0.08) |
| Tier reacts too slowly | Raise `TIER_SMOOTHING` (0.35 → 0.5) |
| Distances are simply wrong | Recalibrate `DISTANCE_K` - see below |

`DISTANCE_K` is uncalibrated. To calibrate: stand at a measured 3 m, note the
box height `h` from the dashboard, then set `DISTANCE_K = 3.0 * h`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No module named cv2` | venv not activated, or requirements not installed |
| Camera won't open | Another app is using it (Zoom, Teams, browser) |
| `database is locked` | `app.py` and `main.py` running at once - run only one |
| App can't reach system | Wrong IP in app settings, or phone on another Wi-Fi |
| Mic does nothing in app | Grant microphone permission in Android settings |
| Command list empty in app | App can't reach the PC - check the address |
