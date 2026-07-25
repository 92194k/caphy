; ============================================================
;  CAPHY Windows Installer  (Inno Setup script)
;  Turns the PyInstaller build (dist\CAPHY) into a single
;  CAPHY-Setup.exe that installs the app with shortcuts + icon.
;
;  HOW TO USE:
;   1. Build the app first:   pyinstaller CAPHY.spec   (makes dist\CAPHY\)
;   2. Install Inno Setup (free): https://jrsoftware.org/isdl.php
;   3. Open this file in Inno Setup and click Build > Compile
;      (or run:  iscc installer.iss )
;   4. Output: Output\CAPHY-Setup.exe  ->  that's the file you share.
; ============================================================

#define AppName "CAPHY"
#define AppVersion "1.0.0"
#define AppPublisher "CAPHY Team"
#define AppExe "CAPHY.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=CAPHY-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; the icon shown for the installer itself and in Add/Remove Programs
SetupIconFile=assets\caphy_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
; CAPHY needs a normal user install location it can write near (LOCALAPPDATA
; is used at runtime), so no admin is strictly required:
PrivilegesRequiredOverridesAllowed=dialog

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Bundle the ENTIRE PyInstaller one-folder build.
Source: "dist\CAPHY\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Start Menu + optional desktop shortcut, both using the app's own icon.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Offer to launch CAPHY right after install.
Filename: "{app}\{#AppExe}"; Description: "Launch CAPHY now"; Flags: nowait postinstall skipifsilent
