[CmdletBinding()]
param(
    [string]$EvidencePath = 'quality/evidence/a2a-smoke.json'
)

$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }

function Get-HttpStatus([object]$errorRecord) {
    try { return [int]$errorRecord.Exception.Response.StatusCode } catch { return 0 }
}

function Get-Sha256Hex([string]$text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($text))).Replace('-', '').ToLowerInvariant()) }
    finally { $sha.Dispose() }
}

function ConvertFrom-Base64UrlJson([string]$value) {
    $normalized = $value.Replace('-', '+').Replace('_', '/')
    while (($normalized.Length % 4) -ne 0) { $normalized += '=' }
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($normalized)) | ConvertFrom-Json
}

function Invoke-PlatformPython([string]$code, [hashtable]$environment = @{}) {
    $arguments = @('--env-file', '.env', '-f', 'deploy/compose/docker-compose.yml', 'exec', '-T')
    foreach ($key in $environment.Keys) { $arguments += @('-e', "$key=$($environment[$key])") }
    $arguments += @('platform-api', 'python', '-c', $code)
    $output = & docker compose @arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "platform-api python failed: $($output -join [Environment]::NewLine)" }
    $line = @($output | Where-Object { $_ -and $_.ToString().Trim() }) | Select-Object -Last 1
    return $line.ToString().Trim()
}

$keyMaterial = Invoke-PlatformPython @'
import base64, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
key = Ed25519PrivateKey.generate()
enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
print(json.dumps({'private_key': enc(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())), 'public_key': enc(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}, separators=(',', ':')))
'@ | ConvertFrom-Json

$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$claims = ConvertFrom-Base64UrlJson (($login.access_token -split '\.')[1])
$workspaceId = $claims.ws
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$idempotencyKey = "a2a-trust-smoke-$suffix"
$nonce = "a2a-trust-nonce-$suffix"
$signedAtEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$signedAt = [DateTimeOffset]::FromUnixTimeSeconds($signedAtEpoch).ToString('o')

$card = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/agent-cards" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "A2A Trusted Smoke Agent $suffix"; agent_id = "trusted-smoke-$suffix"; endpoint = 'mock://agent/smoke'
    version = '1.0.0'; capabilities = @('task.send'); skills = @('research'); authentication = 'delegated'
    public_key_id = 'default'; public_key_algorithm = 'Ed25519'; public_key = $keyMaterial.public_key
} | ConvertTo-Json -Depth 8)
$publicCard = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/a2a/public/agent-cards/$($card.id)"

$signCode = @'
import base64, json, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
raw = base64.urlsafe_b64decode(os.environ['WAMA_PRIVATE_KEY'] + '===')
payload = {'version': 1, 'workspace_id': os.environ['WAMA_WORKSPACE_ID'], 'card_id': os.environ['WAMA_CARD_ID'], 'key_id': os.environ['WAMA_KEY_ID'], 'message_hash': os.environ['WAMA_MESSAGE_HASH'], 'nonce': os.environ['WAMA_NONCE'], 'signed_at_epoch': int(os.environ['WAMA_SIGNED_AT_EPOCH'])}
encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
print(base64.urlsafe_b64encode(Ed25519PrivateKey.from_private_bytes(raw).sign(encoded)).decode().rstrip('='))
'@

$messageJson = [ordered]@{ operation = 'research'; message = 'deterministic trusted agent task'; artifact_refs = @('mock://artifact/input') } | ConvertTo-Json -Compress -Depth 8
$messageHash = Get-Sha256Hex $messageJson
$signature = Invoke-PlatformPython $signCode @{ WAMA_PRIVATE_KEY = $keyMaterial.private_key; WAMA_WORKSPACE_ID = $workspaceId; WAMA_CARD_ID = $card.id; WAMA_KEY_ID = 'default'; WAMA_MESSAGE_HASH = $messageHash; WAMA_NONCE = $nonce; WAMA_SIGNED_AT_EPOCH = $signedAtEpoch }
$taskBody = @{
    card_id = $card.id; operation = 'research'; message = 'deterministic trusted agent task'; artifact_refs = @('mock://artifact/input')
    idempotency_key = $idempotencyKey; signature_key_id = 'default'; signature = $signature; nonce = $nonce; signed_at = $signedAt
}
$task = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body ($taskBody | ConvertTo-Json -Depth 8)
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body ($taskBody | ConvertTo-Json -Depth 8)

$invalidSignatureStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
        card_id = $card.id; operation = 'research'; message = 'invalid signature'; idempotency_key = "a2a-invalid-$suffix"
        signature_key_id = 'default'; signature = ('0' * 128); nonce = "a2a-invalid-nonce-$suffix"; signed_at = $signedAt
    } | ConvertTo-Json) | Out-Null
} catch { $invalidSignatureStatus = Get-HttpStatus $_ }

$staleStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
        card_id = $card.id; operation = 'research'; message = 'stale signature'; artifact_refs = @('mock://artifact/input'); idempotency_key = "a2a-stale-$suffix"
        signature_key_id = 'default'; signature = ('0' * 128); nonce = "a2a-stale-nonce-$suffix"; signed_at = ([DateTimeOffset]::UtcNow.AddMinutes(-6).ToString('o'))
    } | ConvertTo-Json) | Out-Null
} catch { $staleStatus = Get-HttpStatus $_ }

$nonceReplayStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
        card_id = $card.id; operation = 'research'; message = 'deterministic trusted agent task'; artifact_refs = @('mock://artifact/input'); idempotency_key = "a2a-nonce-replay-$suffix"
        signature_key_id = 'default'; signature = $signature; nonce = $nonce; signed_at = $signedAt
    } | ConvertTo-Json) | Out-Null
} catch { $nonceReplayStatus = Get-HttpStatus $_ }

$externalNoKey = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/agent-cards" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "A2A Untrusted External $suffix"; agent_id = "external-no-key-$suffix"; endpoint = 'https://agent.example.test/a2a'; version = '1.0.0'; authentication = 'delegated'
} | ConvertTo-Json -Depth 8)
$externalNoKeyNonce = "a2a-external-no-key-$suffix"
$externalNoKeyHash = Get-Sha256Hex (([ordered]@{ operation = 'research'; message = 'untrusted external'; artifact_refs = @() } | ConvertTo-Json -Compress -Depth 8))
$externalNoKeySignature = Get-Sha256Hex "$externalNoKeyHash`:$externalNoKeyNonce"
$externalNoKeyStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
        card_id = $externalNoKey.id; operation = 'research'; message = 'untrusted external'; idempotency_key = "a2a-external-no-key-task-$suffix"
        signature = $externalNoKeySignature; nonce = $externalNoKeyNonce; signed_at = $signedAt
    } | ConvertTo-Json) | Out-Null
} catch { $externalNoKeyStatus = Get-HttpStatus $_ }

$externalTrusted = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/agent-cards" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "A2A Trusted External $suffix"; agent_id = "external-trusted-$suffix"; endpoint = 'https://agent.example.test/trusted'; version = '1.0.0'; authentication = 'delegated'
    public_key_id = 'default'; public_key_algorithm = 'Ed25519'; public_key = $keyMaterial.public_key
} | ConvertTo-Json -Depth 8)
$externalNonce = "a2a-external-trusted-$suffix"
$externalMessageJson = [ordered]@{ operation = 'research'; message = 'trusted external pending'; artifact_refs = @() } | ConvertTo-Json -Compress -Depth 8
$externalMessageHash = Get-Sha256Hex $externalMessageJson
$externalSignature = Invoke-PlatformPython $signCode @{ WAMA_PRIVATE_KEY = $keyMaterial.private_key; WAMA_WORKSPACE_ID = $workspaceId; WAMA_CARD_ID = $externalTrusted.id; WAMA_KEY_ID = 'default'; WAMA_MESSAGE_HASH = $externalMessageHash; WAMA_NONCE = $externalNonce; WAMA_SIGNED_AT_EPOCH = $signedAtEpoch }
$externalTask = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    card_id = $externalTrusted.id; operation = 'research'; message = 'trusted external pending'; idempotency_key = "a2a-external-trusted-task-$suffix"
    signature_key_id = 'default'; signature = $externalSignature; nonce = $externalNonce; signed_at = $signedAt
} | ConvertTo-Json)

