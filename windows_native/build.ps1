[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$nativeRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $nativeRoot '..'))
$venvRoot = Join-Path $nativeRoot '.build-venv'
$buildRoot = Join-Path $nativeRoot '.build'
$distRoot = Join-Path $nativeRoot 'dist'
$pythonExe = Join-Path $venvRoot 'Scripts\python.exe'
$version = (Get-Content -LiteralPath (Join-Path $nativeRoot 'VERSION') -Raw).Trim()
$versionMatch = [regex]::Match(
    $version,
    '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$'
)
if (-not $versionMatch.Success) {
    throw "VERSION 必须由三个整数段组成，例如 0.2.13；当前值：$version"
}
$versionMajor = [int]$versionMatch.Groups['major'].Value
$versionMinor = [int]$versionMatch.Groups['minor'].Value
$versionPatch = [int]$versionMatch.Groups['patch'].Value


function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}


function Assert-NativeChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = $nativeRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the native project: $resolved"
    }
    return $resolved
}


function Remove-SafeTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = Assert-NativeChildPath -Path $Path
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}


function Write-VersionResource {
    param([Parameter(Mandatory = $true)][string]$TargetPath)
    # Windows 版本资源使用整数元组，因此 0.2.13 会被准确写成 (0, 2, 13, 0)。
    $resource = @"
# UTF-8
# 此文件由 build.ps1 根据 VERSION 自动生成，请勿单独修改版本号。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($versionMajor, $versionMinor, $versionPatch, 0),
    prodvers=($versionMajor, $versionMinor, $versionPatch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404b0',
        [
          StringStruct(u'CompanyName', u'ForTest'),
          StringStruct(u'FileDescription', u'ForTest 测试与部署工具'),
          StringStruct(u'FileVersion', u'$version'),
          StringStruct(u'InternalName', u'ForTest'),
          StringStruct(u'LegalCopyright', u'Copyright (C) 2026'),
          StringStruct(u'OriginalFilename', u'ForTest.exe'),
          StringStruct(u'ProductName', u'ForTest'),
          StringStruct(u'ProductVersion', u'$version')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"@
    [System.IO.File]::WriteAllText(
        $TargetPath,
        $resource,
        [System.Text.UTF8Encoding]::new($false)
    )
}


function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$WorkingDirectory = $nativeRoot,
        [int]$TimeoutSeconds = 180,
        [switch]$Hidden
    )
    $options = @{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
        WorkingDirectory = $WorkingDirectory
        PassThru = $true
    }
    if ($Hidden) {
        $options['WindowStyle'] = 'Hidden'
    }
    $process = Start-Process @options
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill($true) } catch { }
        throw "Process timed out: $FilePath"
    }
    if ($process.ExitCode -ne 0) {
        throw "Process failed with exit code $($process.ExitCode): $FilePath"
    }
}


function Assert-X64PortableExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) {
            throw "Invalid Windows executable: $Path"
        }
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Missing PE signature: $Path"
        }
        $machine = $reader.ReadUInt16()
        if ($machine -ne 0x8664) {
            throw ('Artifact is not AMD64, Machine=0x{0:X4}: {1}' -f $machine, $Path)
        }
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}


if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw 'Build requires 64-bit Windows and 64-bit PowerShell.'
}

Write-VersionResource -TargetPath (Join-Path $nativeRoot 'version_info.txt')

if ($Clean) {
    Write-Step 'Cleaning native desktop build artifacts'
    Remove-SafeTree -Path $venvRoot
    Remove-SafeTree -Path $buildRoot
    Remove-SafeTree -Path $distRoot
}

New-Item -ItemType Directory -Path $buildRoot, $distRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Step 'Creating isolated 64-bit Python build environment'
    & python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the native desktop build environment.'
    }
}

