$ErrorActionPreference = 'Stop'

$root = (Get-Location).Path
$evidence = (Resolve-Path 'quality/evidence').Path
$image = 'workama-browser-gate:local'

& docker build -f tools/browser-gate.Dockerfile -t $image .
if ($LASTEXITCODE -ne 0) { throw "browser gate image build failed with exit code $LASTEXITCODE" }

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  $output = @(& docker run --rm --network host `
    -e 'BROWSER_BASE_URL=http://localhost:20204' `
    -e 'PWA_BASE_URL=http://localhost:20204' `
    -e 'BROWSER_EXECUTABLE=/usr/bin/chromium-browser' `
    -e 'EVIDENCE_DIR=quality/evidence/web-react-final' `
    -v "${evidence}:/workspace/quality/evidence" `
    $image sh -lc 'pnpm --filter @workama/web preview --host 0.0.0.0 --port 4173 >/tmp/workama-web-preview.log 2>&1 & node apps/mobile/tests/pwa-smoke.mjs && node apps/web/tests/pwa-smoke.mjs && node apps/web/tests/browser-smoke.mjs && EVIDENCE_DIR=quality/evidence/operations-react-final node apps/web/tests/operations-smoke.mjs' 2>&1)
  $exitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $previousErrorAction
}
if ($exitCode -ne 0) {
  throw "browser gate failed with exit code ${exitCode}`n$($output -join "`n")"
}
$output
