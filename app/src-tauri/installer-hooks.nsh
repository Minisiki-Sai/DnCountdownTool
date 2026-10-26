Var LegacyUpgradeProduct
Var LegacyUpgradeDir
Var ExplicitUpdateDir
Var UpdateTargetSource
; Resolve relative to this included hook, not the caller expanding the macro.
!define DN_BACKEND_STOP_HELPER "${__FILEDIR__}\..\..\tools\stop_installed_backend.ps1"
!define DN_ENGINE_RELEASE_HELPER "${__FILEDIR__}\..\..\tools\install_engine_release.ps1"
!define DN_INSTALL_VERIFIER "${__FILEDIR__}\..\..\tools\verify_install_manifest.ps1"

!macro DN_TRY_LEGACY_VERSION PRODUCT
  ${If} $LegacyUpgradeDir == ""
    ReadRegStr $0 HKCU "Software\gcg\${PRODUCT}" ""
    ${If} $0 != ""
    ${AndIf} ${FileExists} "$0\dragon-nest-simulator-v420.exe"
      StrCpy $LegacyUpgradeProduct "${PRODUCT}"
      StrCpy $LegacyUpgradeDir "$0"
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  StrCpy $LegacyUpgradeProduct ""
  StrCpy $LegacyUpgradeDir ""
  StrCpy $ExplicitUpdateDir ""
  StrCpy $UpdateTargetSource ""

  ${If} $UpdateMode = 1
    ; New clients pass their actual executable directory. This covers moved or
    ; portable installations whose registry entry is absent/stale.
    ReadEnvStr $ExplicitUpdateDir "DRAGON_NEST_UPDATE_INSTALL_ROOT"
    ${If} $ExplicitUpdateDir != ""
      ${IfNot} ${FileExists} "$ExplicitUpdateDir\${MAINBINARYNAME}.exe"
        MessageBox MB_ICONSTOP|MB_OK "自动更新收到的原程序目录无效，为避免把新版本安装到其他目录，本次更新已取消。$\r$\n$\r$\n目录：$ExplicitUpdateDir$\r$\n$\r$\n请从原程序目录重新启动后再试。"
        SetErrorLevel 5
        Abort
      ${EndIf}
      StrCpy $INSTDIR "$ExplicitUpdateDir"
      StrCpy $UpdateTargetSource "当前运行程序目录"
    ${Else}
      ; Compatibility for old clients that launch this new installer without
      ; the environment variable: accept only a registry directory containing
      ; the real desktop executable.
      ReadRegStr $0 HKCU "Software\gcg\${PRODUCTNAME}" ""
      ${If} $0 != ""
      ${AndIf} ${FileExists} "$0\${MAINBINARYNAME}.exe"
        StrCpy $INSTDIR "$0"
        StrCpy $UpdateTargetSource "当前产品安装记录"
      ${Else}
        ; Automatic updates must ignore the manual-migration completion marker:
        ; an older product can still be the running version being upgraded.
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷战斗平台"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.25"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.24"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.23"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.22"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.21"
        !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.20"
        ${If} $LegacyUpgradeDir != ""
          StrCpy $INSTDIR "$LegacyUpgradeDir"
          StrCpy $UpdateTargetSource "历史版本安装记录"
        ${Else}
          MessageBox MB_ICONSTOP|MB_OK "无法确认当前版本的安装目录，为避免把新版本安装到其他目录，本次自动更新已取消。$\r$\n$\r$\n请从原程序目录启动后重试，或手动运行安装包并选择原目录。"
          SetErrorLevel 5
          Abort
        ${EndIf}
      ${EndIf}
    ${EndIf}
    DetailPrint "自动更新目标：$INSTDIR（来源：$UpdateTargetSource）"
  ${Else}
    ; The display name changed in V4.50. Keep the previous unified install in
    ; place even when the older V4.20-V4.25 migration was already completed.
    !insertmacro DN_TRY_LEGACY_VERSION "龙之谷战斗平台"
    ReadRegDWORD $0 HKCU "Software\gcg\DragonNestSimulatorInstaller" "LegacyMigrationCompleted"
    ${If} $LegacyUpgradeDir == ""
    ${AndIf} $0 != 1
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.25"
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.24"
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.23"
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.22"
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.21"
      !insertmacro DN_TRY_LEGACY_VERSION "龙之谷配装模拟器 V4.20"
    ${EndIf}
    ${If} $LegacyUpgradeDir != ""
      MessageBox MB_ICONQUESTION|MB_YESNO|MB_DEFBUTTON1 \
        "检测到 $LegacyUpgradeProduct。$\r$\n$\r$\n是否把它直接升级为 ${PRODUCTNAME} ${VERSION}？$\r$\n$\r$\n将继续使用原安装目录，并保留用户配置。" \
        IDNO dn_legacy_upgrade_skipped
      StrCpy $INSTDIR "$LegacyUpgradeDir"
      DetailPrint "升级现有安装：$LegacyUpgradeProduct"
      Goto dn_legacy_upgrade_selected
      dn_legacy_upgrade_skipped:
      StrCpy $LegacyUpgradeProduct ""
      StrCpy $LegacyUpgradeDir ""
      dn_legacy_upgrade_selected:
    ${EndIf}
  ${EndIf}

  ; Tauri normally checks the desktop AFTER this hook. Run its normal close
  ; prompt first, then verify orphan workers and actual file locks. Otherwise
  ; an open desktop bypasses cleanup and its engine can survive into extraction.
  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  ; Legacy migration may change INSTDIR after Tauri sets its output directory.
  SetOutPath "$INSTDIR"
  InitPluginsDir
  File /oname=$PLUGINSDIR\stop-dragon-nest-backend.ps1 "${DN_BACKEND_STOP_HELPER}"
  File /oname=$PLUGINSDIR\install-engine-release.ps1 "${DN_ENGINE_RELEASE_HELPER}"
  File /oname=$PLUGINSDIR\verify-install.ps1 "${DN_INSTALL_VERIFIER}"
  DetailPrint "正在等待后台引擎退出并检查安装文件占用..."
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\stop-dragon-nest-backend.ps1" -InstallRoot "$INSTDIR" -SideBySide -GracefulTimeoutSeconds 8 -UnlockTimeoutSeconds 12 -LogPath "$TEMP\dragon-nest-install-preflight.log"'
  Pop $8
  Pop $9
  ${If} $8 != 0
    MessageBox MB_ICONSTOP|MB_OK "安装文件仍被占用或没有写入权限，尚未覆盖程序文件。$\r$\n$\r$\n请确认旧程序已退出；若仍失败，请重启电脑后再安装，不要选择忽略文件。$\r$\n$\r$\n诊断日志：$TEMP\dragon-nest-install-preflight.log$\r$\n$\r$\n$9"
    SetErrorLevel 4
    Abort
  ${EndIf}
  DetailPrint "$9"
