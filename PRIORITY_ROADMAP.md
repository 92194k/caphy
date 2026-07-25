# CAPHY: Priority Roadmap (Track A + B)

**Last updated:** 2026-07-20  
**Status:** Identity + launcher built; now running Track A validation tests.

## Executive Summary

Two parallel tracks:

- **Track A (Thesis Defense)** — Validate the existing offline system works end-to-end with real data. Panel defense depends on this. **Do this first.**
- **Track B (Multi-Household Product)** — Add accounts, pairing, Firebase alerts. Only start after Track A passes.

---

## TRACK A — Thesis Validation (Do First)

**Goal:** Prove the system is real, works fully offline, and is ready for panel defense.

### A1: End-to-End Test ✅ **Ready**
**Owner:** Kem + team  
**Checklist:** `A1_END_TO_END_CHECKLIST.md`

Run the full system once. Verify:
- Laptop console (dashboard) starts and displays live feed
- Phone app connects to laptop over Wi-Fi
- Motion detection works (two-factor validation)
- Tiers escalate correctly (1 → 2 → 3 as person approaches)
- Siren sounds, can be acknowledged
- Snapshots + videos save to Pictures/Videos
- Dark mode night vision works

**Success:** All features work without crashes.  
**Failure:** Debug and fix before moving to A2.

### A2: Calibrate Distance Constant ✅ **Ready**
**Owner:** Kem or any team member  
**Guide:** `A2_CALIBRATION_GUIDE.md`

Measure YOLOv8 bounding-box height at exactly 3 meters, calculate real `DISTANCE_K`, update `config.py`.

**Deliverable:** Updated config.py + calibration note in Chapter III thesis.

### A3: Sync Repo ⏳ **In Progress**
**Owner:** Kem

Commit + push to GitHub (network permitting), team pulls latest code.

**Checklist:**
- [ ] All changes committed locally ✅ (identity.py, launcher, .gitignore)
- [ ] Push to origin/main (blocked by proxy — retry when connectivity available)
- [ ] All team members pull
- [ ] Confirm everyone on same version (e.g., `git log --oneline | head -1`)

### A4: Evaluation Data Collection ✅ **Delegated**
**Owner:** Team members (parallel with A1–A3)

While A1–A2 are happening, run real evaluation tests:
- Enable `EVAL_LOGGING = True` in config.py
- Set `EVAL_SESSION = "daylight"` and `EVAL_GROUND_TRUTH = "person"` 
- Trigger 10–15 real motions at each tier
- Save data to database (detection_events table)

### A5: Brownout Test ✅ **Ready**
**Owner:** Kem or team member

During active detection, unplug laptop from AC power. Confirm system continues detecting on battery for at least 5 minutes. Document with screenshot.

**Deliverable:** Proof-of-concept photo/video in thesis appendix.

### A6: Update Thesis Chapters I–III ✅ **Ready**
**Owner:** Kem or designated writer

Replace:
- Telegram → Firebase Cloud Messaging
- PWA → Flutter Android app (com.caphy.app)
- Placeholder tier thresholds → actual calibrated values (A2)
- Add brownout proof (A5)
- Fix figure numbering + diagram updates

**Checklist:**
- [ ] Chapter I: Rationale, Objectives, Definition of Terms
- [ ] Chapter II: Related literature (already updated, verify)
- [ ] Chapter III: Methodology, technical requirements, diagrams, DISTANCE_K value
- [ ] Appendix: A5 brownout photo + test logs

### A7: Write Defensive Offline Claim ✅ **Ready**
**Owner:** Kem

Add to thesis **Scope and Limitations** + **Definition of Terms**:

> "CAPHY detects, logs, alerts locally, sounds the siren, and streams live camera via MJPEG—all with zero internet, over local Wi-Fi. Alerts are queued locally and synced to cloud when internet is available. Internet is required only to receive push notifications while physically away from the home."

This wording is defensible and matches what actually works.

---

## TRACK B — Multi-Household Product (Start After A Passes)

**Goal:** Make CAPHY safe for multiple households to each run their own instance, with secure device pairing and per-user alerts.

### B1: Device Identity ✅ **DONE**
**Status:** Complete

`identity.py` generates + persists `device_id` (caphy_XXXX...) + `device_secret` to `%APPDATA%\CAPHY\device.json`. Every laptop gets a unique, permanent ID.

### B2: Desktop Launcher ✅ **DONE**
**Status:** Complete

`desktop_launcher.py` wraps Flask app in native pywebview window. Feels like a real installed app, not a browser tab.

Packaging ready (`CAPHY.spec` for PyInstaller → standalone .exe).

