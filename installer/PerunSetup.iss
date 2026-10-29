#define MyAppName "Perun"
#define MyAppVersion "0.8.46"
#define MyAppPublisher "Perun in The Machine"
#define MyAppURL "https://github.com/azagor/perun"
#define MyAppExeName "Perun.cmd"

[Setup]
AppId={{PERUN-APP-7C3A-4B2E-9D1E-8F5A2B3C4D5E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Perun
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=PerunSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\web\assets\perun.ico
UninstallDisplayIcon={app}\web\assets\perun.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "Full installation"; Flags: iscustom

[Components]
Name: "perun"; Description: "Perun dashboard and orchestrator"; Types: full; Flags: fixed
Name: "python"; Description: "Python 3.12.10 (required if Python 3.10+ is not installed)"; Types: full
Name: "git"; Description: "Git for Windows (required for project discovery)"; Types: full
Name: "nodejs"; Description: "Node.js 22.14.0 LTS (required by OpenCode CLI)"; Types: full
Name: "opencode"; Description: "OpenCode CLI"; Types: full
Name: "ollama"; Description: "Ollama (for semantic memory)"; Types: full

[Files]
Source: "..\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs; Excludes: "\.git,\.gitignore,perun.md,AGENTS.md,README.md,docs,.perun,*.pyc,__pycache__,.pytest_cache,node_modules,installer\Output,installer\*.exe,installer\*.iss,installer\certs"
Source: "install-prereqs.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "uninstall.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "..\web\assets\perun.ico"; DestDir: "{app}\web\assets"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Perun"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\web\assets\perun.ico"
Name: "{autodesktop}\Perun"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\web\assets\perun.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Run]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\installer\install-prereqs.ps1"" -PerunDir ""{app}""{code:GetPrereqSwitches}"; StatusMsg: "Installing dependencies and finishing Perun setup..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\installer\uninstall.ps1"" -PerunDir ""{app}"""; RunOnceId: "PerunUninstall"; Flags: runhidden waituntilterminated

[Code]
function GetPrereqSwitches(Param: String): String;
var
  Switches: String;
begin
  Switches := '';
  if WizardIsComponentSelected('python') then
    Switches := Switches + ' -InstallPython';
  if WizardIsComponentSelected('git') then
    Switches := Switches + ' -InstallGit';
  if WizardIsComponentSelected('nodejs') then
    Switches := Switches + ' -InstallNodeJS';
  if WizardIsComponentSelected('opencode') then
    Switches := Switches + ' -InstallOpencode';
  if WizardIsComponentSelected('ollama') then
    Switches := Switches + ' -InstallOllama';
  Result := Switches;
end;
