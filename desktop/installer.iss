; Complete-source Windows installer. Build the PyInstaller onedir first.
; Parameters: /DPayloadDir=... /DOutputDir=... /DRepoRoot=...
;             /DAppVersion=0.1.0 /DAppBuild=2
; Optional:   /DWebView2Installer=...\MicrosoftEdgeWebView2RuntimeInstallerX64.exe

#ifndef PayloadDir
  #error PayloadDir must point to the complete PyInstaller ScholarFlow directory.
#endif
#ifndef OutputDir
  #error OutputDir must point to a separate installer output directory.
#endif
#ifndef RepoRoot
  #error RepoRoot must point to the current ScholarFlow source root.
#endif
#ifndef AppVersion
  #error AppVersion must match release.json and the packaged executable.
#endif
#ifndef AppBuild
  #error AppBuild must match release.json and the packaged executable.
#endif
#if !FileExists(AddBackslash(PayloadDir) + "ScholarFlow.exe")
  #error PayloadDir does not contain ScholarFlow.exe.
#endif
#if !DirExists(AddBackslash(PayloadDir) + "_internal")
  #error PayloadDir must contain the complete PyInstaller _internal directory.
#endif
#if FileExists(AddBackslash(PayloadDir) + ".env") || FileExists(AddBackslash(PayloadDir) + "scholarflow.db") || FileExists(AddBackslash(PayloadDir) + "config.json") || DirExists(AddBackslash(PayloadDir) + "data")
  #error PayloadDir contains application data or configuration; use clean release output.
#endif
#ifdef WebView2Installer
  #if !FileExists(WebView2Installer)
    #error WebView2Installer must refer to the verified official standalone x64 installer.
  #endif
#endif

