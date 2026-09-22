# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for CAPHY Desktop (.exe).
#   Build:   pyinstaller CAPHY.spec
#   Output:  dist/CAPHY/CAPHY.exe   (a one-FOLDER build - ship the whole
#            dist/CAPHY folder, or wrap it in an installer)
#
# One-folder (not one-file) on purpose: CAPHY pulls in PyTorch, OpenCV,
# ultralytics and aiortc, which are large and unreliable to cram into a
# single self-extracting .exe. One-folder builds start faster and are far
# less flaky. See BUILD_EXE.md for the full walkthrough + caveats.

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# Heavy packages: pull in their code, data files and dynamic libs wholesale
# so nothing is missed at runtime.
for pkg in ["ultralytics", "torch", "torchvision", "cv2", "av", "aiortc",
            "firebase_admin", "google.cloud.firestore", "google.cloud.storage",
            "grpc", "pygame"]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# CAPHY's own bundled read-only files (model weights, web UI, sounds,
# voice intents, and the credentials the app reads at startup).
datas += [
    ('yolov8n.pt', '.'),
    ('web/templates', 'web/templates'),
    ('web/static', 'web/static'),
    ('assets', 'assets'),
    # models/caphy_person_best.pt is the ACTUAL detection model
    # (config.py's YOLO_MODEL) - yolov8n.pt above is just the base/generic
    # weights kept for reference. Bundle the real one so the built .exe
    # doesn't silently fall back to worse detection on a machine that
    # doesn't happen to have this file lying around outside the bundle.
    ('models/caphy_person_best.pt', 'models'),
    # voice/intents.json REMOVED 2026-08-31 - that folder (a legacy,
    # unused Vosk-based voice prototype) was deleted; voice/chat is now
    # phone-side ("Ask CAPHY") talking to assistant_ai/ over Groq, which
    # needs no bundled data file. Leaving this line in would make the
    # build fail outright (PyInstaller errors on a missing source path).
    ('firebase_key.json', '.'),
    ('caphy_keys.json', '.'),
]

hiddenimports += [
    'flask', 'jinja2', 'webview',
    'firebase_admin.messaging', 'firebase_admin.auth',
    'firebase_admin.firestore', 'firebase_admin.storage',
    'engineio.async_drivers.threading',
]

block_cipher = None

a = Analysis(
    ['caphy_desktop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CAPHY',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX often breaks torch/opencv DLLs - keep off
    console=False,             # no visible terminal - caphy_desktop.py opens a
                               # pywebview window (falling back to a browser tab)
                               # instead, so a console window isn't needed once the
                               # app has been test-run once via `python caphy_desktop.py`
                               # with console=True and confirmed to start cleanly.
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/caphy_icon.ico' if __import__('os').path.exists('assets/caphy_icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CAPHY',
)
