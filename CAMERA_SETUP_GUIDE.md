# CAPHY Camera Setup Guide

(Rewritten 2026-08-31 to match the actual running system - the previous
version described an older prototype flow, `camera_selector.html` at the
repo root plus `camera_routes.py`, that is no longer used. That prototype
also referenced a specific V380 IP camera at a fixed survey-site IP
address, which is not part of the current design; `config.py` explicitly
notes it was removed because it has no ONVIF/RTSP option CAPHY can use.)

## How camera selection actually works today

CAPHY supports up to 2 cameras at once (`config.MAX_CAMERAS`), each
independently detected, named, and swapped without restarting the app.
Only two kinds of camera are supported: a USB webcam, the laptop's
built-in camera, or a second phone acting as a camera via a companion
IP-camera app - no other IP/network camera brand or model is supported.

## Where to pick cameras

1. Open the web dashboard and log in.
2. Go to **Settings -> Cameras**. The available-camera list loads
   automatically (calls `GET /api/cameras/available`) - no button press
   needed. Press **Rescan** if you plug something in after the page is
   already open.
3. Select up to `MAX_CAMERAS` (2) cameras and confirm. This calls
   `POST /api/cameras/select` with `{"sources": [...]}` and restarts the
   camera workers on the new selection immediately - no app restart
   needed.
4. Your selection is saved to local preferences and reused automatically
   on the next startup, including with no internet connection.

There is also a standalone `/camera-setup` page (served from
`web/templates/camera_selector.html`) with the same basic
scan/select/test flow, useful for a quick plug-and-play check outside the
main dashboard.

## API Endpoints (current)

### GET /api/cameras/available
Returns every camera the system can currently offer:
```json
{"cameras": [{"source": 0, "label": "Camera 0", "active": true}],
 "max": 2}
```

### POST /api/cameras/select
Body: `{"sources": [0, 1]}` (integers for device indices, strings for a
network camera URL) - applies the selection and restarts workers.

### POST /api/camera/<id>/name
Rename a camera slot: `{"name": "Front Gate"}`.

## Adding a phone as a second camera

1. Install an IP-camera app on the phone (e.g. IP Webcam) and start its
   stream.
2. Note the stream URL it shows (e.g. `http://192.168.1.6:4747/video`).
3. Add that URL to `config.py`'s `EXTRA_CAMERAS` list.
4. Restart the CAPHY server, then select it from Settings -> Cameras like
   any other camera.

## Troubleshooting

| Problem | Solution |
|---|---|
| No cameras detected | Check camera connections/permissions, press Rescan |
| A camera shows the wrong/generic name | Press Rescan to refresh the device-name cache, or rename it manually via the app |
| Selection doesn't take effect | Check the server console for an error from `restart_workers()` |
| Phone camera won't connect | Confirm the phone and laptop are on the same Wi-Fi, and the IP-camera app's stream URL is correct in `EXTRA_CAMERAS` |
