[CmdletBinding()]
param(
    [string]$EvidencePath = 'quality/evidence/external-apps-smoke.json'
)

$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$mockName = "workama-external-app-mock-$suffix"
trap {
    if ($mockName) { docker rm -f $mockName | Out-Null; $mockName = $null }
    throw $_
}
$network = docker network ls --filter 'label=com.docker.compose.project=workama' --format '{{.Name}}' | Select-Object -First 1
if (-not $network) { throw 'WorkAMA compose network was not found.' }
$mockSource = @'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/retry":
            status = 503
            response = {"error": "busy", "received": request_body}
        else:
            status = 200
            response = {"ok": True, "received": request_body, "api_key": "mock-secret"}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'@
$mockEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($mockSource))
docker run -d --rm --name $mockName --network $network --network-alias workama-external-app-mock python:3.12-slim python -c "import base64;exec(base64.b64decode('$mockEncoded'))" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to start WorkAMA external-app mock provider.' }
$mockRunning = $false
for ($attempt = 0; $attempt -lt 12; $attempt++) {
    $state = docker inspect --format '{{.State.Running}}' $mockName 2>$null
    if ($state -eq 'true') { $mockRunning = $true; break }
    Start-Sleep -Milliseconds 250
}
if (-not $mockRunning) { throw 'WorkAMA external-app mock provider did not become ready.' }
Start-Sleep -Seconds 2

function Get-ErrorStatus($errorRecord) {
    if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }
    return 0
}

$app = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Dify Smoke $suffix"; provider = 'dify'; endpoint = "mock://dify/smoke/$suffix"; credential = "smoke-credential-$suffix"; config = @{ timeout_seconds = 30 }; enabled = $true
} | ConvertTo-Json -Depth 8)
if (-not $app.id -or -not $app.credential_configured -or $app.credential -or $app.credential_hash -or $app.execution_mode -ne 'controlled_mock') { throw 'External app credential/execution handling is incomplete.' }

$invokeBody = @{ operation = 'chat'; payload = @{ message = 'hello from smoke' }; idempotency_key = "external-app-$suffix" } | ConvertTo-Json -Depth 8
$invocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($app.id)/invocations" -Headers $headers -ContentType 'application/json' -Body $invokeBody
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($app.id)/invocations" -Headers $headers -ContentType 'application/json' -Body $invokeBody
$mismatchStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($app.id)/invocations" -Headers $headers -ContentType 'application/json' -Body (@{ operation = 'chat'; payload = @{ message = 'different' }; idempotency_key = "external-app-$suffix" } | ConvertTo-Json -Depth 8) | Out-Null
} catch { $mismatchStatus = Get-ErrorStatus $_ }
if ($invocation.status -ne 'succeeded' -or $invocation.external_execution -ne 'completed' -or $invocation.execution_mode -ne 'controlled_mock' -or $invocation.idempotency_replayed -or -not $replay.idempotency_replayed -or $replay.id -ne $invocation.id -or $mismatchStatus -ne 409 -or -not $invocation.result.output_text) { throw 'Controlled external invocation idempotency contract failed.' }

$httpApp = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "HTTP Test Smoke $suffix"; provider = 'dify'; endpoint = 'http://workama-external-app-mock:8080/ok'; credential = "http-test-credential-$suffix"; config = @{ execution_mode = 'http_test'; timeout_seconds = 5; max_retries = 1; backoff_ms = 0 }; enabled = $true
} | ConvertTo-Json -Depth 8)
$httpInvokeBody = @{ operation = 'chat'; payload = @{ message = 'http test smoke' }; idempotency_key = "http-test-$suffix" } | ConvertTo-Json -Depth 8
$httpInvocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($httpApp.id)/invocations" -Headers $headers -ContentType 'application/json' -Body $httpInvokeBody
$httpReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($httpApp.id)/invocations" -Headers $headers -ContentType 'application/json' -Body $httpInvokeBody
if ($httpInvocation.status -ne 'succeeded' -or $httpInvocation.execution_mode -ne 'http_test' -or $httpInvocation.external_execution -ne 'completed' -or $httpInvocation.result.provider_request_sent -ne $true -or -not $httpReplay.idempotency_replayed -or $httpReplay.id -ne $httpInvocation.id -or $httpInvocation.result.attempts -lt 1) { throw 'Opt-in HTTP execution contract failed.' }

