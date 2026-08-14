[CmdletBinding()]
param(
  [string]$Project = "workama"
)

$ErrorActionPreference = "Stop"
if ($Project -notmatch '^workama(?:$|[-_][a-z0-9][a-z0-9_-]*)$') {
  throw "Project must be workama-prefixed."
}

docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm platform-api pytest -q tests/test_mcp_protocol.py
if ($LASTEXITCODE -ne 0) { throw "MCP transport tests failed with exit code $LASTEXITCODE" }

$evidence = [ordered]@{
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
  project = $Project
  stdio_controlled_session = $true
  sse_endpoint_response_events = $true
  ssrf_and_redirect_guards = $true
  bounded_reads_and_timeout = $true
  subprocess_policy_and_cleanup = $true
  credential_non_disclosure = $true
  verification_scope = 'docker-targeted-unit-and-transport-tests'
  pending_external = $true
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'quality/evidence/mcp-transport-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 8
