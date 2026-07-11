# CAPHY Core Detection Engine settings
# All tunable values live here so you only edit one file.

# ---- Camera ----
CAMERA_INDEX  = 0      # 0 = default webcam. Change if you have more than one.
FRAME_WIDTH   = 640
FRAME_HEIGHT  = 480

# ---- Factor 1: Motion (pixel-change) ----
MOTION_MIN_AREA   = 1500
MOG2_HISTORY      = 500
MOG2_VAR_THRESHOLD= 40
MOTION_BLUR       = 5

# ---- Factor 2: Person (YOLOv8) ----
YOLO_MODEL      = "yolov8n.pt"
PERSON_CLASS_ID = 0
PERSON_CONF     = 0.50

# ---- Phase 3: Threat tiers (distance in meters) ----
# distance_m = DISTANCE_K / box_height_pixels   (calibrate DISTANCE_K in daylight)
DISTANCE_K      = 900.0
TIER1_MIN_DIST  = 4.5     # farther than this -> Tier 1 (far,   log only)
TIER3_MAX_DIST  = 2.5     # closer than this  -> Tier 3 (close, snapshot+video+siren)
                          # in between        -> Tier 2 (medium, snapshot+alert)

# ---- Phase 4: Database & captures ----
DB_PATH          = "caphy.db"     # SQLite file, created automatically
CAPTURES_DIR     = "captures"     # folder for saved snapshots and videos
ALERT_COOLDOWN_SEC = 5.0          # save at most one alert every 5 seconds
VIDEO_SECONDS    = 5              # length of Tier 3 video clips
VIDEO_FPS        = 20             # frames per second for saved video
SNAPSHOT_TIERS   = [2, 3]         # tiers that save a snapshot image
VIDEO_TIERS      = [3]            # tiers that also save a video clip

# ---- Display ----
SHOW_MOTION_MASK = False

# ---- Phase 9: Night vision (software CLAHE) ----
NIGHT_VISION_AUTO = True    # True = enhance only when the room is dark
NIGHT_LOW_LIGHT   = 70      # avg brightness (0-255) below this = "low light"
CLAHE_CLIP        = 2.5     # contrast strength
CLAHE_TILE        = 8       # grid size for local contrast
NIGHT_GAMMA       = 1.4     # >1 brightens the image

# ---- Phase 8: Voice control (Vosk, offline) ----
VOSK_MODEL_PATH   = "models/vosk-en"   # unzip the small English model into this folder
VOICE_SAMPLE_RATE = 16000

# ---- Phase 6: Storage Manager / cloud sync ----
CLOUD_DIR         = "cloud_sim"            # simulated cloud folder (swap for Firebase later)
COMPRESSED_DIR    = "captures_compressed"  # where compressed snapshots go before upload
IMAGE_QUALITY     = 60                      # JPEG quality for uploads (0-100, lower = smaller)
SYNC_INTERVAL_SEC = 15                      # how often to try syncing (seconds)

# ---- Phase 7: Mobile push (Firebase) ----
FIREBASE_KEY  = "firebase_key.json"   # service-account key in the CAPHY folder
PUSH_TOPIC    = "caphy_alerts"        # the topic the phone app subscribes to
PUSH_MIN_TIER = 2                     # send a push for Tier 2 and Tier 3