#define MyAppName "RelayDeck Local"
#define MyAppVersion GetEnv("RELAYDECK_VERSION")

[Setup]
AppId={{DC58C1EF-341B-4352-891B-07C406B34E12}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\RelayDeck Local
DefaultGroupName=RelayDeck Local
OutputBaseFilename=RelayDeck-Setup-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\build\windows\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\RelayDeck Local"; Filename: "{app}\RelayDeck.exe"
Name: "{autodesktop}\RelayDeck Local"; Filename: "{app}\RelayDeck.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\RelayDeck.exe"; Description: "Launch RelayDeck Local"; Flags: nowait postinstall skipifsilent
