; ForTest 0.2.13 纯原生 Windows x64 安装器。
#ifndef MyAppVersion
  #define MyAppVersion "0.2.13"
#endif

#define MyAppName "ForTest"
#define MyAppExeName "ForTest.exe"

[Setup]
AppId={{6A87C65B-9717-487B-92A6-B7073540BEB4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=ForTest
AppComments=ForTest 测试与部署工具纯原生 Windows x64 桌面端
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany=ForTest
VersionInfoDescription=ForTest 安装程序
VersionInfoProductName=ForTest
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
UsePreviousAppDir=no
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist\installer
OutputBaseFilename=ForTest-Windows-x64-Setup-{#MyAppVersion}
SetupIconFile=assets\ForTester.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=ForTest.exe,QAQ.exe,ForTester.exe,PRDtoCASE.exe
RestartApplications=no
MinVersion=10.0.17763

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\ForTest\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 使用原 AppId 覆盖升级，但程序迁移到 ForTest 目录；用户数据目录不在此路径内。
Type: filesandordirs; Name: "{localappdata}\Programs\QAQ"
Type: files; Name: "{autodesktop}\QAQ.lnk"
Type: filesandordirs; Name: "{userprograms}\QAQ"
Type: filesandordirs; Name: "{localappdata}\Programs\ForTester"
Type: files; Name: "{autodesktop}\ForTester.lnk"
Type: filesandordirs; Name: "{userprograms}\ForTester"
Type: filesandordirs; Name: "{localappdata}\Programs\PRDtoCASE"
Type: files; Name: "{autodesktop}\PRDtoCASE.lnk"
Type: filesandordirs; Name: "{userprograms}\PRDtoCASE"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  DeleteUserDataOnUninstall: Boolean;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('ForTest 仅支持 64 位 Windows。', mbError, MB_OK);
    Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  DeleteUserDataOnUninstall := False;
  if not UninstallSilent then
  begin
    DeleteUserDataOnUninstall := MsgBox(
      '是否同时删除 ForTest 的全部用户数据？' + #13#10 + #13#10 +
      '选择“否”将保留配置、任务记录和生成文件，重新安装后可继续使用。' + #13#10 +
      '选择“是”将永久删除这些数据以及已保存的连接凭据。',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2
    ) = IDYES;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { 开机启动项不属于用户业务数据，卸载时始终清理，避免残留失效路径。 }
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      '{#MyAppName}'
    );
    if DeleteUserDataOnUninstall then
    begin
      Exec(
        ExpandConstant('{app}\{#MyAppExeName}'),
        '--delete-user-data',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode
      );
    end;
  end;
end;