$retryApp = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "HTTP Retry Smoke $suffix"; provider = 'fastgpt'; endpoint = 'http://workama-external-app-mock:8080/retry'; config = @{ execution_mode = 'http_test'; timeout_seconds = 5; max_retries = 1; backoff_ms = 0 }; enabled = $true
} | ConvertTo-Json -Depth 8)
$retryInvocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($retryApp.id)/invocations" -Headers $headers -ContentType 'application/json' -Body (@{ operation = 'health'; payload = @{}; idempotency_key = "http-retry-$suffix" } | ConvertTo-Json -Depth 8)
if ($retryInvocation.status -ne 'failed' -or $retryInvocation.error_code -ne 'provider_http_503' -or $retryInvocation.attempt -ne 2 -or $retryInvocation.result.attempts -ne 2) { throw 'Bounded HTTP retry/error writeback contract failed.' }

$invalidEndpointStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{ name = "Unsafe $suffix"; provider = 'ragflow'; endpoint = 'http://127.0.0.1:8080'; enabled = $true } | ConvertTo-Json) | Out-Null
} catch { $invalidEndpointStatus = Get-ErrorStatus $_ }
if ($invalidEndpointStatus -ne 422) { throw "SSRF validation contract failed: status=$invalidEndpointStatus" }

$externalApp = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "External Pending Smoke $suffix"; provider = 'fastgpt'; endpoint = 'https://fastgpt.example.com/api'; credential = "external-credential-$suffix"; enabled = $true
} | ConvertTo-Json -Depth 8)
$externalInvocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($externalApp.id)/invocations" -Headers $headers -ContentType 'application/json' -Body (@{ operation = 'chat'; payload = @{ message = 'external pending' }; idempotency_key = "external-pending-$suffix" } | ConvertTo-Json -Depth 8)
if ($externalInvocation.status -ne 'pending_external' -or $externalInvocation.external_execution -ne 'pending' -or $externalInvocation.execution_mode -ne 'external_pending') { throw 'External provider pending boundary failed.' }

$blockedHttpApp = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "External HTTP Blocked Smoke $suffix"; provider = 'ragflow'; endpoint = 'https://ragflow.example.com/api'; config = @{ execution_mode = 'external_http'; max_retries = 2; backoff_ms = 0 }; enabled = $true
} | ConvertTo-Json -Depth 8)
$blockedHttpInvocation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/external-apps/$($blockedHttpApp.id)/invocations" -Headers $headers -ContentType 'application/json' -Body (@{ operation = 'chat'; payload = @{ message = 'blocked without staging credential' }; idempotency_key = "external-http-blocked-$suffix" } | ConvertTo-Json -Depth 8)
if ($blockedHttpInvocation.status -ne 'pending_external' -or $blockedHttpInvocation.external_execution -ne 'blocked' -or $blockedHttpInvocation.execution_mode -ne 'external_http' -or $blockedHttpInvocation.error_code -ne 'staging_credential_required') { throw 'External HTTP staging credential gate failed.' }

$usage = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/usage" -Headers $headers
$externalUsage = @($usage.items | Where-Object { $_.model -eq 'external-app:dify' })
if ($externalUsage.Count -lt 1) { throw 'Controlled external invocation was not metered.' }

