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
FRAME_WIDTH   = 1920   # native 1080p capture - sharper snapshots/recordings.
FRAME_HEIGHT  = 1080   # NOTE: if this is changed again, DISTANCE_K below MUST be
                        # rescaled by the same ratio (new_height / 720), since
                        # distance = DISTANCE_K / box_height_px - a taller frame
                        # gives every person a taller box in pixels at the SAME
                        # real-world distance, which would otherwise make
                        # everyone read as closer than they really are and skew
                        # every Tier 1/2/3 threshold. Test actual detection FPS
                        # after this change - this is more CPU-expensive per
                        # frame for YOLO/motion than 960x720 was.

# ---- Live-view streaming (MJPEG /video_feed AND WebRTC - see
# web/server.py's Worker._store(), the one place both paths get their
# frame from) ----
# Deliberately separate from FRAME_WIDTH/HEIGHT above: those feed YOLO/
# OpenCV detection and the 3-Tier distance estimate (bounding-box height
# in pixels), which stays at full resolution regardless of this setting -
# lowering FRAME_WIDTH/HEIGHT would need every Tier 1/2/3 distance
# threshold recalibrated, since a person's box gets fewer pixels tall at
# a lower capture resolution. STREAM_WIDTH/HEIGHT only affects the copy
# of the (already-detected, already-annotated) frame that gets encoded
# and sent to viewers - detection itself never sees this resolution.
#
# 640x360 chosen specifically: keeps a 16:9 aspect ratio for a
# mobile-friendly picture (960x720 is 4:3, so this letterboxes/crops
# slightly - a known, accepted tradeoff, not a bug), and roughly a 3x
# reduction in pixel count vs 960x720 at the same JPEG quality, which is
# the actual lever for cutting per-frame bytes on a slow mobile-data
# connection - this is the fix for "video lags when phone is on mobile
# data and laptop is on WiFi", not a detection-accuracy change.
#
# Set STREAM_WIDTH/HEIGHT = FRAME_WIDTH/HEIGHT (960/720) to disable
# downscaling entirely and stream at full detection resolution, e.g. for
# an A/B comparison while gathering Chapter 4 latency numbers.
STREAM_WIDTH  = 640
STREAM_HEIGHT = 360

# ---- Factor 1: Motion (pixel-change via MOG2 background subtraction) ----
MOTION_MIN_AREA    = 1500
MOG2_HISTORY       = 500
MOG2_VAR_THRESHOLD = 40
MOTION_BLUR        = 5

# ---- Factor 2: Person (YOLOv8) ----
YOLO_MODEL      = "models/caphy_person_best.pt"   # custom-trained CAPHY person model (committed to git so teammates get it)
PERSON_CLASS_ID = 0
PERSON_CONF     = 0.50

# ---- Threat tiers (distance in meters) ----
DISTANCE_K      = 1350.0  # = 900.0 * (1080/720), rescaled for the FRAME_HEIGHT=1080 bump above - keeps the same real-world meter readings as before
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

# ---- Tier behavior (armed/disarmed x tier response table) ----
# Disarmed:
#   Tier 1: snapshot + log only (no video)
#   Tier 2: snapshot + notification; a short 3-5s clip ONLY if the person
#           remains in view that long (see DWELL_BEFORE_RECORD_SEC below) -
#           someone briefly passing through only ever gets a snapshot
#   Tier 3: snapshot + siren + continuous video (until person leaves) + notification
# Armed:
#   Tier 1: snapshot + short video clip + urgent notification
#   Tier 2: snapshot + continuous video + siren
#   Tier 3: snapshot + siren + continuous video (until person leaves) + urgent notification (max response)
SNAPSHOT_TIERS   = [1, 2, 3]   # tiers that save a snapshot (used as the app image)
RECORD_TIERS     = [1, 2, 3]   # tiers that CAN record video at all - armed/disarmed narrows this further per-tier (see AlertManager._recording_allowed_for_tier); Tier 1 disarmed still never actually records
SIREN_TIERS      = [3]         # tiers that sound the siren when DISARMED - armed mode adds Tier 2 (see server.py's siren gate)
PRESENCE_GRACE_SEC = 1.5       # keep recording this long after the person disappears (for Tier 2/3)
TIER2_RECORD_DURATION = 5      # Tier 2 in disarmed mode: max recording duration once it starts (seconds)
TIER1_ARMED_RECORD_DURATION = 4  # Tier 1 in armed mode: fixed short clip length (seconds) - not continuous

# How long (seconds) a person must remain continuously in view before a
# recording is allowed to start at all, for tiers where recording is
# conditional on sustained presence (Tier 2 disarmed, per the table above).
# Someone just walking past the camera - in frame for barely a second or
# two - only ever gets a snapshot, never a video, which is most of what
# used to fill storage with near-identical short clips of nothing.
DWELL_BEFORE_RECORD_SEC = 2.5