Write-Step 'Installing native desktop and build dependencies'
$requirements = Join-Path $nativeRoot 'requirements-build.txt'
$requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLowerInvariant()
$runtimeRequirements = Join-Path $nativeRoot 'requirements-runtime.txt'
$rootRequirements = Join-Path $projectRoot 'requirements.txt'
$combinedHash = (
    $requirementsHash +
    (Get-FileHash -LiteralPath $runtimeRequirements -Algorithm SHA256).Hash.ToLowerInvariant() +
    (Get-FileHash -LiteralPath $rootRequirements -Algorithm SHA256).Hash.ToLowerInvariant()
)
$stamp = Join-Path $venvRoot '.dependencies.sha256'
$installedHash = if (Test-Path -LiteralPath $stamp) {
    (Get-Content -LiteralPath $stamp -Raw).Trim()
} else { '' }
if ($installedHash -ne $combinedHash) {
    $oldGitConfigCount = $env:GIT_CONFIG_COUNT
    $oldGitConfigKey = $env:GIT_CONFIG_KEY_0
    $oldGitConfigValue = $env:GIT_CONFIG_VALUE_0
    try {
        # Enable long paths only for the git process launched by pip.
        $env:GIT_CONFIG_COUNT = '1'
        $env:GIT_CONFIG_KEY_0 = 'core.longpaths'
        $env:GIT_CONFIG_VALUE_0 = 'true'
        & $pythonExe -m pip install --disable-pip-version-check --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }
        & $pythonExe -m pip install --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies.' }
        # 同一隔离环境补齐共享业务核心的回归依赖。
        & $pythonExe -m pip install --disable-pip-version-check -r $rootRequirements
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install core regression dependencies.' }
        # SDK 标签的元数据可能暂时声明较旧内嵌 CLI；显式覆盖到支持 GPT-5.6
        # 模型目录的稳定版本，且不让依赖解析器降级已经安装的 Python SDK。
        & $pythonExe -m pip install --disable-pip-version-check --upgrade `
            --force-reinstall --no-deps openai-codex-cli-bin==0.144.4
        if ($LASTEXITCODE -ne 0) { throw 'Failed to pin the Codex CLI runtime.' }
        $combinedHash | Set-Content -LiteralPath $stamp -Encoding ascii
    }
    finally {
        $env:GIT_CONFIG_COUNT = $oldGitConfigCount
        $env:GIT_CONFIG_KEY_0 = $oldGitConfigKey
        $env:GIT_CONFIG_VALUE_0 = $oldGitConfigValue
    }
} else {
    Write-Host 'Dependency versions unchanged; reusing isolated environment.' -ForegroundColor DarkGray
}

$architecture = & $pythonExe -c "import struct; print(struct.calcsize('P') * 8)"
if (($architecture | Select-Object -Last 1).Trim() -ne '64') {
    throw 'Build environment does not use 64-bit Python.'
}

Write-Step 'Auditing source defaults and package inputs for private data'
$sourcePrivacyReport = Join-Path $buildRoot 'package-privacy-source.json'
& $pythonExe (Join-Path $nativeRoot 'package_privacy.py') source `
    --project-root $projectRoot `
    --report $sourcePrivacyReport
if ($LASTEXITCODE -ne 0) { throw 'Source privacy audit failed.' }

if (-not $SkipTests) {
    Write-Step 'Running native unit and offscreen UI tests'
    $oldQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = 'offscreen'
        & $pythonExe -m pytest (Join-Path $nativeRoot 'tests') -q
        if ($LASTEXITCODE -ne 0) { throw 'Native desktop tests failed.' }
    }
    finally {
        $env:QT_QPA_PLATFORM = $oldQtPlatform
    }

    Write-Step 'Running shared business-core regression tests'
    & $pythonExe -m pytest (Join-Path $projectRoot 'tests') -q
    if ($LASTEXITCODE -ne 0) { throw 'Shared business-core regression failed.' }

}

Write-Step 'Building the pure native Windows x64 application'
$pyinstallerWork = Join-Path $buildRoot 'pyinstaller'
$basePythonRoot = (& $pythonExe -c 'import sys; print(sys.base_prefix)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $basePythonRoot) {
    throw 'Failed to resolve the base Python runtime.'
}
$isolatedBuildPath = @(
    (Join-Path $venvRoot 'Scripts'),
    $basePythonRoot,
    (Join-Path $basePythonRoot 'Scripts'),
    (Join-Path $env:SystemRoot 'System32'),
    $env:SystemRoot,
    (Join-Path $env:SystemRoot 'System32\Wbem'),
    (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$previousBuildPath = $env:PATH
try {
    # 只在打包阶段隔离 PATH，避免宿主工具链中的同名 ICU/OpenSSL DLL 被误收集。
    $env:PATH = $isolatedBuildPath -join [System.IO.Path]::PathSeparator
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $distRoot `
        --workpath $pyinstallerWork `
        (Join-Path $nativeRoot 'ForTest.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
}
finally {
    $env:PATH = $previousBuildPath
}

$analysisToc = Join-Path $pyinstallerWork 'ForTest\Analysis-00.toc'
if (
    -not (Test-Path -LiteralPath $analysisToc) -or
    (Select-String -LiteralPath $analysisToc -SimpleMatch 'codex-runtimes' -Quiet)
) {
    # 分析清单必须存在，且绝不能引用 Codex 主机自带的工作区依赖。
    throw 'PyInstaller dependency isolation check failed.'
}

$packagedExe = Join-Path $distRoot 'ForTest\ForTest.exe'
if (-not (Test-Path -LiteralPath $packagedExe)) {
    throw "Packaged application was not created: $packagedExe"
}
Assert-X64PortableExecutable -Path $packagedExe

Write-Step 'Auditing packaged modules and files for private data'
$artifactPrivacyReport = Join-Path $buildRoot 'package-privacy-artifact.json'
& $pythonExe (Join-Path $nativeRoot 'package_privacy.py') artifact `
    --project-root $projectRoot `
    --artifact-root (Join-Path $distRoot 'ForTest') `
    --executable $packagedExe `
    --report $artifactPrivacyReport
if ($LASTEXITCODE -ne 0) { throw 'Packaged privacy audit failed.' }

Write-Step 'Running packaged local backup diagnostics'
$backupDiagnostics = Join-Path $buildRoot 'packaged-backup-diagnostics.json'
if (Test-Path -LiteralPath $backupDiagnostics) {
    Remove-Item -LiteralPath $backupDiagnostics -Force
}
Invoke-CheckedProcess `
    -FilePath $packagedExe `
    -ArgumentList @('--backup-smoke-test', '--diagnostics-file', "`"$backupDiagnostics`"") `
    -WorkingDirectory (Split-Path -Parent $packagedExe) `
    -TimeoutSeconds 30 `
    -Hidden
if (-not (Test-Path -LiteralPath $backupDiagnostics)) {
    throw 'Packaged application did not write local backup diagnostics.'
}
$backupView = Get-Content -LiteralPath $backupDiagnostics -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $backupView.success -or $backupView.error_type) {
    throw 'Packaged local backup diagnostics failed.'
}

