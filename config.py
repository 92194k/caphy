# CAPHY settings - all tunable values in one place.

# ---- Camera ----
CAMERA_INDEX  = 0
AUTO_SCAN_CAMERAS = True     # auto-find any plugged-in / built-in camera (USB, laptop, DroidCam)
MAX_CAMERAS       = 2        # system is limited to 2 cameras (CCTV grid)
EXTRA_CAMERAS     = [        # network (WiFi/IP) cameras added by URL
    # V380 Pro removed: it has no ONVIF/RTSP option, so it cannot stream to CAPHY.
    # Use a phone as the 2nd camera instead (IP Webcam app) -> paste its URL here, e.g.:
    # "http://192.168.1.6:4747/video",
]
CAMERAS           = [0, 1]   # used ONLY if AUTO_SCAN_CAMERAS = False (manual list)
CAMERA_NAMES      = ["Cam 0", "Cam 1"]   # display names per camera slot - rename freely (e.g. "Front Gate")
FRAME_WIDTH   = 640    # safe, widely-supported resolution (480x360 is non-standard and breaks some cams)
FRAME_HEIGHT  = 480

# ---- Factor 1: Motion (pixel-change via MOG2 background subtraction) ----
MOTION_MIN_AREA    = 1500
MOG2_HISTORY       = 500
MOG2_VAR_THRESHOLD = 40
MOTION_BLUR        = 5

# ---- Factor 2: Person (YOLOv8) ----
YOLO_MODEL      = "yolov8n.pt"
PERSON_CLASS_ID = 0
PERSON_CONF     = 0.50

# ---- Threat tiers (distance in meters) ----
DISTANCE_K      = 900.0
TIER1_MIN_DIST  = 4.5     # farther than this -> Tier 1
TIER3_MAX_DIST  = 2.5     # closer than this  -> Tier 3

# Stability. A YOLO box jitters a few percent every frame even when the person
# is standing still, which used to make the tier flicker (and fire the siren at
# random near the Tier 2/3 line).
TIER_SMOOTHING  = 0.35    # 0..1 - how much each new reading counts.
                          # LOWER  = steadier tier, reacts slower
                          # HIGHER = reacts faster, more flicker
TIER_HYSTERESIS = 0.12    # a threshold must be crossed by this fraction before
                          # the tier changes. RAISE if the tier still wobbles;
                          # LOWER if Tier 3 / the siren triggers too late.

# ---- Tier behavior (redesigned) ----
SNAPSHOT_TIERS   = [1, 2, 3]   # tiers that save a snapshot (used as the app image)
RECORD_TIERS     = [2, 3]      # tiers that record video until the person leaves
SIREN_TIERS      = [3]         # tiers that sound the siren
PRESENCE_GRACE_SEC = 1.5       # keep recording this long after the person disappears
HIGHEST_SECURITY = False       # if True, ANY confirmed person triggers the full Tier-3 response

# ---- Evaluation / data collection (thesis Chapter 4) ----
# Turn ON while running a test scenario, OFF for normal use.
# Every motion event is recorded - including the ones YOLO rejected, which is
# the proof that two-factor validation cuts false alarms.
EVAL_LOGGING      = False
EVAL_SESSION      = ""   # label this run, e.g. "daylight-person" / "night-cat"
EVAL_GROUND_TRUTH = ""   # what SHOULD happen: "person", "no_person", or ""
                         # Set this and CAPHY can compute precision/recall.

# ---- Database & captures ----
DB_PATH            = "caphy.db"
CAPTURES_DIR       = "captures"
ALERT_COOLDOWN_SEC = 5.0

# ---- Cloud sync ----
CLOUD_DIR         = "cloud_sim"
COMPRESSED_DIR    = "captures_compressed"
IMAGE_QUALITY     = 60
SYNC_INTERVAL_SEC = 15

# ---- Night vision (software CLAHE) ----
NIGHT_VISION_AUTO = True
NIGHT_LOW_LIGHT   = 70
CLAHE_CLIP        = 2.5
CLAHE_TILE        = 8
NIGHT_GAMMA       = 1.4

# ---- Voice ----
# Voice lives ONLY in the phone app. The app runs speech-to-text on the device,
# POSTs the words to /api/voice, and speaks the reply with its own TTS.
# This PC has no microphone loop and no text-to-speech - nothing to configure.
# The phrases CAPHY understands are in voice/intents.json, served to the app
# by GET /api/intents.

# ---- Mobile push (Firebase) ----
FIREBASE_KEY  = "firebase_key.json"
PUSH_TOPIC    = "caphy_alerts"
PUSH_MIN_TIER = 1             # send a push from Tier 1 up (Tier 1 now alerts too)

# ---- Display ----
SHOW_MOTION_MASK = False

# ---- Battery / power-save ----
BATTERY_LOW       = 20     # percent at/below this = low battery warning
POWER_SAVE_SKIP   = 2      # in power-save mode, run YOLO every Nth frame

# ---- Performance ----
PERSON_EVERY_N = 8     # run YOLO every Nth frame (higher = smoother video, less CPU)
PERSON_IMGSZ   = 256   # YOLO input size (lower = much faster; 320 fastest, 640 most accurate)
CAP_BUFFERSIZE = 1     # keep only the newest frame (kills lag/delay build-up)
JPEG_QUALITY   = 55    # MJPEG stream quality 1-100 (low = lightest stream)

# ---- Firebase Storage (real photos on phone) ----
FIREBASE_BUCKET = "caphy-c6b77.firebasestorage.app"    # e.g. "caphy-xxxx.appspot.com"  (from Firebase Console -> Storage)

