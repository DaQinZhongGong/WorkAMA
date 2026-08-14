# Builds the React renderer from apps/web into apps/desktop/dist for Electron to load.
#
# Run this before `pnpm dist` to ensure the renderer is available in the package.
# Usage:  pnpm build-renderer    (or:  pwsh scripts/build-renderer.ps1)

$ErrorActionPreference = 'Stop'

# Resolve repo root from scripts/ directory: scripts -> desktop -> apps -> repo root
$repoRoot   = Resolve-Path (Join-Path $PSScriptRoot '../..')
$webDir     = Join-Path $repoRoot 'apps/web'
$desktopDir = Join-Path $repoRoot 'apps/desktop'
$outDir     = Join-Path $desktopDir 'dist'

Write-Host "[build-renderer] Building React renderer from apps/web -> apps/desktop/dist ..."

if (-not (Test-Path $webDir)) {
    throw "apps/web not found at $webDir"
}

Push-Location $webDir
try {
    # pnpm build with Vite --outDir to output into apps/desktop/dist
    pnpm build -- --outDir "$outDir"
} finally {
    Pop-Location
}

Write-Host "[build-renderer] Renderer build complete -> $outDir"
