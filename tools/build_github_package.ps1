param(
  [string]$OutputRoot = "",
  [switch]$DirectoryOnly
)

$ErrorActionPreference = "Stop"
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = (Get-Content -LiteralPath (Join-Path $sourceRoot "VERSION") -Raw).Trim()
if (-not $OutputRoot) {
  $OutputRoot = Join-Path $sourceRoot "github-package"
}

$packageRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$target = [System.IO.Path]::GetFullPath(
  (Join-Path $packageRoot "subtitle-pipeline-$version")
)
$expectedPrefix = $packageRoot.TrimEnd(
  [System.IO.Path]::DirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith(
  $expectedPrefix,
  [System.StringComparison]::OrdinalIgnoreCase
)) {
  throw "Refusing to build outside the requested package root: $target"
}

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
if (Test-Path -LiteralPath $target) {
  Remove-Item -LiteralPath $target -Recurse -Force
}
New-Item -ItemType Directory -Path $target -Force | Out-Null

$topLevelFiles = @(
  ".gitignore",
  "CHANGELOG.md",
  "docs\assets\demo-workbench.png",
  "README.md",
  "THIRD-PARTY-NOTICES.md",
  "VERSION",
  "WINDOWS-PORTABLE.md",
  "install_web.bat",
  "install_whisper.bat",
  "pytest.ini",
  "requirements.txt",
  "start_web.bat",
  "stop_web.bat",
  "web_requirements.txt",
  "whisper_requirements.txt"
)
$directories = @(
  "data",
  "examples",
  "pipeline",
  "tests",
  "tools",
  "web"
)

$archivePath = Join-Path $packageRoot (
  ".subtitle-pipeline-" + [Guid]::NewGuid().ToString("N") + ".zip"
)
$optionalDemo = "docs\assets\demo-workbench.png"
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $optionalDemo))) {
  $topLevelFiles = @($topLevelFiles | Where-Object { $_ -ne $optionalDemo })
}
$releaseRoots = @($topLevelFiles) + @($directories)
try {
  & git -C $sourceRoot rev-parse --is-inside-work-tree 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Release packaging requires a Git checkout."
  }
  # Archive HEAD rather than copying the dirty worktree. This excludes local
  # reports, database backups, secrets, and untracked helper scripts by design.
  & git -C $sourceRoot archive --format=zip "--output=$archivePath" HEAD -- @releaseRoots
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archivePath)) {
    throw "git archive failed for committed HEAD."
  }
  Expand-Archive -LiteralPath $archivePath -DestinationPath $target -Force
} finally {
  if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
  }
}

# Internal audits and handoff notes describe the owner's local workflow. They
# are intentionally absent from the shareable package even when tracked.
$internalReleasePaths = @(
  "docs\archive",
  "docs\audit_tables",
  "docs\tasks",
  "docs\ASR_CORRECTION_AUDIT.md",
  "docs\CODEX_CONTEXT.md",
  "docs\DEAD_FILE_AUDIT.md",
  "docs\HANDOFF_NEXT_WINDOW.md",
  "docs\OPTIMIZATION_ROADMAP.md",
  "docs\PROJECT_INVENTORY.md"
)
foreach ($relativePath in $internalReleasePaths) {
  $internalPath = Join-Path $target $relativePath
  if (Test-Path -LiteralPath $internalPath) {
    Remove-Item -LiteralPath $internalPath -Recurse -Force
  }
}

# Translation Memory contains local human review data and is intentionally not
# distributed even when the schema file is tracked.
$translationMemory = Join-Path $target "data\translation_memory.json"
if (Test-Path -LiteralPath $translationMemory) {
  Remove-Item -LiteralPath $translationMemory -Force
}

