; Samurai Dealer Research のインストーラー（Inno Setup 6）
; ビルド: ISCC.exe /DAppVersion=1.1.0 installer\SamuraiDealerResearch.iss
; - 管理者権限は不要（利用者ごとに %LOCALAPPDATA%\Programs へ導入）
; - データ（%LOCALAPPDATA%\SamuraiDealerResearch）は本体と別の場所にあり、更新・アンインストールで消さない
; - 起動中は更新を進めず、終了を案内する（アプリが作る名前付きミューテックスで判定）

#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppName "Samurai Dealer Research"
#define AppExe "SamuraiDealerResearch.exe"

[Setup]
AppId={{6F1E4C2A-8B37-4D59-A1C2-3E9B7D50F842}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\SamuraiDealerResearch
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist-installer
OutputBaseFilename=SamuraiDealerResearch-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AppMutex=SamuraiDealerResearch.Running
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 古い版の部品を残さない（データフォルダーは別の場所なので影響しない）
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\SamuraiDealerResearch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Messages]
japanese.FinishedLabel=[name] のインストールが完了しました。スタートメニュー（デスクトップのアイコンを作った場合はそちらも）から起動できます。%n%n調べたデータは 利用者フォルダーの AppData\Local\SamuraiDealerResearch に保存され、更新やアンインストールでは消えません。

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox('アプリ本体を削除しました。調べたデータ（販売店・連絡方法・根拠・バックアップ）は次の場所に残しています。不要な場合は手動で削除してください。' + #13#10#13#10 +
      ExpandConstant('{localappdata}\SamuraiDealerResearch'), mbInformation, MB_OK);
end;
