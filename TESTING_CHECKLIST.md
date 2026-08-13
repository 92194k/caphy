# CAPHY Full System Test Checklist

Run in order. Each row: **Do this** -> **Expect this**. Check the box when it matches.

---

## PART 1 — Core system (laptop only, no phone yet)

### 1.1 Startup

- [ ] Run `python app.py` from project root.
  **Expect:** Console prints Device ID, Hostname, then "Console: http://127.0.0.1:5000". No traceback. Camera light does NOT turn on yet (cameras start closed/paused).
- [ ] Open `http://127.0.0.1:5000`, log in (`admin` / `admin`).
  **Expect:** Dashboard loads, System Health panel shows OpenCV/Flask as "running", YOLOv8-nano status shown.

### 1.2 Camera lifecycle

- [ ] Click "Open Camera" (or equivalent) for Cam 0.
  **Expect:** Webcam light turns on, live feed appears within ~1-2s, `caphy.log` shows `camera OPEN`/opened source line.
- [ ] Click "Close Camera".
  **Expect:** Webcam light turns off, feed shows "Camera off" placeholder, log shows `camera OFF (device released)`.
- [ ] Re-open the same camera 3 times in a row.
  **Expect:** No crash, no duplicate device errors, each open/close cycle logs cleanly.

### 1.3 Two-factor detection (the core thesis logic)

- [ ] With camera open and **armed = OFF**, wave your hand / move an object (no person) in frame.
  **Expect:** Motion registers (System Health / live overlay shows motion), but NO alert is created — log shows `motion, no person - ignored`.
- [ ] Stand in frame, far from camera (~4.5m+).
  **Expect:** Tier 1 (far) — snapshot saved, alert appears in history, no siren, no video recording.
- [ ] Stand at medium distance (~2.5-4.5m).
  **Expect:** Tier 2 — snapshot + short video (auto-stops ~5s after you leave frame in unarmed mode).
- [ ] Stand close (<2.5m).
  **Expect:** Tier 3 — snapshot + siren sounds + video records continuously while you're in frame + stops ~1.5s after you leave.
- [ ] Walk toward the camera slowly across all three zones.
  **Expect:** Tier number increases smoothly, no rapid flicker between tiers (tests `TIER_SMOOTHING`/`TIER_HYSTERESIS`).

### 1.4 Armed / disarmed behavior