$forbiddenNames = @(
  ".env",
  "youtube_cookies.txt",
  "youtube.cookies.txt",
  "job.json"
)
$forbidden = Get-ChildItem -LiteralPath $target -Recurse -File | Where-Object {
  $relativePackagePath = $_.FullName.Substring($target.Length + 1)
  $_.Name -in $forbiddenNames -or
  $_.Name -match "(?i)(\.bak($|-)|\.backup($|-)|pre-language-migration)" -or
  $relativePackagePath -match "(^|[\\/])download([\\/]|$)" -or
  (
    $relativePackagePath -match "(^|[\\/])docs([\\/]|$)" -and
    $relativePackagePath -ne "docs\assets\demo-workbench.png"
  )
}
if ($forbidden) {
  throw "Sensitive runtime files entered the package: $($forbidden.FullName -join ', ')"
}

$textExtensions = @(
  ".bat", ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsonl",
  ".md", ".ps1", ".py", ".srt", ".svg", ".toml", ".ts", ".tsv",
  ".txt", ".xml", ".yaml", ".yml"
)
$textNames = @(".gitignore", "VERSION")
$scannableFiles = @(
  Get-ChildItem -LiteralPath $target -Recurse -File |
    Where-Object {
      $_.Length -le 10MB -and (
        $_.Extension.ToLowerInvariant() -in $textExtensions -or
        $_.Name -in $textNames
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
$secretHits = $scannableFiles | Select-String -Pattern $secretPatterns -List
if ($secretHits) {
  throw "Possible secret found in release package: $($secretHits.Path -join ', ')"
}

# Reject personal home-directory paths and email addresses. Pattern strings are
# split so the gate does not match its own source code when this script is part
# of the package.
$privacyPatterns = @(
  (("(?i)[A-Z]:[\\/]+Use" + "rs[\\/]+") + '[^\\/\s"''<>]+'),
  (("(?i)/Use" + "rs/") + '[^/\s"''<>]+'),
  (("(?i)/ho" + "me/") + '[^/\s"''<>]+'),
  "(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
)
$privacyHits = $scannableFiles | Select-String -Pattern $privacyPatterns -List
if ($privacyHits) {
  throw "Personal path or email found in release package: $($privacyHits.Path -join ', ')"
}

$localUserMarker = [Environment]::UserName
if ($localUserMarker -and $localUserMarker.Length -ge 3) {
  $markerPattern = "(?i)(?<![A-Z0-9])" +
    [Regex]::Escape($localUserMarker) + "(?![A-Z0-9])"
  $markerHits = $scannableFiles | Select-String -Pattern $markerPattern -List
  if ($markerHits) {
    throw "Local account marker found in release package: $($markerHits.Path -join ', ')"
  }
}

$manifestEntries = Get-ChildItem -LiteralPath $target -Recurse -File |
  Sort-Object FullName |
  ForEach-Object {
    [PSCustomObject]@{
      path = $_.FullName.Substring($target.Length + 1).Replace("\", "/")
      bytes = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
    }
  }
$manifest = [PSCustomObject]@{
  package = "subtitle-pipeline"
  version = $version
  created_at = (Get-Date).ToString("o")
  file_count = @($manifestEntries).Count
  files = @($manifestEntries)
}
$manifest | ConvertTo-Json -Depth 5 |
  Set-Content -LiteralPath (Join-Path $target "PACKAGE-MANIFEST.json") -Encoding utf8

$zipPath = Join-Path $packageRoot (
  "subtitle-pipeline-" + $version + "-windows-beta-source.zip"
)
$checksumPath = $zipPath + ".sha256"
if (-not $DirectoryOnly) {
  if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
  }
  if (Test-Path -LiteralPath $checksumPath) {
    Remove-Item -LiteralPath $checksumPath -Force
  }
  Compress-Archive -LiteralPath $target -DestinationPath $zipPath `
    -CompressionLevel Optimal
  $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
  ($zipHash + "  " + [System.IO.Path]::GetFileName($zipPath)) |
    Set-Content -LiteralPath $checksumPath -Encoding ascii
}

Write-Host "GitHub package ready:"
Write-Host $target
Write-Host "Files: $(@($manifestEntries).Count)"
if (-not $DirectoryOnly) {
  Write-Host "Windows Beta ZIP:"
  Write-Host $zipPath
  Write-Host "SHA-256: $zipHash"
}
