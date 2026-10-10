; Tally Party App - Windows installer (built automatically by GitHub Actions)
#define AppName "Tally Party App"
#define AppVersion "1.0"

[Setup]
AppId={{6E3C2B7A-4D1F-4C55-9A3E-7B2D1F0A9C11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Tally Party App
DefaultDirName={autopf}\Tally Party App
DefaultGroupName=Tally Party App
DisableProgramGroupPage=yes
; Installs for the current user only, so no administrator rights are needed.
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=TallyPartyApp-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\TallyPartyApp.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start with Windows and keep syncing with Tally in the background"; GroupDescription: "Background syncing:"

[Files]
Source: "dist\TallyPartyApp.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Tally Party App"; Filename: "{app}\TallyPartyApp.exe"
Name: "{group}\Uninstall Tally Party App"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Tally Party App"; Filename: "{app}\TallyPartyApp.exe"; Tasks: desktopicon
Name: "{userstartup}\Tally Party App"; Filename: "{app}\TallyPartyApp.exe"; Parameters: "--background"; Tasks: startup

[Run]
Filename: "{app}\TallyPartyApp.exe"; Description: "Open Tally Party App now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM TallyPartyApp.exe /F"; Flags: runhidden; RunOnceId: "StopApp"