- [ ] Arm the system, then immediately stand in frame within 8 seconds.
  **Expect:** NO siren/alert fires (arm grace period — walking away from the laptop after arming shouldn't trigger on you).
- [ ] Wait past 8 seconds, stand in frame again.
  **Expect:** Now it alerts normally, and since armed, ALL tiers push a phone notification (if phone paired) not just Tier 3.
- [ ] Disarm, trigger Tier 1/2.
  **Expect:** Alert still saved to history, but no phone push (unarmed only pushes Tier 3).

### 1.5 Manual controls

- [ ] Toggle Night Vision on in a dim room.
  **Expect:** Live feed visibly brightens/clarifies (CLAHE effect).
- [ ] Trigger siren manually (button or voice, see Part 2).
  **Expect:** Sound plays immediately, "Siren off" stops it regardless of what triggered it.
- [ ] Trigger Emergency mode.
  **Expect:** Full response (siren + record) regardless of current tier/distance.

### 1.6 Stability check (run this while doing everything else)

- [ ] Leave CAPHY running 30-60 minutes with intermittent motion/detection.
  **Expect:** No crash, no frozen "last frame" while log keeps advancing, memory usage in Task Manager doesn't show a steep continuous climb (small growth then leveling off is normal; unbounded climb is not).

---

## PART 2 — With the phone app

### 2.1 Pairing

- [ ] Install/run the Flutter app, sign in (Firebase auth), pair to this laptop (QR or LAN discovery).
  **Expect:** Pairing succeeds, phone shows "connected"/laptop's device name.

### 2.2 Live view + control parity

- [ ] Open Live tab on phone while laptop dashboard is also open.
  **Expect:** Both show the same camera feed, roughly in sync (small delay is normal, several-second lag is not).
- [ ] Arm/disarm from the phone.
  **Expect:** Laptop dashboard reflects the change within a couple seconds.
- [ ] Trigger a Tier 3 alert in front of the camera.
  **Expect:** Phone receives a push notification with the real snapshot image.

### 2.3 Voice assistant (this is the part we just changed — test carefully)

- [ ] With internet on, say/type: "arm the system."
  **Expect:** Reply confirms arming, system actually arms (check laptop dashboard).
- [ ] Say: "what is CAPHY?" (a "know"-type open question).
  **Expect:** A real explanatory answer (this only works with internet — see Part 3).
- [ ] Say: "please disarm."
  **Expect:** System disarms — NOT arms. (This exact phrase was a bug I found and fixed; re-confirm it's correct.)
- [ ] Say: "turn off the siren" while siren is on.
  **Expect:** Siren stops — NOT starts.
- [ ] Say something nonsensical: "what's the weather like."
  **Expect:** A "clarify" / "I don't know" style reply — NOT an incorrect action executed.

### 2.4 Alert history + evidence

- [ ] Open Alert History on phone, tap into a Tier 2/3 alert.
  **Expect:** Snapshot loads; video (if Tier 2/3) plays.
- [ ] Dismiss an alert, then check "show all" toggle.
  **Expect:** Dismissed alert is hidden by default, reappears under "show all."

---

## PART 3 — Offline / degraded internet (do this LAST)

### 3.1 Fully offline (turn off laptop's Wi-Fi/Ethernet)

- [ ] Disconnect the laptop from the internet entirely. Trigger a Tier 3 detection.
  **Expect:** Detection, snapshot, siren, and local alert history all still work normally — nothing about local detection should degrade.
- [ ] Check the cloud sync status.
  **Expect:** Alert is saved with `synced=0` / shows "offline queue", NOT lost, NOT crashing the app.
- [ ] Use voice: "arm the system" (with laptop offline; phone can still be on Wi-Fi/LAN if reachable, or fully offline too).
  **Expect:** This is the fallback we just built — should say something like "I'm running offline right now..." if it can't match, or execute directly if the phrase matches a known command. Should NOT hang or return a raw error.
- [ ] Use voice: "what is CAPHY?" while offline.
  **Expect:** Should gracefully say it doesn't know / can't answer that offline — NOT crash, NOT hang for a long time (Groq calls should time out reasonably, not stall the whole voice pipeline).

### 3.2 Reconnect after being offline

- [ ] Restore internet. Wait a minute or trigger the sync manually.
  **Expect:** Queued offline alerts upload to Firebase; `synced=0` rows flip to `synced=1`; phone gets any push notifications that were missed (or at least sees them in history).

### 3.3 Same Wi-Fi, no internet (LAN-only mode)

- [ ] Connect phone and laptop to the same Wi-Fi/router, but the router itself has no internet (or block outbound on the laptop only).
  **Expect:** Per CAPHY's documented design, phone can still control/view via LAN even without internet — arm/disarm/live view should keep working; only push notifications and away-from-home (cloud relay) access should pause.

### 3.4 Slow/flaky internet (simulate with throttling if possible)

- [ ] Throttle bandwidth heavily (or just use a bad connection) and trigger several Tier 3 detections in a row.
  **Expect:** Detection loop and live view stay responsive (no freezing) even if Firebase upload/push is visibly slow or queued — this is the "cloud must never block detection" guarantee, worth specifically watching for lag on the live feed while an upload is in flight.

---

## Quick pass/fail summary (fill in after running)

| Section | Pass | Notes |
|---|---|---|
| 1. Core system | ☐ | |
| 2. Phone app | ☐ | |
| 3. Offline/degraded internet | ☐ | |

**If anything fails:** note the exact step, what you expected vs. what happened, and check `caphy.log` around that timestamp before reporting back — that log is the fastest way to see what actually happened internally.