# Once a recording finishes for an ongoing visit, how long to wait before
# allowing ANOTHER recording to start for what's effectively the same
# presence (tier hasn't escalated) - stops a person who lingers near a
# recording-duration boundary from generating a new clip every few seconds.
# An escalation to a higher tier always bypasses this and starts recording
# immediately regardless of cooldown, since a bigger threat should never be
# silently throttled.
RECORDING_COOLDOWN_SEC = 45

# While a person remains continuously present and alerts keep being
# generated for them (each one already rate-limited by ALERT_COOLDOWN_SEC),
# only save another SNAPSHOT at most this often - not on every single
# alert. Keeps the alert history from filling with dozens of near-identical
# photos of someone who's just standing/sitting in frame.
PRESENCE_SNAPSHOT_INTERVAL_SEC = 45

# Kept for backward compatibility with Worker/_set_camera/_emergency code paths
# that still reference config.HIGHEST_SECURITY as their default. The actual
# "every tier behaves like Tier 3" behavior now lives in the ARM button /
# TierEngine armed mode (see detection/tier_engine.py) - this flag is no
# longer surfaced in Settings and should stay False.
HIGHEST_SECURITY = False

# ---- Auto-arm at night ----
# Enable the switch in Settings -> Detection. The system arms itself at
# AUTO_ARM_START_HOUR and disarms at AUTO_ARM_END_HOUR (24-hour clock).
AUTO_ARM_START_HOUR = 22   # 10:00 PM
AUTO_ARM_END_HOUR   = 6    # 6:00 AM

# Seconds after arming during which no alert fires - lets you arm while still
# in view without instantly triggering yourself.
ARM_GRACE_SEC = 8

# Seconds after DISARMING during which Tier 3 does not escalate (no siren,
# no urgent push) - lets you walk right past the camera after turning the
# system off without it treating you, the owner, as an intruder. A Tier 3
# snapshot + log still happens (so the alert history stays complete), just
# without the alarm response. Separate from ARM_GRACE_SEC above, which
# covers the opposite moment (just armed, still in view).
DISARM_GRACE_SEC = 45

# Seconds after a camera (re)starts during which detections are computed
# (so MOG2 keeps learning the background) but never saved as an alert. Without
# this, the very first frame(s) after starting the app can be flagged as
# "motion" before MOG2 has any real background to compare against, causing a
# false snapshot/alert even when nobody is there.
STARTUP_WARMUP_SEC = 3.0

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

# ---- Retention (auto-delete old alerts) ----
# Alerts (and their snapshot/video files, local AND cloud copy) older than
# this many days are deleted automatically. Applies equally to every tier -
# a Tier-1 alert and a Tier-3 alert both get the same window. Cloud copies
# are deleted alongside local ones, so Firebase Storage usage stays low
# instead of growing forever.
RETENTION_DAYS     = 30
RETENTION_ENABLED  = True

# Registry value names for the REAL target of the "Pictures"/"Videos" Windows
# libraries. Just guessing "~/Pictures" is wrong if OneDrive has redirected
# the library (Known Folder Move) or the user customized it in Explorer -
# this reads the actual configured location so files really land in
# Libraries > Pictures / Libraries > Videos, wherever that currently points.
_SHELL_FOLDER_KEY = {"Pictures": "My Pictures", "Videos": "My Video"}


def _library_target(kind):
    """The real, current folder behind Libraries > Pictures/Videos on
    Windows (accounts for OneDrive redirection). Returns None off-Windows
    or if the registry lookup fails for any reason."""
    import os
    value_name = _SHELL_FOLDER_KEY.get(kind)
    if not value_name:
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        raw, _ = winreg.QueryValueEx(key, value_name)
        return os.path.expandvars(raw)
    except Exception:
        return None


# Snapshots and recordings save into the user's real Pictures/Videos library
# so they show up in Libraries > Pictures/Videos, inside a "CAPHY" folder.
# Falls back to ~/Pictures (or ~/Videos), then to a local ./captures folder,
# ONLY if each option truly can't be created/written to - and always prints
# exactly which path was picked, so a silent fallback is never a mystery
# ("why isn't there a CAPHY folder in my Pictures?").
def _gallery_dir(kind, default):
    import os
    candidates = []
    lib_target = _library_target(kind)
    if lib_target:
        candidates.append(os.path.join(lib_target, "CAPHY"))
    candidates.append(os.path.join(os.path.expanduser("~"), kind, "CAPHY"))

    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".caphy_write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            print(f"[CAPHY] {kind} folder: {base}")
            return base
        except Exception as e:
            print(f"[CAPHY] could not use {base} ({e}) - trying next option")

    fallback = os.path.abspath(default)
    os.makedirs(fallback, exist_ok=True)
    print(f"[CAPHY] WARNING: no {kind} library location was writable - "
          f"falling back to {fallback}")
    return fallback

