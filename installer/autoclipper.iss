; Inno Setup script - builds AutoClipper-Setup.exe from dist\AutoClipper
#define AppName "Auto Clipper"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F1B7C2E-4D5A-4E2B-9C1F-AC11B0C1A9E1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Auto Clipper
DefaultDirName={localappdata}\Programs\AutoClipper
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AutoClipper-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\AutoClipper.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng ngoài Desktop"; GroupDescription: "Biểu tượng:"

[Files]
Source: "..\dist\AutoClipper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\AutoClipper.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\AutoClipper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoClipper.exe"; Description: "Mở Auto Clipper"; Flags: nowait postinstall skipifsilent
