# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for CAPHY Desktop App.
# Usage: pyinstaller CAPHY.spec
# Output: dist/CAPHY.exe

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Include YOLOv8 model
        ('yolov8n.pt', '.'),
        # Include Vosk models (English + Tagalog)
        ('models/vosk-en', 'models/vosk-en'),
        ('models/vosk-tl', 'models/vosk-tl'),
        # Include web templates and static files
        ('web/templates', 'web/templates'),
        ('web/static', 'web/static'),
    ],
    hiddenimports=[
        'flask',
        'pywebview',
        'cv2',
        'ultralytics',
        'vosk',
        'firebase_admin',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='CAPHY',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window (GUI app only)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/caphy_icon.ico',  # If you have an icon file, put it in assets/
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CAPHY'
)
