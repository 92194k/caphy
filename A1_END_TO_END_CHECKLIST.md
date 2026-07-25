# A1: End-to-End System Test Checklist

**Objective:** Verify CAPHY runs fully from laptop + phone, every feature works.

**Prerequisites:**
- Latest code pulled (after git push succeeds)
- Camera connected (Iriun confirmed working)
- Phone with CAPHY App installed
- Firebase project accessible (caphy-c6b77)

## Setup
- [ ] Start laptop: `python app.py`
- [ ] Open browser: http://127.0.0.1:5000 → login admin/admin
- [ ] Start Flutter app on phone
- [ ] Phone on same Wi-Fi as laptop

## Laptop Console Tests
- [ ] **Live feed** — camera displays in dashboard
- [ ] **Arm/Disarm** — button works, status updates
- [ ] **Settings** — open and close without crash
- [ ] **Emergency mode** — button triggers siren sound
- [ ] **Snapshot** — manually take photo, appears in captures/
- [ ] **Record** — manually start/stop, video saves
- [ ] **Night vision toggle** — CLAHE applies/removes in live feed
- [ ] **System logs** — view alert history in dashboard

## Phone App Tests
- [ ] **Home tab** — shows arm/disarm + status
- [ ] **Alerts tab** — displays past alerts + snapshots
- [ ] **Live feed** — connects to laptop's MJPEG stream
- [ ] **Voice commands** — speak Tagalog/English commands (if phone mic available)
  - "CAPHY, i-arm" / "CAPHY arm" 
  - "CAPHY, i-disarm" / "CAPHY disarm"
- [ ] **Emergency button** — triggers siren on laptop

## Detection Pipeline Tests (critical for thesis)
1. **Move in front of camera** at ~5 meters
   - [ ] OpenCV detects motion (Factor 1 ✓)
   - [ ] YOLOv8 confirms person (Factor 2 ✓)
   - [ ] Tier assigned (should be Tier 1, farthest)
   - [ ] Alert logged to database
   - [ ] Snapshot saved

2. **Move to ~3 meters** (if space allows)
   - [ ] Tier updates to Tier 2 (medium)
   - [ ] Video recording starts
   - [ ] Phone receives image alert via Firebase

3. **Move to <2.5 meters** (Tier 3, closest)
   - [ ] Siren sounds on laptop
   - [ ] Tier-3 alert sent to phone
   - [ ] Video recording active
   - [ ] **Acknowledge siren** via phone app button
     - Siren should stop

4. **Person leaves room**
   - [ ] Recording stops after grace period (1.5 sec)
   - [ ] Presence cleared, system ready for next alert

## Dark/Night Testing (if time allows)
- [ ] Disable room lights
- [ ] Toggle night vision ON on dashboard
- [ ] Confirm CLAHE brightens feed
- [ ] Motion detection still works in darkness
- [ ] Toggle night vision OFF → feed darkens again

## Failures to Document
If anything breaks, note:
- [ ] What action triggered it?
- [ ] Error message (screenshot or text)?
- [ ] Can you reproduce it?

## Success Criteria
✅ All boxes checked = system is production-ready for evaluation
❌ Any critical path (detection → alert → siren) fails = **blocker, do not proceed to A2**

## After Passing
- [ ] Take final screenshot of dashboard + phone alert
- [ ] Save one test video + one test snapshot to a folder labeled "A1_DEMO"
- [ ] Move to **A2: Calibrate Distance Constant**