CAPTURES_DIR       = _gallery_dir("Pictures", "captures")   # snapshots + alert images
VIDEOS_DIR         = _gallery_dir("Videos", "captures")     # recordings
ALERT_COOLDOWN_SEC = 5.0

# ---- Siren ----
# A real alarm sound (looped, with a smooth fade in/out - no click, no
# startle). Put the .mp3 at assets/siren.mp3 next to this file. If it's
# missing, siren.py automatically falls back to a synthesized tone so the
# system still has a siren either way.
import os as _os
SIREN_SOUND_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  "assets", "siren.mp3")
SIREN_FADE_MS     = 300     # fade in/out time - smooth, not startling
SIREN_VOLUME       = 0.9    # 0.0 - 1.0

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
# Separate, higher quality specifically for saved snapshots (manual
# Live-tab snapshots AND automatic alert snapshots) - these are kept as
# evidence and re-viewed later/uploaded to Firebase, so they deserve
# noticeably better quality than the live MJPEG stream, which only ever
# needs to look good for a moment on a phone screen. 85 keeps files
# small (well under half the size of OpenCV's uncompressed-ish default
# of 95) while still being clearly readable evidence.
SNAPSHOT_JPEG_QUALITY = 85

# Tries RTSP cameras (IP cameras like Tapo) with low-latency FFmpeg options
# (TCP transport, no internal buffering) first, which can noticeably cut
# live-view delay. SAFE to leave on: Worker._open_capture() now PROBES the
# tuned connection (same first-frame-timing check already used for the
# DirectShow/MSMF webcam fallback) and automatically falls back to a plain,
# untouched connection for that camera if the tuned one fails to open or is
# slow - so a camera/network combo that doesn't like these options just
# quietly runs at default speed instead of breaking live view. (An earlier
# version of this forced the options globally with no fallback, which DID
# break live view - "stuck on Camera loading.../black screen" - on at least
# one real Tapo setup. That version is gone; this is the self-correcting
# replacement.) Set False to skip the attempt entirely for every camera.
RTSP_LOW_LATENCY = True

# ---- Firebase Storage (real photos on phone) ----
FIREBASE_BUCKET = "caphy-c6b77.firebasestorage.app"    # e.g. "caphy-xxxx.appspot.com"  (from Firebase Console -> Storage)

# ===== Firebase Configuration (B3+B4+B5) =====
FIREBASE_KEY_PATH = "firebase_key.json"
# NOTE: FIREBASE_BUCKET is defined ONCE, above (the .firebasestorage.app
# value). There used to be a second definition here ("caphy-c6b77.appspot.com")
# that silently overrode it - a duplicate is exactly the kind of thing that
# makes cloud snapshot uploads fail with a confusing "bucket not found".
# Verify the exact name in Firebase Console -> Storage (top of the page); it
# is either <project>.firebasestorage.app (newer projects) or
# <project>.appspot.com (older ones). If phone alert images don't load,
# this line is the first thing to check.
GOOGLE_CLIENT_ID = "790179915609-sujeq75jbavgsekof1vsk92prqiqpes9.apps.googleusercontent.com"

# Web API key for this Firebase project (from android/app/google-services.json
# -> client[0].api_key[0].current_key, or Firebase Console -> Project
# Settings -> General -> Web API Key). Lets the server-rendered web
# dashboard sign in against Firebase Auth's REST API - the SAME identity
# store the phone's Firebase SDK uses - instead of checking a local
# password hash. If left blank, web/server.py falls back to reading it
# straight out of google-services.json at request time.
FIREBASE_WEB_API_KEY = "AIzaSyDFRq5xH3AufJVQiXj1GrPxn3iJLVyw-oQ"

# ---- WebRTC (true live streaming from outside the LAN) ----
# TURN/STUN credentials are NEVER hardcoded here - they're fetched fresh
# from Metered.ca's REST API every time a WebRTC call starts (see
# secrets_config.py for where the API key itself lives, and
# web/webrtc_stream.py / caphy_app/lib/webrtc_call.dart for the fetch).
# This keeps the actual relay credentials short-lived and out of the
# repo entirely, and means users never configure anything by hand.
#
# Fallback STUN-only server list, used only if the Metered fetch fails
# (network hiccup, key rotated, etc.) - direct P2P still works for most
# network pairs on STUN alone, just without a relay for the harder cases.
FALLBACK_STUN_URLS = [
    "stun:stun.l.google.com:19302",
    "stun:global.stun.twilio.com:3478",
]

