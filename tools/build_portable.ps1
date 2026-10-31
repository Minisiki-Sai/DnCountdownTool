param(
    [string]$OutDir = '',
    [Alias('SkipBuild')][switch]$SkipShell,
    [switch]$SkipEngine,
    [switch]$NoZip,
    [switch]$FreshEngine
)

# 一次性产出「免安装绿色版」整套目录：
#   1. PyInstaller 冻结引擎   -> release-portable\engine_dist\DragonNestEngine
#   2. Tauri 桌面壳（无安装包）-> app\src-tauri\target\release\dn-countdown.exe
#   3. 绿色版布局             -> <OutDir>\Start-Portable.bat + runtime\engines\<id>
#   4. 可选 zip               -> <OutDir>.zip
# 全程不写注册表、不写 Program Files。
#
# 用法（在仓库根）：
#   powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1
#   ... -SkipEngine            复用已冻结的引擎
#   ... -SkipShell             复用已编译的桌面壳
#   ... -NoZip                 不压缩
#   ... -FreshEngine           PyInstaller 加 --clean
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$env:PATH = (Join-Path $env:USERPROFILE '.cargo\bin') + ';' + $env:PATH

function Write-Step([string]$Message) { Write-Output ('[portable] ' + $Message) }
function Write-Note([string]$Message) { Write-Output ('[portable] - ' + $Message) }
function Format-Elapsed($Span) { return $Span.ToString('mm\:ss') }
function Stop-WithHelp {
    param([string]$Message, [string[]]$Hints)
    Write-Output ''
    Write-Output ('[portable] 构建中止：' + $Message)
    foreach ($hint in $Hints) { Write-Output ('  - ' + $hint) }
    Write-Output ''
    exit 2
}

$total = [Diagnostics.Stopwatch]::StartNew()

# --- 路径与版本 -------------------------------------------------------------
$appDir = Join-Path $root 'app'
$tauriDir = Join-Path $appDir 'src-tauri'
$python = Join-Path $root '.venv\Scripts\python.exe'
$specFile = Join-Path $root 'DragonNestEngine.spec'
$engineDist = Join-Path $root 'release-portable\engine_dist\DragonNestEngine'
$engineWork = Join-Path $root 'release-portable\engine_work'

$cargoFile = Join-Path $tauriDir 'Cargo.toml'
$cargoText = Get-Content -LiteralPath $cargoFile -Raw
# appVersion 必须和 Rust 的 env!("CARGO_PKG_VERSION") 完全一致，
# active-engine.json 校验用的是这个字符串（不是去掉 .0 的显示版本）。
$mVersion = [regex]::Match($cargoText, '(?m)^version\s*=\s*"([^"]+)"')
$mPackage = [regex]::Match($cargoText, '(?m)^name\s*=\s*"([^"]+)"')
if (-not $mVersion.Success -or -not $mPackage.Success) {
    throw "读不出 $cargoFile 里的 name/version"
}
$appVersion = $mVersion.Groups[1].Value
$cargoPackage = $mPackage.Groups[1].Value

$productVersion = ''
$versionJson = Join-Path $root 'version.json'
if (Test-Path -LiteralPath $versionJson) {
    try { $productVersion = [string]((Get-Content -LiteralPath $versionJson -Raw | ConvertFrom-Json).product) } catch { }
}
if ($productVersion -and $productVersion -ne $appVersion) {
    Write-Note ('警告：version.json product=' + $productVersion + ' 与 Cargo.toml version=' + $appVersion + ' 不一致；先跑 python tools\sync_version.py')
}

if (-not $OutDir) {
    # 产物放项目**同级**目录，不写回源码树（否则每次打包都几百条 git 变更）。
    $OutDir = Join-Path (Split-Path -Parent $root) 'DnCountdownTool-release\DN倒计时工具'
}

Write-Output ''
Write-Step ('Version ' + $appVersion)
Write-Step ('Project ' + $root)
Write-Output ''

