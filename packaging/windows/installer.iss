; LiDAR Tree Inventory - Windows installer (Inno Setup 6).
; Built by .github/workflows/release.yml:
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\..\dist\lidar-tree-inventory packaging\windows\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\lidar-tree-inventory"
#endif
#define AppName "LiDAR Tree Inventory"
#define AppExe "fip.exe"

[Setup]
AppId={{6F1C2E4B-8A2D-4C5E-9B7A-3D1F0E2A9C47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=lidar-historic-trees
AppPublisherURL=https://github.com/band72/lidar-historic-trees
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; per-user install by default (no admin); the dialog offers "all users"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=LiDAR-Tree-Inventory-{#AppVersion}-windows-x64-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
; lzma2/ultra64 ran out of memory on the ~800 MB bundle (ISCC is 32-bit); compress in a
; separate 64-bit process with a 64 MB dictionary instead
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMADictionarySize=65536
LZMANumBlockThreads=4
WizardStyle=modern
ChangesEnvironment=yes
LicenseFile=..\..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "addtopath"; Description: "Add the &fip command to PATH (use it from any terminal)"; GroupDescription: "Command line:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{userdocs}"; Comment: "Start the web app"
Name: "{group}\{#AppName} command prompt"; Filename: "{cmd}"; Parameters: "/k ""set PATH={app};%PATH% && fip --help"""; WorkingDir: "{userdocs}"; Comment: "Terminal with the fip command"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "self-test --skip-app"; StatusMsg: "Checking the installation..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; WorkingDir: "{userdocs}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
const
  EnvKeyUser = 'Environment';
  EnvKeyMachine = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function EnvRoot: Integer;
begin
  if IsAdminInstallMode then Result := HKEY_LOCAL_MACHINE else Result := HKEY_CURRENT_USER;
end;

function EnvKey: String;
begin
  if IsAdminInstallMode then Result := EnvKeyMachine else Result := EnvKeyUser;
end;

function PathContains(const Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToPath(const Dir: String);
var Paths: String;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then Paths := '';
  if PathContains(Paths, Dir) then exit;
  if (Paths <> '') and (Paths[Length(Paths)] <> ';') then Paths := Paths + ';';
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(const Dir: String);
var Paths, Rest, Part, Kept: String; P: Integer;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then exit;
  Rest := Paths + ';';
  Kept := '';
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    Part := Copy(Rest, 1, P - 1);
    Delete(Rest, 1, P);
    if (Part <> '') and (CompareText(Part, Dir) <> 0) then
    begin
      if Kept <> '' then Kept := Kept + ';';
      Kept := Kept + Part;
    end;
  end;
  if Kept <> Paths then RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Kept);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
