# CAPHY Camera Setup Guide

## Files Created

1. **camera_detector.py** - Scans for local & network cameras
2. **camera_routes.py** - Flask API endpoints for camera selection
3. **camera_selector.html** - Web UI for camera selection
4. **camera_config.json** - Saved camera settings (auto-created)

---

## Integration Steps

### Step 1: Update your app.py

Add these imports at the top:

```python
from camera_detector import get_camera_source
from camera_routes import register_camera_routes
```

Then register camera routes (after creating Flask app):

```python
app = Flask(__name__)
# ... your other setup ...

# Register camera routes
register_camera_routes(app)

# ... rest of your code ...
```

### Step 2: Add camera selector route

Add this route to serve the camera selector page:

```python
@app.route('/camera-setup')
def camera_setup():
    return open('camera_selector.html').read()
```

### Step 3: Use selected camera in detection

Replace your camera initialization with:

```python
from camera_detector import get_camera_source

# Instead of: cap = cv2.VideoCapture(0)
camera_source = get_camera_source()
cap = cv2.VideoCapture(camera_source)
```

---

## How to Use

### For Respondents (Homeowners & Lab Personnel)

1. **Open camera setup page:**
   - Go to: `http://laptop-ip:5000/camera-setup`

2. **Scan for cameras:**
   - Click "Scan for Cameras" button
   - System detects:
     - Local cameras (USB, built-in)
     - V380 at 161.248.58.9

3. **Select camera:**
   - Click radio button to select camera
   - Camera name appears in "Selected Camera"

4. **Test connection:**
   - Click "✓ Test Connection"
   - Confirms camera is working

5. **Save:**
   - Click "✓ Confirm & Save"
   - Camera setting stored in `camera_config.json`
   - System reboots with selected camera

### Offline Operation

- Selected camera is saved in `camera_config.json`
- If system restarts offline, it uses saved camera automatically
- No internet needed for camera selection to work

---

## V380 Camera Setup

Your V380 at **161.248.58.9** will auto-detect if:

- V380 is powered on
- Connected to same network (WiFi/LAN)
- Laptop can reach 161.248.58.9

### If V380 doesn't appear:

1. Check V380 is powered on
2. Check V380 is on same network
3. Test manually: Open browser → `http://161.248.58.9`
4. If works in browser, system will detect it

---

## API Endpoints

### GET /api/cameras
Returns all available cameras:
```json
{
  "local": [{"id": 0, "name": "Local Camera 0", ...}],
  "network": [{"url": "rtsp://...", "name": "V380 IP Camera", ...}],
  "total": 1
}
```

### GET /api/cameras/saved
Returns currently saved camera

### POST /api/cameras/select
Select and save camera:
```json
{"camera": "rtsp://161.248.58.9:554/stream"}
```

### POST /api/cameras/test
Test camera connection:
```json
{"camera": "rtsp://161.248.58.9:554/stream"}
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No cameras detected | Check camera connections, refresh page |
| V380 not showing | Confirm V380 power & network, restart V380 |
| Test connection fails | Check network/WiFi, try different RTSP URL |
| Camera selection won't save | Check file permissions on `camera_config.json` |
| Offline camera doesn't work | Verify `camera_config.json` exists & is readable |

---

## Default Behavior

If no camera selected:
- System falls back to Camera 0 (first available)
- Prompt user to use camera selector to choose

---

## For Survey Respondents

**Simple instructions to give:**

1. Visit: `http://[laptop-ip]:5000/camera-setup`
2. Click "Scan for Cameras"
3. Select your camera from the list
4. Click "✓ Test Connection" to verify
5. Click "✓ Confirm & Save" to use this camera

Done! System will now use selected camera.
