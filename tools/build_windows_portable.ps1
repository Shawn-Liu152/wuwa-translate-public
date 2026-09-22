param(
  [string]$OutputRoot = "",
  [string]$UvPath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = (Get-Content -LiteralPath (Join-Path $sourceRoot "VERSION") -Raw).Trim()

if (-not $OutputRoot) {
  $OutputRoot = Join-Path $sourceRoot "..\release"
}
$packageRoot = [System.IO.Path]::GetFullPath($OutputRoot)

function Get-CheckedChildPath {
  param(
    [Parameter(Mandatory = $true)][string]$Parent,
    [Parameter(Mandatory = $true)][string]$Child,
    [Parameter(Mandatory = $true)][string]$Label
  )
  $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar
  )
  $childFull = [System.IO.Path]::GetFullPath($Child)
  $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
  if (-not $childFull.StartsWith(
    $prefix, [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Refusing to use $Label outside output root: $childFull"
  }
  return $childFull
}

$target = Get-CheckedChildPath `
  -Parent $packageRoot `
  -Child (Join-Path $packageRoot "subtitle-pipeline-$version-windows-x64-portable") `
  -Label "portable target"
$cacheRoot = Get-CheckedChildPath `
  -Parent $packageRoot `
  -Child (Join-Path $packageRoot ".portable-cache") `
  -Label "portable cache"
$sourceStageRoot = Get-CheckedChildPath `
  -Parent $packageRoot `
  -Child (Join-Path $packageRoot (".portable-source-" + [Guid]::NewGuid().ToString("N"))) `
  -Label "source stage"

$trackedBuildInputs = @(
  "CHANGELOG.md",
  "README.md",
  "THIRD-PARTY-NOTICES.md",
  "VERSION",
  "WINDOWS-PORTABLE.md",
  "docs/assets/demo-workbench.png",
  "pipeline",
  "stop_web.bat",
  "tools/build_github_package.ps1",
  "tools/audit_release_sqlite.py",
  "tools/build_windows_portable.ps1",
  "tools/windows_portable_requirements.in",
  "tools/windows_portable_requirements.lock",
  "web"
)
& git -C $sourceRoot diff --quiet HEAD -- $trackedBuildInputs
if ($LASTEXITCODE -ne 0) {
  throw "Portable build inputs must be committed before packaging."
}

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
if (Test-Path -LiteralPath $target) {
  Remove-Item -LiteralPath $target -Recurse -Force
}
New-Item -ItemType Directory -Path $target -Force | Out-Null

function Get-VerifiedDownload {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][string]$Sha256
  )
  $expected = $Sha256.ToLowerInvariant()
  if (Test-Path -LiteralPath $Destination) {
    $cached = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($cached -eq $expected) {
      return
    }
    Remove-Item -LiteralPath $Destination -Force
  }
  Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
  $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) {
    Remove-Item -LiteralPath $Destination -Force
    throw "Downloaded component failed SHA-256 verification: $Url"
  }
}

$pythonVersion = "3.13.15"
$pythonSha256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
$pythonArchive = Join-Path $cacheRoot "python-$pythonVersion-embed-amd64.zip"

$denoVersion = "2.9.5"
$denoSha256 = "171efab55ac6b9881fd53ee4c20f8bf3bb1340ffc618483746909014db12216a"
$denoUrl = "https://github.com/denoland/deno/releases/download/v$denoVersion/deno-x86_64-pc-windows-msvc.zip"
$denoArchive = Join-Path $cacheRoot "deno-$denoVersion-x86_64-pc-windows-msvc.zip"

try {
  & (Join-Path $PSScriptRoot "build_github_package.ps1") `
    -OutputRoot $sourceStageRoot -DirectoryOnly
  if ($LASTEXITCODE -ne 0) {
    throw "Sanitized source staging failed."
  }
  $sourcePackage = Join-Path $sourceStageRoot "subtitle-pipeline-$version"
  if (-not (Test-Path -LiteralPath $sourcePackage -PathType Container)) {
    throw "Sanitized source staging did not produce the expected directory."
  }

  $runtimeRoots = @(
    "CHANGELOG.md",
    "README.md",
    "THIRD-PARTY-NOTICES.md",
    "VERSION",
    "WINDOWS-PORTABLE.md",
    "data",
    "docs",
    "pipeline",
    "stop_web.bat",
    "web"
  )
  foreach ($relativePath in $runtimeRoots) {
    $sourcePath = Join-Path $sourcePackage $relativePath
    if (-not (Test-Path -LiteralPath $sourcePath)) {
      throw "Required portable input is missing: $relativePath"
    }
    Copy-Item -LiteralPath $sourcePath -Destination $target -Recurse -Force
  }

  $runtimeRoot = Join-Path $target "runtime"
  $pythonRoot = Join-Path $runtimeRoot "python"
  $binRoot = Join-Path $runtimeRoot "bin"
  $sitePackages = Join-Path $pythonRoot "Lib\site-packages"
  New-Item -ItemType Directory -Path $pythonRoot -Force | Out-Null
  New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
  New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null

  Get-VerifiedDownload -Url $pythonUrl -Destination $pythonArchive -Sha256 $pythonSha256
  Expand-Archive -LiteralPath $pythonArchive -DestinationPath $pythonRoot -Force
  $runtimePython = Join-Path $pythonRoot "python.exe"
  if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    throw "Official Python embeddable runtime is incomplete."
  }
  $pthFile = Get-ChildItem -LiteralPath $pythonRoot -Filter "python*._pth" -File |
    Select-Object -First 1
  if (-not $pthFile) {
    throw "Official Python embeddable runtime did not contain a ._pth file."
  }
  @(
    "python313.zip",
    ".",
    "Lib\site-packages",
    "..\..",
    "import site"
  ) | Set-Content -LiteralPath $pthFile.FullName -Encoding ascii

  $requirementsLock = Join-Path $sourceRoot "tools\windows_portable_requirements.lock"
  Copy-Item -LiteralPath $requirementsLock `
    -Destination (Join-Path $runtimeRoot "requirements.lock") -Force
  if (-not $UvPath) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
      $UvPath = $uvCommand.Source
    }
  }
  if (-not $UvPath -or -not (Test-Path -LiteralPath $UvPath -PathType Leaf)) {
    throw "uv was not found. Pass -UvPath with the uv.exe location."
  }
  & $UvPath pip install `
    --python $runtimePython `
    --target $sitePackages `
    --require-hashes `
    --only-binary=:all: `
    --no-compile `
    --cache-dir (Join-Path $cacheRoot "uv") `
    --requirements $requirementsLock
  if ($LASTEXITCODE -ne 0) {
    throw "Hash-locked Python dependency installation failed."
  }

  Get-VerifiedDownload -Url $denoUrl -Destination $denoArchive -Sha256 $denoSha256
  Expand-Archive -LiteralPath $denoArchive -DestinationPath $binRoot -Force
  if (-not (Test-Path -LiteralPath (Join-Path $binRoot "deno.exe") -PathType Leaf)) {
    throw "Official Deno archive did not contain deno.exe."
  }
  if (Get-ChildItem -LiteralPath $binRoot -Filter "ffmpeg*.exe" -File) {
    throw "FFmpeg must not enter the Core portable package."
  }

  $startScript = @'
@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8:replace"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONHOME="
set "PYTHONPATH="
set "SUBTITLE_PORTABLE=1"
set "SUBTITLE_DISTRIBUTION=windows-x64-portable-core"
set "PATH=%~dp0runtime\bin;%PATH%"
if not exist "%~dp0runtime\python\python.exe" (
  echo Portable Python runtime is missing. Extract the complete ZIP and try again.
  pause
  exit /b 1
)
"%~dp0runtime\python\python.exe" -I -m web.launcher --project-root "%~dp0."
if errorlevel 1 pause
'@
  [System.IO.File]::WriteAllText(
    (Join-Path $target "start_web.bat"),
    $startScript,
    [System.Text.UTF8Encoding]::new($false)
  )
  # Keep this script ASCII-only for Windows PowerShell 5.1.  Build the two
  # Chinese convenience names from Unicode code points at runtime.
  $startAliasName = (-join ([char[]]@(
    21551, 21160, 32763, 35793, 24037, 20316, 21488
  ))) + ".bat"
  $stopAliasName = (-join ([char[]]@(
    20572, 27490, 32763, 35793, 24037, 20316, 21488
  ))) + ".bat"
  Copy-Item -LiteralPath (Join-Path $target "start_web.bat") `
    -Destination (Join-Path $target $startAliasName) -Force
  Copy-Item -LiteralPath (Join-Path $target "stop_web.bat") `
    -Destination (Join-Path $target $stopAliasName) -Force

  $lockedPackages = @(
    Get-Content -LiteralPath $requirementsLock |
      ForEach-Object {
        if ($_ -match '^([A-Za-z0-9_.-]+)==([^\s\\]+)') {
          [PSCustomObject]@{ name = $Matches[1]; version = $Matches[2] }
        }
      }
  )
  $components = [PSCustomObject]@{
    package = "subtitle-pipeline-windows-x64-portable-core"
    version = $version
    python = [PSCustomObject]@{
      version = $pythonVersion
      url = $pythonUrl
      sha256 = $pythonSha256
    }
    deno = [PSCustomObject]@{
      version = $denoVersion
      url = $denoUrl
      sha256 = $denoSha256
    }
    ffmpeg_bundled = $false
    whisper_bundled = $false
    python_packages = $lockedPackages
  }
  $components | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $target "RUNTIME-COMPONENTS.json") -Encoding utf8

  $forbidden = Get-ChildItem -LiteralPath $target -Recurse -Force | Where-Object {
    $relativePath = $_.FullName.Substring($target.Length + 1)
    $_.Name -in @(".env", "youtube_cookies.txt", "youtube.cookies.txt", "job.json", "translation_memory.json") -or
    $_.Name -match "(?i)(\.bak($|-)|\.backup($|-)|pre-language-migration)" -or
    $relativePath -match "(?i)^(download|reports|tests|tools|\.git)([\\/]|$)"
  }
  if ($forbidden) {
    throw "Private or developer-only files entered the portable package: $($forbidden.FullName -join ', ')"
  }

  $applicationTextFiles = @(
    Get-ChildItem -LiteralPath $target -Recurse -File |
      Where-Object {
        $_.FullName -notlike ((Join-Path $runtimeRoot "*") ) -and
        $_.Length -le 10MB -and
        $_.Extension.ToLowerInvariant() -in @(
          ".bat", ".css", ".html", ".ini", ".js", ".json", ".md",
          ".py", ".srt", ".svg", ".txt", ".yaml", ".yml"
        )
      }
  )
  $secretPatterns = @(
    "sk-[A-Za-z0-9_-]{20,}",
    "gh[pousr]_[A-Za-z0-9]{20,}",
    "AIza[0-9A-Za-z_-]{30,}",
    "AKIA[0-9A-Z]{16}",
    "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"
  )
  $secretHits = $applicationTextFiles | Select-String -Pattern $secretPatterns -List
  if ($secretHits) {
    throw "Possible secret found in portable package: $($secretHits.Path -join ', ')"
  }
  $privacyPatterns = @(
    (("(?i)[A-Z]:[\\/]+Use" + "rs[\\/]+") + '[^\\/\s"''<>]+'),
    (("(?i)/Use" + "rs/") + '[^/\s"''<>]+'),
    (("(?i)/ho" + "me/") + '[^/\s"''<>]+')
  )
  $privacyHits = $applicationTextFiles | Select-String -Pattern $privacyPatterns -List
  if ($privacyHits) {
    throw "Personal path found in portable package: $($privacyHits.Path -join ', ')"
  }
  $localMarker = [Environment]::UserName
  $databaseAuditor = Join-Path $sourceRoot "tools\audit_release_sqlite.py"
  $releaseDatabases = @(Get-ChildItem -LiteralPath $target -Recurse -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in @(".db", ".sqlite", ".sqlite3") })
  foreach ($database in $releaseDatabases) {
    & $runtimePython -I $databaseAuditor $database.FullName `
      --local-marker $localMarker
    if ($LASTEXITCODE -ne 0) {
      throw "Private data pattern found in bundled database: $($database.Name)"
    }
  }
  if ($localMarker -and $localMarker.Length -ge 3) {
    $markerPattern = "(?i)(?<![A-Z0-9])" + [Regex]::Escape($localMarker) + "(?![A-Z0-9])"
    $markerHits = $applicationTextFiles | Select-String -Pattern $markerPattern -List
    if ($markerHits) {
      throw "Local account marker found in portable package: $($markerHits.Path -join ', ')"
    }
  }

  $manifestEntries = Get-ChildItem -LiteralPath $target -Recurse -File |
    Sort-Object FullName |
    ForEach-Object {
      [PSCustomObject]@{
        path = $_.FullName.Substring($target.Length + 1).Replace("\", "/")
        bytes = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
      }
    }
  [PSCustomObject]@{
    package = "subtitle-pipeline-windows-x64-portable-core"
    version = $version
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    file_count = @($manifestEntries).Count
    files = @($manifestEntries)
  } | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $target "PACKAGE-MANIFEST.json") -Encoding utf8

  $zipPath = Join-Path $packageRoot "subtitle-pipeline-$version-windows-x64-portable.zip"
  $checksumPath = $zipPath + ".sha256"
  if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
  }
  if (Test-Path -LiteralPath $checksumPath) {
    Remove-Item -LiteralPath $checksumPath -Force
  }
  Compress-Archive -LiteralPath $target -DestinationPath $zipPath -CompressionLevel Optimal
  $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
  ($zipHash + "  " + [System.IO.Path]::GetFileName($zipPath)) |
    Set-Content -LiteralPath $checksumPath -Encoding ascii

  Write-Host "Windows portable package ready:"
  Write-Host $zipPath
  Write-Host "SHA-256: $zipHash"
  Write-Host "Files: $(@($manifestEntries).Count + 1)"
} finally {
  if (Test-Path -LiteralPath $sourceStageRoot) {
    $verifiedStage = Get-CheckedChildPath `
      -Parent $packageRoot -Child $sourceStageRoot -Label "source stage cleanup"
    Remove-Item -LiteralPath $verifiedStage -Recurse -Force
  }
}
