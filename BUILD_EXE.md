# Packaging CAPHY as a Windows desktop app (.exe)

This turns CAPHY into `dist\CAPHY\CAPHY.exe` — double-click and the console
opens (native window if available, otherwise your browser), with cameras,
detection, cloud sync and everything running behind it.

## One-time setup

1. Open PowerShell in the CAPHY folder and activate your venv:
   ```
   venv\Scripts\activate
   ```
   Build inside the **same Python 3.12 venv** the app runs in — PyInstaller
   packages whatever is installed there.

2. Build:
   ```
   build_exe.bat
   ```
   (or manually: `pip install pyinstaller` then `pyinstaller CAPHY.spec`)

3. When it finishes, run `dist\CAPHY\CAPHY.exe`.

## What you ship

Ship the **entire `dist\CAPHY\` folder**, not just the .exe — the folder
holds the model, libraries, and UI the .exe needs. Zip it, or wrap it in an
installer (e.g. Inno Setup) later if you want a single setup file.

## Important notes / caveats

- **Size:** the build is large (roughly 2–4 GB) because of PyTorch/YOLO,
  OpenCV and aiortc. That's normal for an on-device-AI app.
- **First run of the build shows a console window on purpose** (`console=True`
  in `CAPHY.spec`) so you can see any startup errors. Once it runs cleanly,
  set `console=False` in the spec and rebuild for a windowless app.
- **Secrets are bundled.** `firebase_key.json` and `caphy_keys.json` get packed
  into the app, so anyone with the folder can extract them. That's acceptable
  for a thesis demo, but **rotate those keys** before sharing the build widely,
  and don't publish the folder publicly.
- **Runtime files** (caphy.db, logs, prefs) are written to
  `%LOCALAPPDATA%\CAPHY\`; snapshots/videos go to your Pictures/Videos as
  usual — so the app works even if installed in a read-only location.
- **Camera drivers:** the machine running the .exe still needs working
  webcam drivers (same as running from source).
- **Expect one or two iterations.** PyInstaller + PyTorch sometimes needs an
  extra `hiddenimports` entry. If the .exe starts then errors with
  `ModuleNotFoundError: X`, add `'X'` to the `hiddenimports` list in
  `CAPHY.spec` and rebuild. Paste me the error and I'll tell you exactly what
  to add.

## If the native window fails but the browser opens

That's the built-in fallback working — the app is fine, it just couldn't load
the pywebview window backend on that machine. To get a true native window,
install a WebView2 backend (`pip install pywebview[cef]` or ensure the
Microsoft Edge WebView2 runtime is present) and rebuild. The browser path is
perfectly fine for a demo.
