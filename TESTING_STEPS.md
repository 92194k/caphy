# CAPHY — Testing Steps (Laptop)

Run `python app.py` from `C:\GitHub\CAPHY` (venv activated), then open
`http://127.0.0.1:5000` and log in (`admin` / `admin`). Go through each
section below in order.

## 1. Logo (new)

- Sidebar (top-left, every page): shows the new shield+eye icon, not the old
  plain SVG shield.
- Browser tab favicon: same new icon, small.
- Click the sidebar logo: a full-screen dark modal pops up with the icon
  large and centered, a soft pulsing glow around it, and "CAPHY" animating in
  letter-by-letter underneath.
- Also check the **login page**, **console/kiosk page** (`/console` if used),
  and the **set-password page** — all three should show the same new icon,
  not the old one.

## 2. Cameras closed by default

- Fully stop and restart `app.py`.
- Open the Dashboard / Live Camera page immediately — both Camera 1 and
  Camera 2 should show as **closed** (no live feed), not auto-opened.
- No alert/snapshot should appear just from starting the app with nobody in
  frame (this was the false-alert-on-startup bug).

## 3. Individual camera open/close

- On Live Camera, open only Camera 1 — Camera 2 must stay closed.
- Close Camera 1 — only Camera 1 stops; Camera 2 (if open) is unaffected.
- The Camera On/Off button: icon should be a power-toggle glyph (not a
  camera icon, not an X, not a refresh icon) and should look visibly
  "pressed"/active (orange) when the camera is closed, and the resting green
  style when open. Refresh the page — the button state should still match
  reality (no green-when-off flip).

## 4. Open All / Close All

- These two buttons should behave exactly as before — untouched. Open All
  opens both cameras; Close All closes both. Confirm this still works
  alongside the per-camera controls above.

## 5. Three-tier detection (Unarmed / default mode)

- With system unarmed, walk into frame far away — Tier 1 (snapshot +
  notification only, no video, no siren).
- Move closer — Tier 2 (adds a ~5 sec video clip).
- Move very close — Tier 3 (records until you leave frame; no siren while
  unarmed).

## 6. Armed mode

- Press ARM. Repeat the same distances — all three tiers should now escalate
  to high-alert behavior (siren where applicable, ack-required on Tier 3).

## 7. Emergency Mode

- Trigger Emergency Mode — all cameras should open and the siren should
  sound regardless of current tier/distance.

## 8. Alert History

- Confirm the standalone "Alerts" page is gone from the nav — only "Alerts
  History" remains.
- Each alert row shows an actual snapshot **thumbnail preview**, not a
  generic icon.
- Delete a test alert — confirmation prompt appears, and after confirming,
  the row (and its snapshot/video file on disk) is gone.
- With the History page open in one tab, trigger a new alert from another
  camera — a "new alert(s) came in" banner should appear live (no full page
  reload, filters/pagination stay intact).

## 9. Cloud sync / System Logs

- Check Dashboard sync status while online — should read "synced", not stuck
  on "pending".
- Disconnect Wi-Fi briefly, trigger an alert, reconnect — within ~30 seconds
  the alert should sync (pending counter drops).

## 10. Settings — Detection

- Auto-arm toggle with a custom time and 12hr/24hr format — set it, reload
  the page, confirm the time and format persisted correctly and didn't get
  mixed up with the real Armed/Unarmed state.

## 11. Settings — Cameras

- Two separate dropdowns, "Camera 1" and "Camera 2", each listing detected
  devices. Selecting the same device in both should be blocked/warned
  (conflict check).

## 12. Settings — Users

- Google-linked account: confirm the optional password prompt behaves as
  expected (doesn't force a password if not set).

## 13. General

- No emojis anywhere in Alerts settings.
- 12hr/24hr toggle affects displayed times consistently across Dashboard,
  Alerts History, and Settings.

---

Report anything that doesn't match the above — screenshot + which step
number is the fastest way to flag it.
