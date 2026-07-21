@echo off
REM ============================================================
REM  Build CAPHY into a desktop app (dist\CAPHY\CAPHY.exe)
REM  Run this from the CAPHY folder with your venv ACTIVATED:
REM      venv\Scripts\activate
REM      build_exe.bat
REM ============================================================

echo [CAPHY] Installing PyInstaller (if needed)...
pip install pyinstaller

echo [CAPHY] Cleaning old build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [CAPHY] Building (this takes several minutes and needs internet the first time)...
pyinstaller CAPHY.spec

echo.
echo [CAPHY] Done. Your app is here:
echo     dist\CAPHY\CAPHY.exe
echo.
echo Ship the ENTIRE dist\CAPHY folder (not just the .exe).
pause