!macroend

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "正在部署独立版本引擎并校验文件（SHA-256）..."
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\install-engine-release.ps1" -InstallRoot "$INSTDIR" -ManifestPath "$INSTDIR\runtime\install-manifest.json" -VerifierPath "$PLUGINSDIR\verify-install.ps1" -StopHelperPath "$PLUGINSDIR\stop-dragon-nest-backend.ps1" -ExpectedVersion "${VERSION}" -LogPath "$TEMP\dragon-nest-install-preflight.log"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "新引擎部署或校验失败，未切换到新引擎，本次升级未被标记为成功。$\r$\n$\r$\n$1$\r$\n$\r$\n诊断日志：$TEMP\dragon-nest-install-preflight.log$\r$\n$\r$\n若提示文件缺失，请检查安全软件的隔离记录；若提示大小或哈希不符，请重新下载安装包。其他情况请将诊断日志发给作者，不要跳过校验。"
    SetErrorLevel 3
    Abort
  ${EndIf}
  DetailPrint "$1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}" "InstallIntegrity" "SHA-256 verified"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}" "InstallVerifiedVersion" "${VERSION}"
  WriteRegDWORD HKCU "Software\gcg\DragonNestSimulatorInstaller" "LegacyMigrationCompleted" 1

  ${If} $LegacyUpgradeProduct != ""
  ${AndIf} $LegacyUpgradeDir == $INSTDIR
  ${AndIf} ${FileExists} "$INSTDIR\dragon-nest-simulator-v420.exe"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\$LegacyUpgradeProduct"
    DeleteRegKey HKCU "Software\gcg\$LegacyUpgradeProduct"
    Delete "$DESKTOP\$LegacyUpgradeProduct.lnk"
    Delete "$SMPROGRAMS\$LegacyUpgradeProduct.lnk"
    ; Legacy engine directories may still be locked. Leave them intact; only
    ; marked side-by-side releases are eligible for bounded cleanup.
    DetailPrint "旧版安装记录已迁移到统一产品记录。"
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  InitPluginsDir
  File /oname=$PLUGINSDIR\stop-dragon-nest-backend.ps1 "${DN_BACKEND_STOP_HELPER}"
  File /oname=$PLUGINSDIR\install-engine-release.ps1 "${DN_ENGINE_RELEASE_HELPER}"
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\stop-dragon-nest-backend.ps1" -InstallRoot "$INSTDIR" -SideBySide -LogPath "$TEMP\dragon-nest-install-preflight.log"'
  Pop $8
  Pop $9
  ${If} $8 != 0
    MessageBox MB_ICONSTOP|MB_OK "后台引擎尚未退出，卸载已停止。$\r$\n$9"
    Abort
  ${EndIf}
  ; Keep rollback files when this uninstaller is used as part of an upgrade.
  ${If} $UpdateMode != 1
    nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\install-engine-release.ps1" -InstallRoot "$INSTDIR" -CleanupOnly -LogPath "$TEMP\dragon-nest-install-preflight.log"'
    Pop $8
    Pop $9
    DetailPrint "$9"
  ${EndIf}
!macroend
