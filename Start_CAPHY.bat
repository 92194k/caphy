@echo off
REM ============================================================
REM  CAPHY Backup Launcher
REM  Double-click this to start CAPHY without opening VS Code,
REM  a terminal window, or any code editor. Use this if the
REM  packaged CAPHY.exe has a problem right before/during the
REM  defense - this runs the exact same app from source instead,
REM  and it looks just as clean: no code editor, no visible
REM  console window, just the CAPHY window opening.
REM ============================================================
cd /d "%~dp0"
call venv\Scripts\activate.bat
start "" pythonw desktop_launcher.py