# --- 0. 环境预检 ------------------------------------------------------------
$problems = New-Object System.Collections.ArrayList

if (-not $SkipEngine) {
    if (-not (Test-Path -LiteralPath $python)) {
        [void]$problems.Add('缺少 Python 虚拟环境：' + $python)
    } elseif (-not (Test-Path -LiteralPath $specFile)) {
        [void]$problems.Add('缺少 PyInstaller 配置：' + $specFile)
    } else {
        $probe = & $python -c "import PyInstaller,sys;sys.stdout.write(PyInstaller.__version__)" 2>&1
        if ($LASTEXITCODE -ne 0) {
            [void]$problems.Add('虚拟环境里没有 PyInstaller：.venv\Scripts\python.exe -m pip install pyinstaller')
        } else {
            Write-Note ('PyInstaller ' + $probe)
        }
    }
}

$msvcRoot = $env:DN_MSVC_ROOT
$sdkRoot = $env:DN_SDK_ROOT
$sdkVer = $env:DN_SDK_VER

if (-not $SkipShell) {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        [void]$problems.Add('找不到 cargo（安装 rustup 后重开终端）')
    } else {
        Write-Note ((& cargo --version 2>&1 | Select-Object -First 1))
    }

    if (-not $msvcRoot) {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path -LiteralPath $vswhere) {
            $vsInstall = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
            if ($vsInstall) {
                $latest = Get-ChildItem -LiteralPath (Join-Path $vsInstall 'VC\Tools\MSVC') -Directory -ErrorAction SilentlyContinue |
                    Sort-Object Name -Descending | Select-Object -First 1
                if ($latest) { $msvcRoot = $latest.FullName }
            }
        }
    }
    if (-not $sdkRoot) {
        $kitRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
        if (Test-Path -LiteralPath (Join-Path $kitRoot 'Lib')) { $sdkRoot = $kitRoot }
    }
    if (-not $sdkVer -and $sdkRoot) {
        $latestSdk = Get-ChildItem -LiteralPath (Join-Path $sdkRoot 'Lib') -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'ucrt') } |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($latestSdk) { $sdkVer = $latestSdk.Name }
    }
    if (-not $msvcRoot -or -not $sdkRoot -or -not $sdkVer) {
        [void]$problems.Add('探测不到 MSVC/SDK，请用 DN_MSVC_ROOT / DN_SDK_ROOT / DN_SDK_VER 指定')
    } else {
        $linkExe = Join-Path $msvcRoot 'bin\Hostx64\x64\link.exe'
        if (-not (Test-Path -LiteralPath $linkExe)) {
            [void]$problems.Add('找不到 MSVC 链接器：' + $linkExe)
        } else {
            Write-Note ('MSVC ' + $msvcRoot)
        }
        if (-not (Test-Path -LiteralPath (Join-Path $sdkRoot ('Lib\' + $sdkVer + '\ucrt\x64')))) {
            [void]$problems.Add('找不到 Windows SDK ' + $sdkVer + '：' + $sdkRoot)
        } else {
            Write-Note ('Windows SDK ' + $sdkVer)
        }
    }

    $pnpm = $null
    $pnpmCmd = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if ($pnpmCmd) { $pnpm = $pnpmCmd.Source }
    if (-not $pnpm -and (Test-Path -LiteralPath (Join-Path $env:APPDATA 'npm\pnpm.cmd'))) { $pnpm = Join-Path $env:APPDATA 'npm\pnpm.cmd' }
    if (-not $pnpm -and (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'nodejs\pnpm.cmd'))) { $pnpm = Join-Path $env:ProgramFiles 'nodejs\pnpm.cmd' }
    if (-not $pnpm) {
        foreach ($item in @(Get-ChildItem -Path (Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions') -Filter 'pnpm.CMD' -Recurse -ErrorAction SilentlyContinue)) {
            if (-not $pnpm) { $pnpm = $item.FullName }
        }
    }
    if (-not $pnpm) { [void]$problems.Add('找不到 pnpm（安装 Node.js 后执行 npm i -g pnpm）') }
    else { Write-Note ('pnpm ' + $pnpm) }
}

if ($problems.Count -gt 0) {
    Stop-WithHelp '构建环境不完整。' $problems.ToArray()
}

# --- 1. 冻结引擎 ------------------------------------------------------------
$engineExe = Join-Path $engineDist 'DragonNestEngine.exe'
if ($SkipEngine) {
    if (-not (Test-Path -LiteralPath $engineExe)) {
        Stop-WithHelp ('-SkipEngine 已指定，但找不到已冻结的引擎：' + $engineExe) @(
            '去掉 -SkipEngine 让脚本自己构建。'
        )
    }
    Write-Step ('Step 1/4 skipped, engine: ' + $engineDist)
} else {
    if (Test-Path -LiteralPath $engineDist) {
        Write-Step '清理上一次的引擎产物...'
        & cmd /c ('rmdir /s /q "' + $engineDist + '"')
        if (Test-Path -LiteralPath $engineDist) {
            Stop-WithHelp ('无法清理旧引擎输出：' + $engineDist) @(
                '多半是仍有进程占用其中的文件（引擎还在跑？）或权限不足。'
            )
        }
    }
    Write-Step 'Step 1/4 冻结 Python 引擎（PyInstaller，几分钟）...'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $pyArgs = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--distpath', (Join-Path $root 'release-portable\engine_dist'),
        '--workpath', $engineWork
    )
    if ($FreshEngine) { $pyArgs += '--clean' }
    $pyArgs += $specFile
    & $python @pyArgs
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHelp ('PyInstaller 构建失败，退出码 ' + $LASTEXITCODE) @(
            '看上面 PyInstaller 的 ERROR 行，通常是 spec 里的数据目录不存在。',
            '告警清单：release-portable\engine_work\DragonNestEngine\warn-DragonNestEngine.txt'
        )
    }
    if (-not (Test-Path -LiteralPath $engineExe)) {
        Stop-WithHelp ('PyInstaller 结束了，但没有产出 ' + $engineExe) @(
            '检查 spec 里 COLLECT 的 name 是否仍为 DragonNestEngine。'
        )
    }
    $sw.Stop()
    Write-Step ('Step 1/4 done in ' + (Format-Elapsed $sw.Elapsed))
}

# --- 2. 桌面壳（不生成安装包）-----------------------------------------------
$releaseDir = Join-Path $tauriDir 'target\release'
if ($SkipShell) {
    Write-Step 'Step 2/4 skipped, 复用已编译的桌面程序。'
} else {
    Write-Step 'Step 2/4 构建桌面壳（vite + cargo，--no-bundle）...'
    $sw = [Diagnostics.Stopwatch]::StartNew()

    $env:PATH = "$env:USERPROFILE\.cargo\bin;$msvcRoot\bin\Hostx64\x64;$sdkRoot\bin\$sdkVer\x64;$env:PATH"
    $env:LIB = "$msvcRoot\lib\x64;$sdkRoot\Lib\$sdkVer\ucrt\x64;$sdkRoot\Lib\$sdkVer\um\x64;$env:LIB"
    $env:INCLUDE = "$msvcRoot\include;$sdkRoot\Include\$sdkVer\ucrt;$sdkRoot\Include\$sdkVer\um;$sdkRoot\Include\$sdkVer\shared;$sdkRoot\Include\$sdkVer\winrt;$env:INCLUDE"
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER = Join-Path $msvcRoot 'bin\Hostx64\x64\link.exe'
    $env:CARGO_HTTP_MULTIPLEXING = 'false'

    $pnpmDir = Split-Path -Parent $pnpm
    if ($pnpmDir) { $env:PATH = "$pnpmDir;$env:PATH" }

    $distDir = Join-Path $appDir 'dist'
    if (Test-Path -LiteralPath $distDir) { & cmd /c ('rmdir /s /q "' + $distDir + '"') }

    Push-Location -LiteralPath $appDir
    try {
        & $pnpm 'build:web'
        if ($LASTEXITCODE -ne 0) { Stop-WithHelp ('前端构建失败，退出码 ' + $LASTEXITCODE) @('单独跑 cd app && pnpm build:web 看完整日志。') }

        & $pnpm 'exec' 'tauri' 'build' '--no-bundle' '--config' (Join-Path $root 'tools\tauri.portable.conf.json')
        if ($LASTEXITCODE -ne 0) {
            Stop-WithHelp ('桌面程序构建失败，退出码 ' + $LASTEXITCODE) @(
                '常见原因：Rust 工具链缺失、MSVC/SDK 版本变了、crates 下载失败、TypeScript 类型检查没通过。',
                '单独跑 cd app && pnpm exec tauri build --no-bundle --config ..\tools\tauri.portable.conf.json 看完整日志。'
            )
        }
    } finally {
        Pop-Location
    }
    $sw.Stop()
    Write-Step ('Step 2/4 done in ' + (Format-Elapsed $sw.Elapsed))
}

# target\release 里会残留旧产品名的 exe，按 Cargo 包名取，别"挑最大的"。
$mainExe = Get-Item -LiteralPath (Join-Path $releaseDir ($cargoPackage + '.exe')) -ErrorAction SilentlyContinue
if (-not $mainExe) {
    Stop-WithHelp ('没有找到桌面主程序：' + (Join-Path $releaseDir ($cargoPackage + '.exe'))) @(
        '确认第 2 步成功结束，且 target\release 下有 ' + $cargoPackage + '.exe。'
    )
}
Write-Step ('Desktop: ' + $mainExe.Name + ' (' + [math]::Round($mainExe.Length / 1MB, 1) + ' MB)')

# --- 3. 绿色版布局 ----------------------------------------------------------
$outFull = [IO.Path]::GetFullPath($OutDir).TrimEnd('\')
if ($outFull -eq [IO.Path]::GetPathRoot($outFull).TrimEnd('\')) { throw '必须给一个具体的输出目录。' }
if (Test-Path -LiteralPath $outFull) {
    # 只允许清理两个位置：项目自己、项目同级的 DnCountdownTool-release\。
    $releaseRoot = [IO.Path]::GetFullPath(
        (Join-Path (Split-Path -Parent $root) 'DnCountdownTool-release')
    ).TrimEnd('\')
    $insideProject = $outFull.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
    $insideRelease = $outFull.Equals($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $outFull.StartsWith($releaseRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    if (-not ($insideProject -or $insideRelease)) {
        throw "拒绝清理项目之外的输出目录：$outFull"
    }
    $holders = @()
    foreach ($proc in (Get-Process -ErrorAction SilentlyContinue)) {
        try {
            if ($proc.Path -and $proc.Path.StartsWith($outFull, [StringComparison]::OrdinalIgnoreCase)) {
                $holders += ($proc.ProcessName + '(' + $proc.Id + ')')
            }
        } catch {
        }
    }
    if ($holders.Count -gt 0) {
        Stop-WithHelp ('旧的绿色版目录正被占用：' + $outFull) @(
            '先关闭正在运行的绿色版程序，再重新打包。',
            '占用中的进程：' + (($holders | Select-Object -Unique) -join ', ')
        )
    }
    Write-Step 'Step 3/4 清理上一次的绿色版目录...'
    Remove-Item -LiteralPath $outFull -Recurse -Force
}
New-Item -ItemType Directory -Path $outFull -Force | Out-Null

Copy-Item -LiteralPath $mainExe.FullName -Destination (Join-Path $outFull $mainExe.Name) -Force
Get-ChildItem -LiteralPath $releaseDir -Filter *.dll -File -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $outFull $_.Name) -Force
}

$runtimeOut = Join-Path $outFull 'runtime'
$releaseId = ([guid]::NewGuid().ToString('N')).Substring(0, 12)
$engineOut = Join-Path $runtimeOut ('engines\' + $releaseId)
New-Item -ItemType Directory -Path $engineOut -Force | Out-Null
Write-Step ('拷贝引擎到 runtime\engines\' + $releaseId)
Copy-Item -Path (Join-Path $engineDist '*') -Destination $engineOut -Recurse -Force

# 这两个文件是 Rust 侧 resolve_engine() 的唯一凭据：
# schemaVersion=1 / verified=true / appVersion 必须等于 CARGO_PKG_VERSION，
# 目录名必须是 12 位小写十六进制。
$selection = [ordered]@{
    schemaVersion = 1
    verified      = $true
    appVersion    = $appVersion
    directory     = 'engines/' + $releaseId
}
$json = ($selection | ConvertTo-Json -Compress)
[IO.File]::WriteAllText((Join-Path $engineOut 'engine-release.json'), $json, (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $runtimeOut 'active-engine.json'), $json, (New-Object Text.UTF8Encoding($false)))

$npcap = Join-Path $root 'third_party\npcap\npcap-1.88.exe'
if (Test-Path -LiteralPath $npcap) {
    New-Item -ItemType Directory -Path (Join-Path $outFull 'third_party\npcap') -Force | Out-Null
    Copy-Item -LiteralPath $npcap -Destination (Join-Path $outFull 'third_party\npcap\npcap-1.88.exe') -Force
    Write-Step '已附带 Npcap 安装包'
} else {
    Write-Note '缺少 third_party\npcap\npcap-1.88.exe：包内不带抓包驱动，用户需自行准备 Npcap。'
}

$launcher = @'
@echo off
setlocal EnableExtensions
cd /d "%~dp0"
for %%F in (*.exe) do (
    if /I not "%%~nF"=="DragonNestEngine" (
        start "" "%%~fF"
        exit /b 0
    )
)
echo Desktop executable was not found next to this launcher.
pause
exit /b 1
'@
[IO.File]::WriteAllText((Join-Path $outFull 'Start-Portable.bat'),
    ($launcher -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding($false)))

# --- 4. 压缩 ----------------------------------------------------------------
$zipPath = $outFull + '.zip'
if ($NoZip) {
    Write-Step 'Step 4/4 skipped, 未要求压缩。'
} else {
    Write-Step 'Step 4/4 压缩（一两分钟）...'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Compress-Archive -Path $outFull -DestinationPath $zipPath -CompressionLevel Optimal
    $sw.Stop()
    Write-Step ('Step 4/4 done in ' + (Format-Elapsed $sw.Elapsed))
}

# --- 报告 -------------------------------------------------------------------
$total.Stop()
$size = (Get-ChildItem -LiteralPath $outFull -Recurse -File | Measure-Object -Property Length -Sum).Sum
$engineSize = (Get-ChildItem -LiteralPath $engineOut -Recurse -File | Measure-Object -Property Length -Sum).Sum
$fileCount = (Get-ChildItem -LiteralPath $outFull -Recurse -File).Count

Write-Output ''
Write-Output '[portable] ================ build finished ================'
Write-Output ('[portable] Bundle   : ' + $outFull)
Write-Output ('[portable] Files    : ' + $fileCount + ' files, ' + ('{0:N1}' -f ($size / 1MB)) + ' MB (engine ' + ('{0:N1}' -f ($engineSize / 1MB)) + ' MB)')
if (-not $NoZip -and (Test-Path -LiteralPath $zipPath)) {
    Write-Output ('[portable] Zip      : ' + $zipPath + ' (' + ('{0:N1}' -f ((Get-Item -LiteralPath $zipPath).Length / 1MB)) + ' MB)')
}
Write-Output ('[portable] Executable: ' + $mainExe.Name)
Write-Output ('[portable] Version  : ' + $appVersion)
Write-Output ('[portable] Elapsed  : ' + (Format-Elapsed $total.Elapsed))
Write-Output '[portable] Next: 双击包内的 Start-Portable.bat（首次会请求管理员权限，抓包需要）。'
Write-Output '[portable] ================================================'
