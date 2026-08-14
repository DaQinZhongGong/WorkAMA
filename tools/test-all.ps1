$ErrorActionPreference = 'Stop'

function Assert-ExternalCommandSucceeded([string]$Stage) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE"
    }
}

docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python tools/docs_consistency.py --json quality/evidence/docs-consistency.json
Assert-ExternalCommandSucceeded 'docs consistency'
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python -m unittest tools/test_docs_consistency.py
Assert-ExternalCommandSucceeded 'docs consistency tests'
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python tools/runtime_surface_sync.py --check
Assert-ExternalCommandSucceeded 'runtime surface sync'
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python tools/contract_registry_check.py --json quality/evidence/contract-registry.json
Assert-ExternalCommandSucceeded 'contract registry'
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python tools/open_platform_contract_gate.py --evidence-dir quality/evidence --require-evidence --json quality/evidence/open-platform-contract-gate.json
Assert-ExternalCommandSucceeded 'open platform contract gate'
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim python -m unittest tools/test_open_platform_contract_gate.py
Assert-ExternalCommandSucceeded 'open platform contract tests'
node --test apps/extension/tests/manifest.test.mjs
Assert-ExternalCommandSucceeded 'extension security tests'
docker build -f tools/frontend-gate.Dockerfile -t workama-frontend-gate:local .
Assert-ExternalCommandSucceeded 'frontend verification'
docker compose --env-file .env -f deploy/compose/docker-compose.yml up --build -d
Assert-ExternalCommandSucceeded 'Compose build and startup'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm platform-api pytest -q
Assert-ExternalCommandSucceeded 'platform-api tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm agent-server pytest -q
Assert-ExternalCommandSucceeded 'agent-server tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/web test
Assert-ExternalCommandSucceeded 'web tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/event-renderer test
Assert-ExternalCommandSucceeded 'event-renderer tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/api-client test
Assert-ExternalCommandSucceeded 'api-client tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/i18n test
Assert-ExternalCommandSucceeded 'i18n tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/core-state test
Assert-ExternalCommandSucceeded 'core-state tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/ui test
Assert-ExternalCommandSucceeded 'ui tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/mobile test
Assert-ExternalCommandSucceeded 'mobile tests'
docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm web pnpm --filter @workama/share test
Assert-ExternalCommandSucceeded 'share tests'
docker run --rm -v "${PWD}:/src" -w /src/apps/gateway golang:1.26-alpine env GOWORK=off go test -mod=vendor -tags=pgx ./...
Assert-ExternalCommandSucceeded 'gateway tests'
docker run --rm -v "${PWD}:/src" -v workama-sandbox-go-modcache:/go/pkg/mod -v workama-sandbox-go-buildcache:/root/.cache/go-build -w /src/apps/sandbox-agentd golang:1.26-alpine env GOWORK=off go test ./...
Assert-ExternalCommandSucceeded 'sandbox-agentd tests'
$liveComposeArgs = @('--env-file', '.env', '-f', 'deploy/compose/docker-compose.yml', 'run', '--rm', '-e', 'WORKAMA_LIVE_BASE_URL=http://platform-api:8000', '-e', 'WORKAMA_GATEWAY_BASE_URL=http://gateway:8080', 'platform-api')
& docker compose @liveComposeArgs pytest -q tests/test_live_integration.py
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Live integration tests failed on the first attempt; waiting for Platform API and Gateway health before retrying once.'
    $ready = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try {
            Invoke-RestMethod -Method Get -Uri 'http://localhost:20200/readyz' -TimeoutSec 3 | Out-Null
            Invoke-RestMethod -Method Get -Uri 'http://localhost:20202/healthz' -TimeoutSec 3 | Out-Null
            $ready = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $ready) { Write-Warning 'Platform API/Gateway health did not settle before the live integration retry.' }
    & docker compose @liveComposeArgs pytest -q tests/test_live_integration.py
}
Assert-ExternalCommandSucceeded 'live integration tests'
& ./tools/smoke.ps1
& ./tools/create-test-account.ps1 | Out-Null
& ./tools/tool-smoke.ps1
& ./tools/sandbox-smoke.ps1
& ./tools/compliance-smoke.ps1
& ./tools/channel-extensions-smoke.ps1
& ./tools/approval-smoke.ps1
& ./tools/event-protocol-smoke.ps1
& ./tools/artifact-smoke.ps1
& ./tools/attachment-smoke.ps1
& ./tools/chat-shape-smoke.ps1
& ./tools/agent-loop-smoke.ps1
& ./tools/agent-runtime-limits-smoke.ps1
& ./tools/sdk-smoke.ps1
& ./tools/workflow-smoke.ps1
& ./tools/workflow-extensions-smoke.ps1
& ./tools/code-notification-smoke.ps1
& ./tools/work-smoke.ps1
& ./tools/automation-smoke.ps1
& ./tools/skills-smoke.ps1
& ./tools/connectors-smoke.ps1
& ./tools/identity-federation-smoke.ps1
& ./tools/saml-acs-smoke.ps1
& ./tools/open-platform-smoke.ps1
& ./tools/design-smoke.ps1
& ./tools/external-apps-smoke.ps1
& ./tools/gateway-prompts-smoke.ps1
& ./tools/enterprise-rbac-smoke.ps1
& ./tools/audit-export-smoke.ps1
& ./tools/a2a-smoke.ps1
& ./tools/responses-smoke.ps1
& ./tools/mcp-protocol-smoke.ps1
& ./tools/mcp-transport-smoke.ps1
& ./tools/observability-smoke.ps1
& ./tools/observability-readiness-smoke.ps1
& ./tools/subscription-smoke.ps1
& ./tools/gateway-import-smoke.ps1
& ./tools/mcp-enterprise-smoke.ps1
& ./tools/moderation-smoke.ps1
& ./tools/test-cli.ps1
& ./tools/availability-chaos-smoke.ps1
& ./tools/sla-readiness.ps1
& ./tools/sandbox-test-cleanup.ps1
& ./tools/browser-gate.ps1
Assert-ExternalCommandSucceeded 'browser and PWA gate'
$axeEvidencePath = 'quality/evidence/web-react-final/axe-wcag.json'
if (-not (Test-Path $axeEvidencePath)) {
    throw "axe WCAG evidence file not found: $axeEvidencePath"
}
$axeEvidence = Get-Content $axeEvidencePath -Raw | ConvertFrom-Json
Write-Host "axe WCAG evidence: $($axeEvidence.total_violations) violations across $($axeEvidence.routes_checked) routes (critical=$($axeEvidence.by_severity.critical), serious=$($axeEvidence.by_severity.serious), moderate=$($axeEvidence.by_severity.moderate), minor=$($axeEvidence.by_severity.minor))"
docker run --rm -v "${PWD}:/src" alpine/helm:3.16.4 lint /src/deploy/helm/workama --strict
Assert-ExternalCommandSucceeded 'Helm lint'
& ./tools/performance-baseline.ps1 -BaseUrl 'http://localhost:20202' -Endpoint '/healthz' -Duration '2s' -VUs 1
& ./tools/refresh-release-smoke.ps1
& ./tools/workama-ctl.ps1 release-check -ReleaseManifest quality/release/smoke/release-manifest.json -EvidenceDirectory quality/release/smoke -VerifyDockerImage
Assert-ExternalCommandSucceeded 'release gate'
$communityOutput = Join-Path ([System.IO.Path]::GetTempPath()) ('workama-community-release-smoke-' + [Guid]::NewGuid().ToString('N'))
& ./tools/community-release.ps1 -Action smoke -OutputDirectory $communityOutput -RunComposeConfig
Assert-ExternalCommandSucceeded 'community release smoke'
& ./quality/release/community/test-community-release.ps1
Assert-ExternalCommandSucceeded 'community release quality gate'
$env:EVIDENCE_DIR = "quality\evidence\knowledge-rag-final"
& node tools/rag-smoke.mjs
Assert-ExternalCommandSucceeded 'RAG smoke'