$template = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/marketplace/templates" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "smoke-template-$suffix"; display_name = 'External App Smoke Template'; template_type = 'workflow'; version = '1.0.0'; description = 'controlled marketplace smoke'; manifest = @{ entrypoint = "mock://template/$suffix" }; artifact_ref = "local://template/$suffix/1.0.0"
} | ConvertTo-Json -Depth 10)
$templates = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/marketplace/templates?template_type=workflow" -Headers $headers
$review = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/marketplace/templates/$($template.id)/reviews" -Headers $headers -ContentType 'application/json' -Body (@{ review_status = 'approved'; reason = 'smoke approval' } | ConvertTo-Json)
$published = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/marketplace/templates/$($template.id)/publish" -Headers $headers
$copyBody = @{ idempotency_key = "template-copy-$suffix" } | ConvertTo-Json
$copy = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/marketplace/templates/$($template.id)/copies" -Headers $headers -ContentType 'application/json' -Body $copyBody
$copyReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/marketplace/templates/$($template.id)/copies" -Headers $headers -ContentType 'application/json' -Body $copyBody
if (-not $template.id -or @($templates.items).Count -lt 1 -or $review.review_status -ne 'approved' -or $published.status -ne 'published' -or $copy.id -ne $copyReplay.id -or $copy.execution -ne 'pending_template_materialization') { throw 'Marketplace review/publish/copy contract failed.' }

$evidence = [ordered]@{
    evidence_schema_version = 2
    verification_scope = 'local-compose'
    protocol_profile = 'workama-external-app-rest-v1'
    verification_target = $baseUrl
    verified_boundary = @('external_app.controlled_invocation', 'external_app.http_test_retry', 'marketplace.controlled_copy')
    pending_boundary = @('external_app.provider_execution', 'external_app.staging_credential', 'external_app.third_party_protocol_compatibility')
    staging_gate = 'requires_external_provider_harness'
    public_protocol_verified = $false
    signature_mutual_trust_verified = $false
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    external_app_created = [bool]$app.id
    credential_hash_only = ([bool]$app.credential_configured -and -not $app.credential -and -not $app.credential_hash)
    invocation_controlled_completed = ($invocation.status -eq 'succeeded' -and $invocation.external_execution -eq 'completed')
    invocation_idempotent = ($replay.id -eq $invocation.id -and [bool]$replay.idempotency_replayed)
    invocation_result_persisted = [bool]$invocation.result.output_text
    http_execution_completed = ($httpInvocation.status -eq 'succeeded' -and $httpInvocation.execution_mode -eq 'http_test' -and [bool]$httpInvocation.result.provider_request_sent)
    http_execution_idempotent = ($httpReplay.id -eq $httpInvocation.id -and [bool]$httpReplay.idempotency_replayed)
    http_retry_bounded = ($retryInvocation.status -eq 'failed' -and $retryInvocation.error_code -eq 'provider_http_503' -and $retryInvocation.attempt -eq 2)
    external_provider_pending = ($externalInvocation.status -eq 'pending_external' -and $externalInvocation.external_execution -eq 'pending')
    external_http_without_staging_credential_blocked = ($blockedHttpInvocation.status -eq 'pending_external' -and $blockedHttpInvocation.external_execution -eq 'blocked' -and $blockedHttpInvocation.error_code -eq 'staging_credential_required')
    controlled_invocation_metered = ($externalUsage.Count -ge 1)
    idempotency_mismatch_status = $mismatchStatus
    unsafe_endpoint_status = $invalidEndpointStatus
    marketplace_template_created = [bool]$template.id
    marketplace_review_approved = ($review.review_status -eq 'approved')
    marketplace_published = ($published.status -eq 'published')
    marketplace_copy_idempotent = ($copy.id -eq $copyReplay.id)
    external_execution_pending = ($externalInvocation.external_execution -eq 'pending')
}
$evidenceDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($EvidencePath))
if ($evidenceDirectory -and -not (Test-Path -LiteralPath $evidenceDirectory)) { New-Item -ItemType Directory -Force -Path $evidenceDirectory | Out-Null }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
docker rm -f $mockName | Out-Null
$mockName = $null