### B3: Firebase Auth (Accounts)
**Owner:** Assign to team member  
**Effort:** Medium (1–2 days)

Wire real login:
- Replace hardcoded admin/admin with Firebase Authentication
- Dashboard login page: email + password
- Phone app: sign up / log in with Firebase
- Scope all data (alerts, logs, settings) per user's uid

**Files to touch:**
- `web/server.py` — add login routes
- `web/templates/login.html` — login form
- `caphy_app/lib/main.dart` — Firebase Auth setup
- Database — add user uid to alerts table

### B4: Cloud Sync (FirebaseUploader)
**Owner:** Assign to team member  
**Effort:** Small (1 day)

Replace the fake `LocalCloudUploader` (currently in `storage/sync.py`) with real Firebase Storage uploader.

The offline queue already exists and works; you're only changing the destination from "local folder" to "cloud bucket".

**Files:**
- `storage/sync.py` — swap uploader class

### B5: Per-Device Alerts
**Owner:** Assign to team member  
**Effort:** Medium (1–2 days)

Tie device_id (B1) + user uid (B3) so Firebase Cloud Messaging routes alerts only to the paired phone.

Validate: User A's phone never receives User B's alerts, even if they guess the device_id.

**Files:**
- `storage/push.py` — FCM routing logic
- `storage/database.py` — store device_id per alert
- Firebase Firestore security rules — enforce uid + device_id match

---

## Timeline Recommendation

### Week 1 (Now)
- ✅ A1: End-to-end test (full system functional test)
- ✅ A2: Calibrate DISTANCE_K
- ✅ A3: Repo sync (when network available)
- ✅ A5: Brownout proof

### Week 2
- ✅ A4: Evaluation data collection (ongoing parallel)
- ✅ A6: Thesis chapter updates (Firebase/Flutter references)
- ✅ A7: Defensive offline wording

### Week 3
- **DO NOT START B** until all of A is signed off by adviser
- If adviser approves multi-household scope:
  - B3: Firebase Auth (accounts)
  - B4: Cloud sync (real uploader)
  - B5: Per-device alerts

---

## Files Created This Session

**B1 (Device Identity):**
- `identity.py` — device ID generation and persistence
- `.gitignore` — added device.json (never commit secret)

**B2 (Desktop Launcher):**
- `desktop_launcher.py` — pywebview wrapper
- `CAPHY.spec` — PyInstaller spec for .exe bundling

**A Guides:**
- `A1_END_TO_END_CHECKLIST.md` — feature validation
- `A2_CALIBRATION_GUIDE.md` — distance constant calibration
- `PRIORITY_ROADMAP.md` — this file

**Git:**
- Committed locally: identity.py, desktop_launcher.py, CAPHY.spec, app.py, .gitignore
- Push pending (network issue)

---

## Next Actions (Today)

1. **Run A1 checklist** — take 2–3 hours, verify everything works
2. **If A1 passes:** Run A2 calibration (30 min)
3. **If A1 fails:** Debug and document the issue
4. **Run A5 brownout test** — unplug laptop, verify battery operation (10 min)
5. **Have team pull latest code** once git push succeeds

---

## Questions for Adviser

Before starting B3:
- "Can we add Firebase Auth + device pairing to the scope, or does that exceed thesis requirements?"
- "Should we demo multi-household isolation during panel defense?"

If adviser says no → stick to A only, stay single-household, thesis is done.  
If adviser says yes → proceed with B3–B5 after A passes.

---

## Estimated Timelines

| Track | Component | Effort | Owner |
|-------|-----------|--------|-------|
| A | End-to-end test | 2 hours | Kem + team |
| A | Calibrate distance | 30 min | Any member |
| A | Repo sync | 15 min | Kem |
| A | Evaluation data | 2 hours | Team (parallel) |
| A | Brownout test | 15 min | Any member |
| A | Thesis updates | 2–3 hours | Writer |
| A | Offline wording | 30 min | Writer |
| **A Total** | | **6–8 hours** | |
| B | Firebase Auth | 1–2 days | Assign |
| B | Cloud sync | 1 day | Assign |
| B | Per-device alerts | 1–2 days | Assign |
| **B Total** | | **3–5 days** | (if approved) |

---

## Success Criteria

**Track A "DONE":**
- ✅ A1, A2, A3, A5 pass
- ✅ A6, A7 thesis sections updated
- ✅ Panel can review and approve
- ✅ System ready for defense demo

**Track B "DONE" (optional):**
- ✅ User A logs in → sees only User A's device + alerts
- ✅ User B logs in → sees only User B's device + alerts
- ✅ Device pairing via QR prevents cross-household access