$registerB = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/register" -ContentType 'application/json' -Body (@{
    email = "a2a-cross-workspace-$suffix@example.com"; password = $values.TEST_ACCOUNT_PASSWORD; display_name = "A2A Cross Workspace $suffix"
} | ConvertTo-Json)
$loginB = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/verify-email" -ContentType 'application/json' -Body (@{ token = $registerB.debug_token } | ConvertTo-Json)
$headersB = @{ Authorization = "Bearer $($loginB.access_token)" }
$crossCard = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/agent-cards" -Headers $headersB -ContentType 'application/json' -Body (@{
    name = "A2A Cross Workspace $suffix"; agent_id = "cross-workspace-$suffix"; endpoint = 'mock://agent/cross-workspace'; version = '1.0.0'; authentication = 'delegated'
    public_key_id = 'default'; public_key_algorithm = 'Ed25519'; public_key = $keyMaterial.public_key
} | ConvertTo-Json -Depth 8)
$crossWorkspaceStatus = 0
try {
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks" -Headers $headersB -ContentType 'application/json' -Body (@{
        card_id = $crossCard.id; operation = 'research'; message = 'deterministic trusted agent task'; artifact_refs = @('mock://artifact/input'); idempotency_key = "a2a-cross-replay-$suffix"
        signature_key_id = 'default'; signature = $signature; nonce = $nonce; signed_at = $signedAt
    } | ConvertTo-Json) | Out-Null
} catch { $crossWorkspaceStatus = Get-HttpStatus $_ }

$loaded = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/a2a/tasks/$($task.id)" -Headers $headers
$updated = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/a2a/tasks/$($task.id)/updates" -Headers $headers -ContentType 'application/json' -Body (@{ status = 'completed'; result_summary = 'external execution remains pending in this baseline'; artifact_refs = @('local://artifact/result') } | ConvertTo-Json)
$cards = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/a2a/agent-cards" -Headers $headers
$trustedCardView = @($cards.items | Where-Object { $_.id -eq $card.id })[0]
$fingerprintOnly = ($trustedCardView.trusted_keys[0].fingerprint.Length -eq 64 -and -not ($trustedCardView.PSObject.Properties.Name -contains 'public_key'))

if (-not $card.id -or $publicCard.trust_status -ne 'trusted' -or $publicCard.trusted_keys[0].fingerprint.Length -ne 64 -or $publicCard.PSObject.Properties.Name -contains 'workspace_id' -or -not $fingerprintOnly -or $task.execution_mode -ne 'pending_external' -or $task.trust_status -ne 'verified_public_key' -or -not $task.signature_verified -or $invalidSignatureStatus -ne 401 -or $staleStatus -ne 401 -or $nonceReplayStatus -ne 409 -or $externalNoKeyStatus -ne 401 -or $externalTask.execution_mode -ne 'pending_external' -or $externalTask.trust_status -ne 'verified_public_key' -or $crossWorkspaceStatus -ne 401 -or -not $replay.idempotency_replayed -or $replay.id -ne $task.id -or $loaded.id -ne $task.id -or $updated.status -ne 'completed') { throw 'A2A public-key trust contract failed.' }
$evidence = [ordered]@{
    evidence_schema_version = 2; verification_scope = 'local-compose'; protocol_profile = 'workama-a2a-rest-v1'; verification_target = "$baseUrl/api/v1/a2a"
    verified_boundary = @('a2a.public_card_fingerprint_only', 'a2a.same_workspace_ed25519_verification', 'a2a.fail_closed_replay_and_scope')
    pending_boundary = @('a2a.third_party_card_compatibility', 'a2a.external_mutual_trust', 'a2a.external_execution')
    staging_gate = 'requires_external_protocol_harness'; public_protocol_verified = $false; signature_mutual_trust_verified = $false
    timestamp = [DateTimeOffset]::UtcNow.ToString('o'); agent_card_created = [bool]$card.id; public_key_algorithm = $trustedCardView.trusted_keys[0].algorithm
    fingerprint_only = $fingerprintOnly; public_card_trusted = ($publicCard.trust_status -eq 'trusted' -and $publicCard.trusted_keys[0].fingerprint.Length -eq 64); task_created = [bool]$task.id; public_key_signature_verified = ($task.trust_status -eq 'verified_public_key' -and [bool]$task.signature_verified)
    task_pending_external = ($task.execution_mode -eq 'pending_external'); invalid_signature_status = $invalidSignatureStatus; stale_signature_status = $staleStatus
    nonce_replay_status = $nonceReplayStatus; external_without_trusted_key_status = $externalNoKeyStatus; trusted_external_pending = ($externalTask.execution_mode -eq 'pending_external' -and $externalTask.trust_status -eq 'verified_public_key')
    cross_workspace_replay_status = $crossWorkspaceStatus; task_idempotent = ($replay.id -eq $task.id -and [bool]$replay.idempotency_replayed); local_update_only = ($updated.execution_mode -eq 'local_update_only')
    third_party_key_discovery_verified = $false; external_execution_status = 'pending_external'
}
$evidenceDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($EvidencePath))
if ($evidenceDirectory -and -not (Test-Path -LiteralPath $evidenceDirectory)) { New-Item -ItemType Directory -Force -Path $evidenceDirectory | Out-Null }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
