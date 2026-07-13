# CAPHY settings - all tunable values in one place.

# ---- Camera ----
CAMERA_INDEX  = 0
FRAME_WIDTH   = 640
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

# ---- Tier behavior (redesigned) ----
SNAPSHOT_TIERS   = [1, 2, 3]   # tiers that save a snapshot (used as the app image)
RECORD_TIERS     = [2, 3]      # tiers that record video until the person leaves
SIREN_TIERS      = [3]         # tiers that sound the siren
PRESENCE_GRACE_SEC = 1.5       # keep recording this long after the person disappears
HIGHEST_SECURITY = False       # if True, ANY confirmed person triggers the full Tier-3 response

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

# ---- Voice (Vosk) ----
VOSK_MODEL_PATH   = "models/vosk-en"
VOICE_SAMPLE_RATE = 16000

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
PERSON_EVERY_N = 2     # run YOLO every Nth frame (1 = every frame, 2 = half as often = ~2x FPS)

# ---- Firebase Storage (real photos on phone) ----
FIREBASE_BUCKET = "caphy-c6b77.firebasestorage.app"    # e.g. "caphy-xxxx.appspot.com"  (from Firebase Console -> Storage)

# ---- Bilingual voice (English + Tagalog) ----
VOSK_MODEL_EN = "models/vosk-en"   # small English model (you already have this)
VOSK_MODEL_TL = "models/vosk-tl"   # unzip vosk-model-tl-ph-generic-0.6 here