[Setup]
AppId={{D701D4AA-BA44-4D44-A3EF-715478E88547}
AppName=研序 ScholarFlow
AppVersion={#AppVersion}
AppVerName=研序 ScholarFlow {#AppVersion}（构建 {#AppBuild}）
AppPublisher=研序 ScholarFlow
VersionInfoVersion={#AppVersion}.{#AppBuild}
VersionInfoDescription=研序 ScholarFlow 安装程序
DefaultDirName={localappdata}\Programs\ScholarFlow
DefaultGroupName=研序 ScholarFlow
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousTasks=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableWelcomePage=no
DisableReadyPage=no
AllowNoIcons=yes
AllowUNCPath=no
WizardStyle=modern
WizardSizePercent=110
SetupIconFile={#RepoRoot}\desktop\assets\icon.ico
UninstallDisplayIcon={app}\ScholarFlow.exe
UninstallDisplayName=研序 ScholarFlow
Uninstallable=yes
CloseApplications=no
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=ScholarFlow-Setup-{#AppVersion}-build{#AppBuild}-x64
Compression=lzma2/normal
SolidCompression=yes
LZMAUseSeparateProcess=yes
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[LangOptions]
LanguageName=简体中文
LanguageID=$0804
LanguageCodePage=0
DialogFontName=Microsoft YaHei UI
DialogFontSize=9

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 - %1
UninstallAppTitle=卸载程序
UninstallAppFullTitle=卸载 %1
InformationTitle=提示
ConfirmTitle=确认
ErrorTitle=错误
ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonOK=确定
ButtonCancel=取消
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)...
ButtonWizardBrowse=浏览(&R)...
ButtonNewFolder=新建文件夹(&M)
ClickNext=点击“下一步”继续，或点击“取消”退出安装。
BrowseDialogTitle=选择安装文件夹
BrowseDialogLabel=请选择文件夹，然后点击“确定”。
NewFolderName=新建文件夹
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将在您的电脑上安装 [name/ver]。%n%n您可以选择 C、D、E 等盘符下的具体安装目录。聊天记录和资料保存在当前用户的应用数据目录，卸载时会保留。%n%n安装到已有目录前，请先关闭该目录中的研序程序。
WizardSelectDir=选择安装位置
SelectDirDesc=您希望将 [name] 安装到哪里？
SelectDirLabel3=程序文件将安装到下列文件夹。聊天记录和资料使用独立的用户数据目录。
SelectDirBrowseLabel=可直接输入 D:\应用\ScholarFlow 等完整路径，或点击“浏览”选择其他盘符和文件夹。
DiskSpaceGBLabel=至少需要 [gb] GB 可用空间。
DiskSpaceMBLabel=至少需要 [mb] MB 可用空间。
DirExistsTitle=文件夹已存在
DirExists=文件夹：%n%n%1%n%n已经存在。继续将安装程序文件到此目录。确定使用此文件夹吗？
DirDoesntExistTitle=创建文件夹
DirDoesntExist=文件夹：%n%n%1%n%n不存在。是否创建？
WizardSelectProgramGroup=选择开始菜单文件夹
SelectStartMenuFolderDesc=您希望在哪里创建程序快捷方式？
SelectStartMenuFolderLabel3=安装程序将在下列开始菜单文件夹中创建快捷方式。
SelectStartMenuFolderBrowseLabel=点击“下一步”继续，或点击“浏览”选择其他文件夹。
NoProgramGroupCheck2=不创建开始菜单文件夹(&D)
WizardSelectTasks=选择附加任务
SelectTasksDesc=您希望执行哪些附加任务？
SelectTasksLabel2=选择需要执行的任务，然后点击“下一步”。
WizardReady=准备安装
ReadyLabel1=安装程序已准备好将 [name] 安装到您的电脑。
ReadyLabel2a=点击“安装”开始安装，或点击“上一步”检查安装目录。
ReadyLabel2b=点击“安装”开始安装。
ReadyMemoDir=安装目录：
ReadyMemoGroup=开始菜单文件夹：
ReadyMemoTasks=附加任务：
WizardPreparing=正在准备安装
PreparingDesc=正在检查运行环境和安装目录。
CannotContinue=无法继续安装。请解决上述问题后重试，或点击“取消”退出。
WizardInstalling=正在安装
InstallingLabel=正在安装 [name]，请稍候。
StatusCreateDirs=正在创建文件夹...
StatusExtractFiles=正在安装程序文件...
StatusCreateIcons=正在创建快捷方式...
StatusCreateRegistryEntries=正在登记安装信息...
StatusSavingUninstall=正在保存卸载信息...
StatusRunProgram=正在完成安装...
StatusRollback=正在撤销未完成的安装...
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=已将 [name] 安装到您的电脑。您可以通过开始菜单或所选目录启动程序。%n%n首次使用云模型时，请在“模型与设置”中配置自己的接口和密钥。
FinishedLabelNoIcons=已将 [name] 安装到您的电脑。请在所选安装目录中运行 ScholarFlow.exe。%n%n首次使用云模型时，请在“模型与设置”中配置自己的接口和密钥。
ClickFinish=点击“完成”退出安装。
ExitSetupTitle=退出安装
ExitSetupMessage=安装尚未完成。现在退出将中止安装。%n%n确定退出吗？
SetupAborted=安装未完成。%n%n请解决提示的问题后重新运行安装程序。
SetupAppRunningError=安装程序检测到 %1 正在运行。%n%n请先关闭该程序，再重试安装。
UninstallAppRunningError=卸载程序检测到 %1 正在运行。%n%n请先关闭该程序，再重试卸载。
UninstallStatusLabel=正在卸载 %1，请稍候。
ConfirmUninstall=确定卸载 %1 及其程序文件吗？%n%n聊天记录、文献、模型配置和其他用户资料将保留。
UninstalledAll=%1 已卸载。%n%n用户资料已保留；以后重新安装可继续使用。
UninstalledMost=%1 已卸载。%n%n部分程序文件未能移除，可能仍被占用。用户资料已保留。

[CustomMessages]
DesktopShortcut=创建桌面快捷方式
AdditionalShortcuts=快捷方式：
LaunchProgram=启动研序 ScholarFlow
UninstallShortcut=卸载研序 ScholarFlow

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; GroupDescription: "{cm:AdditionalShortcuts}"; Flags: unchecked

[Files]
; Only complete, clean PyInstaller output is accepted. Do not point this at source.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: ".env,.env.*,.runtime.json,.runtime.lock,scholarflow.db,scholarflow.db-*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#RepoRoot}\LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion
#ifdef WebView2Installer
Source: "{#WebView2Installer}"; DestName: "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy
#endif

[Icons]
Name: "{group}\研序 ScholarFlow"; Filename: "{app}\ScholarFlow.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallShortcut}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\研序 ScholarFlow"; Filename: "{app}\ScholarFlow.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ScholarFlow.exe"; Description: "{cm:LaunchProgram}"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

; Intentionally no [UninstallDelete]: Inno removes only files it installed.
; %APPDATA%\ScholarFlow is never copied, migrated, or removed by this installer.

[Code]
const
  WebView2RegistryKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2DownloadUrl = 'https://developer.microsoft.com/microsoft-edge/webview2/';

var
  RuntimePage: TWizardPage;
  RuntimeStatusLabel: TNewStaticText;
  RuntimeLinkButton: TNewButton;

function RuntimeInRegistry(RootKey: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, WebView2RegistryKey, 'pv', Version);
  if Result then
    Result := (Trim(Version) <> '') and (Trim(Version) <> '0.0.0.0');
end;

function WebView2Installed: Boolean;
begin
  Result := RuntimeInRegistry(HKLM32) or RuntimeInRegistry(HKCU32) or
    RuntimeInRegistry(HKLM64) or RuntimeInRegistry(HKCU64);
end;

procedure UpdateRuntimeStatus;
begin
  if WebView2Installed then
    RuntimeStatusLabel.Caption := '已检测到 Microsoft Edge WebView2 运行时，可以继续安装。'
  else
  begin
#ifdef WebView2Installer
    RuntimeStatusLabel.Caption := '尚未检测到 Microsoft Edge WebView2 运行时。点击“安装”后，将先使用安装包内的微软独立安装程序安装运行时，无需另外下载。';
#else
    RuntimeStatusLabel.Caption := '尚未检测到 Microsoft Edge WebView2 运行时。请点击下方按钮打开微软官方网站，安装 Evergreen Standalone x64 运行时后回到此处继续。此安装包不包含运行时。';
#endif
  end;
end;

procedure OpenRuntimeDownload(Sender: TObject);
var
  ErrorCode: Integer;
begin
  if not ShellExec('open', WebView2DownloadUrl, '', '', SW_SHOWNORMAL,
    ewNoWait, ErrorCode) then
    MsgBox('无法打开浏览器。请手动访问：' + #13#10 + WebView2DownloadUrl,
      mbInformation, MB_OK);
end;

procedure InitializeWizard;
begin
  RuntimePage := CreateCustomPage(wpWelcome, '运行环境检查',
    '研序使用 Microsoft Edge WebView2 显示桌面界面。');
  RuntimeStatusLabel := TNewStaticText.Create(RuntimePage);
  RuntimeStatusLabel.Parent := RuntimePage.Surface;
  RuntimeStatusLabel.Left := 0;
  RuntimeStatusLabel.Top := ScaleY(12);
  RuntimeStatusLabel.Width := RuntimePage.SurfaceWidth;
  RuntimeStatusLabel.Height := ScaleY(90);
  RuntimeStatusLabel.AutoSize := False;
  RuntimeStatusLabel.WordWrap := True;
  RuntimeLinkButton := TNewButton.Create(RuntimePage);
  RuntimeLinkButton.Parent := RuntimePage.Surface;
  RuntimeLinkButton.Left := 0;
  RuntimeLinkButton.Top := ScaleY(116);
  RuntimeLinkButton.Width := ScaleX(210);
  RuntimeLinkButton.Height := ScaleY(28);
  RuntimeLinkButton.Caption := '打开微软 WebView2 官方下载页';
  RuntimeLinkButton.OnClick := @OpenRuntimeDownload;
  UpdateRuntimeStatus;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = RuntimePage.ID then
    UpdateRuntimeStatus;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Target, DataDirectory: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Target := RemoveBackslash(ExpandFileName(WizardDirValue));
    DataDirectory := RemoveBackslash(ExpandConstant('{userappdata}\ScholarFlow'));
    if Length(Target) <= 3 then
    begin
      MsgBox('请选择盘符下的具体文件夹，例如 D:\应用\ScholarFlow；不要直接安装到盘符根目录。', mbError, MB_OK);
      Result := False;
    end
    else if Pos(Lowercase(AddBackslash(DataDirectory)),
      Lowercase(AddBackslash(Target))) = 1 then
    begin
      MsgBox('安装目录不能位于研序的用户资料目录中。请选择独立的程序文件夹。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function TargetApplicationRunning(TargetDirectory: String; var CheckFailed: Boolean): Boolean;
var
  Locator, Services, Processes, Process: Variant;
  Index: Integer;
  TargetExecutable, ProcessPath: String;
begin
  Result := False;
  CheckFailed := False;
  TargetExecutable := AddBackslash(TargetDirectory) + 'ScholarFlow.exe';
  if not FileExists(TargetExecutable) then
    Exit;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery('SELECT ExecutablePath FROM Win32_Process WHERE Name = ''ScholarFlow.exe''');
    for Index := 0 to Processes.Count - 1 do
    begin
      Process := Processes.ItemIndex(Index);
      if VarIsNull(Process.ExecutablePath) or VarIsEmpty(Process.ExecutablePath) then
        CheckFailed := True
      else
      begin
        ProcessPath := Process.ExecutablePath;
        if CompareText(ExpandFileName(ProcessPath), ExpandFileName(TargetExecutable)) = 0 then
        begin
          Result := True;
          Exit;
        end;
      end;
    end;
  except
    CheckFailed := True;
    Log('Cannot check target process: ' + GetExceptionMessage);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  CheckFailed: Boolean;
#ifdef WebView2Installer
  RuntimeInstaller: String;
  ResultCode: Integer;
#endif
begin
  Result := '';
  if TargetApplicationRunning(ExpandConstant('{app}'), CheckFailed) then
  begin
    Result := '所选目录中的研序正在运行。请保存工作并关闭该程序，然后重试。安装程序不会自动结束进程。';
    Exit;
  end;
  if CheckFailed then
  begin
    Result := '无法确认所选目录中的旧程序是否已经关闭。请先关闭研序，或返回并选择一个新的安装目录。';
    Exit;
  end;
  if WebView2Installed then
    Exit;
#ifdef WebView2Installer
  WizardForm.PreparingLabel.Caption := '正在安装 Microsoft Edge WebView2 运行时，请稍候...';
  ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
  RuntimeInstaller := ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
  if not Exec(RuntimeInstaller, '/silent /install', ExpandConstant('{tmp}'),
    SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '无法启动微软 WebView2 运行时安装程序。请安装官方运行时后重试：' + #13#10 + WebView2DownloadUrl;
    Exit;
  end;
  if (ResultCode <> 0) and (ResultCode <> 3010) then
  begin
    Result := 'WebView2 运行时安装未完成（退出代码 ' + IntToStr(ResultCode) + '）。请安装官方运行时后重试：' + #13#10 + WebView2DownloadUrl;
    Exit;
  end;
  if ResultCode = 3010 then
    NeedsRestart := True;
  if not WebView2Installed then
    Result := '仍未检测到 WebView2 运行时。请完成微软运行时安装后重试：' + #13#10 + WebView2DownloadUrl;
#else
  Result := '尚未安装 Microsoft Edge WebView2 运行时。请从微软官方网站安装 Evergreen Standalone x64 版本，然后重新运行本安装程序：' + #13#10 + WebView2DownloadUrl;
#endif
end;

function InitializeUninstall: Boolean;
var
  CheckFailed: Boolean;
begin
  Result := not TargetApplicationRunning(ExpandConstant('{app}'), CheckFailed);
  if not Result then
    MsgBox('研序正在运行。请保存工作并关闭该目录中的研序，再进行卸载。用户资料将保留。', mbError, MB_OK)
  else if CheckFailed then
  begin
    MsgBox('无法确认程序是否已经关闭。请关闭研序后重试卸载。用户资料将保留。', mbError, MB_OK);
    Result := False;
  end;
end;
