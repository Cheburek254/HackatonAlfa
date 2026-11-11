param(
    [string]$Catalog,
    [string]$Backup,
    [string]$OutputDir,
    [ValidateRange(1, 32)][int]$Jobs = 4,
    [ValidateRange(1, 10000)][int]$Samples = 180,
    [ValidateRange(0, 10000)][int]$Refine = 60,
    [switch]$Exhaustive,
    [string]$GridStep,
    [switch]$CountOnly,
    [switch]$Smoke
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $CountOnly -and -not $Catalog) {
    $Catalog = @(
        (Join-Path $projectRoot 'data/catalog_slim.sqlite')
        (Join-Path $projectRoot 'data/catalog_slim.json')
        (Join-Path $projectRoot 'data/catalog.norm.json')
        (Join-Path $projectRoot '.venv/ab-release/catalog_slim.sqlite')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $CountOnly -and -not $Backup) {
    $Backup = @(
        (Join-Path $projectRoot 'data/alfa_course_hackathon.backup')
        (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads/alfa_course_hackathon.backup')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
$options = @('--jobs', $Jobs, '--samples', $Samples, '--refine', $Refine)
if ($Exhaustive) { $options += '--exhaustive' }
if ($GridStep) { $options += @('--grid-step', $GridStep) }
if ($CountOnly) { $options += '--count-only' }
if ($Smoke) { $options += '--smoke' }
if (-not $CountOnly) {
    if (-not $Catalog -or -not $Backup) {
        throw 'Pass existing catalog and backup files with -Catalog and -Backup.'
    }
    $Catalog = (Resolve-Path -LiteralPath $Catalog).Path
    $Backup = (Resolve-Path -LiteralPath $Backup).Path
    if (-not $OutputDir) {
        $directory = if ($GridStep) { "data/bm25-deep-grid-$GridStep" } elseif ($Exhaustive) {
            'data/bm25-deep-grid'
        } elseif ($Smoke) { 'data/bm25-deep-smoke' } else { 'data/bm25-deep' }
        $OutputDir = Join-Path $projectRoot $directory
    }
    $options += @('--catalog', $Catalog, '--backup', $Backup, '--output-dir', [IO.Path]::GetFullPath($OutputDir))
}
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python).Source
}
Push-Location -LiteralPath $projectRoot
$previousPythonPath = $env:PYTHONPATH
try {
    $qualityDeps = Join-Path $projectRoot '.venv/quality-deps'
    if (Test-Path -LiteralPath (Join-Path $qualityDeps 'numpy') -PathType Container) {
        $env:PYTHONPATH = if ($previousPythonPath) {
            "$qualityDeps$([IO.Path]::PathSeparator)$previousPythonPath"
        } else { $qualityDeps }
    }
    & $python -X utf8 -m evaluation.tune_deep @options
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}