Write-Step 'Running packaged native startup diagnostics'
$diagnostics = Join-Path $buildRoot 'packaged-diagnostics.json'
if (Test-Path -LiteralPath $diagnostics) {
    Remove-Item -LiteralPath $diagnostics -Force
}
Invoke-CheckedProcess `
    -FilePath $packagedExe `
    -ArgumentList @('--smoke-test', '--diagnostics-file', "`"$diagnostics`"") `
    -WorkingDirectory (Split-Path -Parent $packagedExe) `
    -TimeoutSeconds 60 `
    -Hidden
if (-not (Test-Path -LiteralPath $diagnostics)) {
    throw 'Packaged application did not write diagnostics.'
}
$diagnosticView = Get-Content -LiteralPath $diagnostics -Raw -Encoding utf8 | ConvertFrom-Json
if (
    $diagnosticView.architecture_bits -ne 64 -or
    -not $diagnosticView.native_qt -or
    -not $diagnosticView.diagnostics_isolated -or
    $diagnosticView.backend_runtime_loaded -or
    $diagnosticView.codex_runtime_loaded -or
    $diagnosticView.splash_first_paint_seconds -ge 1.5 -or
    $diagnosticView.first_paint_seconds -ge 3.0 -or
    $diagnosticView.startup_heartbeat_ticks -le 0 -or
    -not $diagnosticView.deployment_ready_before_main -or
    $diagnosticView.web_server_started
) {
    throw 'Packaged architecture diagnostics failed.'
}

Write-Step 'Running packaged full startup readiness diagnostics'
$fullStartupDiagnostics = Join-Path $buildRoot 'packaged-full-startup-diagnostics.json'
if (Test-Path -LiteralPath $fullStartupDiagnostics) {
    Remove-Item -LiteralPath $fullStartupDiagnostics -Force
}
Invoke-CheckedProcess `
    -FilePath $packagedExe `
    -ArgumentList @('--full-startup-smoke', '--diagnostics-file', "`"$fullStartupDiagnostics`"") `
    -WorkingDirectory (Split-Path -Parent $packagedExe) `
    -TimeoutSeconds 60 `
    -Hidden
if (-not (Test-Path -LiteralPath $fullStartupDiagnostics)) {
    throw 'Packaged application did not write full startup diagnostics.'
}
$fullStartupView = Get-Content -LiteralPath $fullStartupDiagnostics -Raw -Encoding utf8 | ConvertFrom-Json
if (
    -not $fullStartupView.diagnostics_isolated -or
    -not $fullStartupView.full_startup_smoke -or
    -not $fullStartupView.backend_runtime_loaded -or
    -not $fullStartupView.startup_preload_complete -or
    -not $fullStartupView.startup_snapshot_applied -or
    -not $fullStartupView.backend_ready_before_main -or
    -not $fullStartupView.deployment_ready_before_main -or
    $fullStartupView.startup_heartbeat_ticks -le 0 -or
    $fullStartupView.startup_max_heartbeat_gap_seconds -ge 2.5 -or
    $fullStartupView.post_show_heartbeat_ticks -lt 20 -or
    $fullStartupView.post_show_max_heartbeat_gap_seconds -ge 0.5 -or
    $fullStartupView.post_show_threadpool_peak -ne 0 -or
    $fullStartupView.startup_seconds -ge 45
) {
    throw 'Packaged full startup readiness diagnostics failed.'
}

if (-not $SkipInstaller) {
    Write-Step 'Building the installer with Inno Setup'
    $isccCandidates = @(
        (Join-Path $nativeRoot '.tools\Inno Setup 7\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    $isccExe = $isccCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $isccExe) {
        throw 'Inno Setup compiler was not found.'
    }
    & $isccExe "/DMyAppVersion=$version" (Join-Path $nativeRoot 'installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
    $installer = Join-Path $distRoot "installer\ForTest-Windows-x64-Setup-$version.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Installer was not created: $installer"
    }
    Assert-X64PortableExecutable -Path $installer
}

Write-Step "Build completed: ForTest $version native Windows x64"
