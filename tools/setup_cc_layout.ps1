[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$CcRoot,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
$resolvedCc = (Resolve-Path -LiteralPath $CcRoot).Path.TrimEnd('\')
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$programRoot = Join-Path $resolvedCc "程序文件"
$expectedParent = (Resolve-Path -LiteralPath $programRoot).Path.TrimEnd('\')

if ((Split-Path -Parent $resolvedProject) -ne $expectedParent) {
    throw "ProjectRoot 必须直接位于 CcRoot\程序文件 下；脚本不会移动或删除现有目录。"
}
foreach ($required in ("start_web.bat", "stop_web.bat")) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedProject $required) -PathType Leaf)) {
        throw "ProjectRoot 缺少 $required，拒绝创建入口。"
    }
}

$downloadTarget = Join-Path $resolvedProject "download"
if (-not (Test-Path -LiteralPath $downloadTarget -PathType Container)) {
    New-Item -ItemType Directory -Path $downloadTarget | Out-Null
}

$utf8 = [System.Text.UTF8Encoding]::new($false)
$startLines = @(
    "@echo off",
    "setlocal",
    "call `"%~dp0程序文件\$([IO.Path]::GetFileName($resolvedProject))\start_web.bat`""
)
$stopLines = @(
    "@echo off",
    "setlocal",
    "call `"%~dp0程序文件\$([IO.Path]::GetFileName($resolvedProject))\stop_web.bat`""
)
[IO.File]::WriteAllText(
    (Join-Path $resolvedCc "启动工作台.bat"),
    ($startLines -join "`r`n") + "`r`n",
    $utf8
)
[IO.File]::WriteAllText(
    (Join-Path $resolvedCc "停止工作台.bat"),
    ($stopLines -join "`r`n") + "`r`n",
    $utf8
)

$downloadLink = Join-Path $resolvedCc "下载内容"
if (Test-Path -LiteralPath $downloadLink) {
    $existing = (Get-Item -LiteralPath $downloadLink -Force).Target
    if (-not $existing -or ([IO.Path]::GetFullPath([string]$existing) -ne [IO.Path]::GetFullPath($downloadTarget))) {
        throw "下载内容已存在但未指向现役 download；为保护数据，脚本不会替换它。"
    }
} else {
    New-Item -ItemType Junction -Path $downloadLink -Target $downloadTarget | Out-Null
}

Write-Host "cc 用户目录入口已创建或修复。"
