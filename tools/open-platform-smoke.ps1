[CmdletBinding()]
param(
    [string]$EvidencePath = 'quality/evidence/open-platform-smoke.json',
    # Local out-of-process webhook receiver harness. 'auto' runs it when Docker and the
    # platform worker container are available and silently degrades otherwise.
    [ValidateSet('auto', 'on', 'off')][string]$ReceiverHarness = 'auto'
)

$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

function Get-ErrorStatus($errorRecord) { if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }; return 0 }

function Invoke-Docker {
    # Arguments are passed as one array so short docker flags such as -d/-e/-v are never
    # bound to PowerShell common parameters.
    param([string[]]$DockerArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @DockerArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (($output -join "`n").Trim()) }
    } finally { $ErrorActionPreference = $previous }
}

function Get-HmacHex {
    param([byte[]]$Key, [byte[]]$Message)
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($Key)
    try { return (($hmac.ComputeHash($Message) | ForEach-Object { $_.ToString('x2') }) -join '') } finally { $hmac.Dispose() }
}

$client = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/oauth/clients" -Headers $headers -ContentType 'application/json' -Body (@{
    name = "Smoke OAuth $suffix"; redirect_uris = @('https://app.example.com/workama/callback'); scopes = @('openid','profile')
} | ConvertTo-Json)
if (-not $client.client_id -or -not $client.client_secret) { throw 'OAuth client secret was not shown on creation.' }
$clientView = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/oauth/clients/$($client.client_id)" -Headers $headers
if ($clientView.client_secret -or $clientView.client_secret_hash) { throw 'OAuth client secret/hash leaked in view.' }

$verifier = ('v' * 64)
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$sha = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($verifier))
$sha256.Dispose()
$challenge = [Convert]::ToBase64String($sha).TrimEnd('=').Replace('+','-').Replace('/','_')
$authorizeUri = "$baseUrl/api/v1/oauth/authorize?client_id=$([uri]::EscapeDataString($client.client_id))&redirect_uri=$([uri]::EscapeDataString('https://app.example.com/workama/callback'))&response_type=code&code_challenge=$challenge&code_challenge_method=S256&scope=openid%20profile&state=smoke-state"
$authorization = Invoke-RestMethod -Method Get -Uri $authorizeUri -Headers $headers
$tokens = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/oauth/token" -ContentType 'application/json' -Body (@{
    grant_type = 'authorization_code'; client_id = $client.client_id; client_secret = $client.client_secret; code = $authorization.code
    redirect_uri = 'https://app.example.com/workama/callback'; code_verifier = $verifier
} | ConvertTo-Json)
$replayStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/oauth/token" -ContentType 'application/json' -Body (@{ grant_type = 'authorization_code'; client_id = $client.client_id; client_secret = $client.client_secret; code = $authorization.code; redirect_uri = 'https://app.example.com/workama/callback'; code_verifier = $verifier } | ConvertTo-Json) | Out-Null } catch { $replayStatus = Get-ErrorStatus $_ }
if (-not $tokens.access_token -or $replayStatus -ne 400) { throw "OAuth exchange/replay contract failed: replay=$replayStatus" }

$controlledWebhook = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'mock://webhook/controlled'; events = @('artifact.created'); description = 'controlled smoke' } | ConvertTo-Json)
$controlledView = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/webhooks/$($controlledWebhook.id)" -Headers $headers
if (-not $controlledWebhook.secret -or $controlledView.secret -or $controlledView.secret_hash) { throw 'Webhook secret/hash leaked or was not shown once.' }
$controlledBody = @{ event_type = 'artifact.created'; idempotency_key = "open-platform-controlled-$suffix"; payload = @{ resource_id = 'art_1' } } | ConvertTo-Json -Depth 8
$controlledDelivery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($controlledWebhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body $controlledBody
$controlledWorkerQueued = ($controlledDelivery.status -eq 'pending' -and $controlledDelivery.delivery_mode -eq 'controlled_mock' -and $controlledDelivery.external_execution -eq 'queued')
if (-not $controlledWorkerQueued) { throw 'Controlled webhook request was not queued for the worker.' }
for ($attempt = 0; $attempt -lt 30 -and $controlledDelivery.status -ne 'delivered'; $attempt++) {
    Start-Sleep -Milliseconds 500
    $controlledDelivery = (Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/webhooks/$($controlledWebhook.id)/deliveries" -Headers $headers).items | Where-Object { $_.id -eq $controlledDelivery.id } | Select-Object -First 1
}
$controlledReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($controlledWebhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body $controlledBody
$controlledConflictStatus = 0
try { Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($controlledWebhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'artifact.created'; idempotency_key = "open-platform-controlled-$suffix"; payload = @{ resource_id = 'different' } } | ConvertTo-Json -Depth 8) | Out-Null } catch { $controlledConflictStatus = Get-ErrorStatus $_ }
if ($controlledDelivery.status -ne 'delivered' -or $controlledDelivery.delivery_mode -ne 'controlled_mock' -or -not $controlledDelivery.signature -or $controlledReplay.id -ne $controlledDelivery.id -or $controlledReplay.idempotency_replayed -ne $true -or $controlledConflictStatus -ne 409) { throw 'Controlled webhook worker delivery/signature/idempotency contract failed.' }

$webhook = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks" -Headers $headers -ContentType 'application/json' -Body (@{ url = 'https://hooks.example.com/workama'; events = @('artifact.created'); description = 'public smoke' } | ConvertTo-Json)
$webhookView = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/webhooks/$($webhook.id)" -Headers $headers
if (-not $webhook.secret -or $webhookView.secret -or $webhookView.secret_hash) { throw 'Webhook secret/hash leaked or was not shown once.' }
$delivery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($webhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'artifact.created'; idempotency_key = "open-platform-public-$suffix"; payload = @{ resource_id = 'art_1' } } | ConvertTo-Json -Depth 8)
$deliveryReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($webhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'artifact.created'; idempotency_key = "open-platform-public-$suffix"; payload = @{ resource_id = 'art_1' } } | ConvertTo-Json -Depth 8)
if ($delivery.status -ne 'pending' -or $delivery.delivery_mode -ne 'external' -or $deliveryReplay.id -ne $delivery.id) { throw 'Public webhook delivery must be queued and idempotent.' }

# --- Local out-of-process receiver harness -----------------------------------
# The platform SSRF guard rejects loopback, private, and *.internal targets, so the
# receiver runs on a dedicated Docker network carved out of RFC 6598 shared address
# space (100.64.0.0/10). That range passes the guard yet never leaves this host, which
# lets the real worker open a real socket to an independent receiver.
$harnessNetwork = 'workama-hook-harness'
$harnessSubnet = '100.64.99.0/24'
$harnessContainer = 'workama-hook-receiver'
$harnessPort = 20255
$receiverBase = "http://localhost:$harnessPort"
$workerContainer = if ($env:WORKAMA_WORKER_CONTAINER) { $env:WORKAMA_WORKER_CONTAINER } else { 'workama-platform-worker-1' }
$receiverScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'webhook_harness_receiver.py')).Path.Replace('\', '/')

$harness = [ordered]@{
    mode = 'skipped'; reason = 'not_attempted'; delivery_status = $null; response_code = $null
    capture_count = 0; receiver_verified = $false; smoke_verified = $false
    verified_with_issued_secret = $false; tampered_body_rejected = $false
    headers_contract_ok = $false; signature_key_source = $null
}
$harnessAvailable = $false
if ($ReceiverHarness -ne 'off') {
    try {
        $probe = Invoke-Docker @('container', 'inspect', '--format', '{{.State.Running}}', $workerContainer)
        if ($probe.ExitCode -eq 0 -and $probe.Output -eq 'true') { $harnessAvailable = $true } else { $harness.reason = 'worker_container_not_running' }
    } catch { $harness.reason = 'docker_cli_unavailable' }
} else { $harness.reason = 'disabled_by_parameter' }
if ($ReceiverHarness -eq 'on' -and -not $harnessAvailable) { throw "Receiver harness was requested but is unavailable: $($harness.reason)" }

if ($harnessAvailable) {
    try {
        $null = Invoke-Docker @('rm', '-f', $harnessContainer)
        if ((Invoke-Docker @('network', 'inspect', $harnessNetwork)).ExitCode -ne 0) {
            $created = Invoke-Docker @('network', 'create', '--subnet', $harnessSubnet, $harnessNetwork)
            if ($created.ExitCode -ne 0) { throw "Failed to create the harness network: $($created.Output)" }
        }
        $run = Invoke-Docker @('run', '-d', '--name', $harnessContainer, '--network', $harnessNetwork, '-p', "${harnessPort}:${harnessPort}", '-e', "HARNESS_PORT=$harnessPort", '-v', "${receiverScript}:/app/receiver.py:ro", 'python:3.12-slim', 'python', '/app/receiver.py')
        if ($run.ExitCode -ne 0) { throw "Failed to start the receiver container: $($run.Output)" }
        $connect = Invoke-Docker @('network', 'connect', $harnessNetwork, $workerContainer)
        if ($connect.ExitCode -ne 0 -and $connect.Output -notmatch 'already exists') { throw "Failed to attach $workerContainer to the harness network: $($connect.Output)" }

        $receiverHealthy = $false
        for ($attempt = 0; $attempt -lt 60 -and -not $receiverHealthy; $attempt++) {
            Start-Sleep -Milliseconds 500
            try { $receiverHealthy = (Invoke-RestMethod -Method Get -Uri "$receiverBase/healthz" -TimeoutSec 5).ok -eq $true } catch { $receiverHealthy = $false }
        }
        if (-not $receiverHealthy) { throw 'The local webhook receiver did not become healthy.' }

        # The worker signs with hash_secret(secret) = HMAC(KEY_PEPPER, secret), so the harness
        # needs the server pepper. That dependency is exactly why receiver-side mutual trust
        # stays a pending boundary: an integrator only ever receives the whsec_ value.
        $pepperResult = Invoke-Docker @('exec', $workerContainer, 'printenv', 'KEY_PEPPER')
        if ($pepperResult.ExitCode -ne 0 -or -not $pepperResult.Output) { throw 'Could not read the worker KEY_PEPPER that derives the webhook signing key.' }
        $pepper = $pepperResult.Output

        $receiverWebhook = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks" -Headers $headers -ContentType 'application/json' -Body (@{ url = "http://${harnessContainer}:${harnessPort}/hook"; events = @('artifact.created'); description = 'local receiver harness' } | ConvertTo-Json)
        if (-not $receiverWebhook.secret) { throw 'Harness webhook secret was not shown once on creation.' }
        Invoke-RestMethod -Method Post -Uri "$receiverBase/expect" -ContentType 'application/json' -Body (@{ secret = $receiverWebhook.secret; pepper = $pepper } | ConvertTo-Json) | Out-Null

        $harnessKey = "open-platform-receiver-$suffix"
        $harnessDelivery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/webhooks/$($receiverWebhook.id)/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'artifact.created'; idempotency_key = $harnessKey; payload = @{ resource_id = 'art_1' } } | ConvertTo-Json -Depth 8)
        if ($harnessDelivery.delivery_mode -ne 'external') { throw "Harness delivery must use the external transport, got $($harnessDelivery.delivery_mode)." }
        for ($attempt = 0; $attempt -lt 60 -and @('delivered', 'failed') -notcontains $harnessDelivery.status; $attempt++) {
            Start-Sleep -Milliseconds 500
            $harnessDelivery = (Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/webhooks/$($receiverWebhook.id)/deliveries" -Headers $headers).items | Where-Object { $_.id -eq $harnessDelivery.id } | Select-Object -First 1
        }
        $captures = $null
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            $captures = Invoke-RestMethod -Method Get -Uri "$receiverBase/captures"
            if ($captures.count -ge 1) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $captures -or $captures.count -lt 1) { throw 'The local receiver never captured a webhook delivery.' }
        $capture = $captures.items[0]

        if ($capture.signature -notmatch '^t=(\d+),v1=([0-9a-f]{64})$') { throw "Unexpected x-workama-signature shape: $($capture.signature)" }
        $signedAt = $matches[1]
        $signedDigest = $matches[2]
        $utf8 = [System.Text.Encoding]::UTF8
        $rawBody = [Convert]::FromBase64String($capture.body_base64)
        $signedMessage = [byte[]]($utf8.GetBytes("$signedAt.") + $rawBody)
        $signingKey = Get-HmacHex -Key $utf8.GetBytes($pepper) -Message $utf8.GetBytes($receiverWebhook.secret)
        $smokeVerified = (Get-HmacHex -Key $utf8.GetBytes($signingKey) -Message $signedMessage) -eq $signedDigest
        $issuedSecretVerified = (Get-HmacHex -Key $utf8.GetBytes($receiverWebhook.secret) -Message $signedMessage) -eq $signedDigest
        $headersContractOk = ($capture.event -eq 'artifact.created' -and $capture.idempotency_key -eq $harnessKey -and $capture.content_type -eq 'application/json' -and $capture.user_agent -eq 'WorkAMA-Webhook/1')

        if ($harnessDelivery.status -ne 'delivered' -or [int]$harnessDelivery.response_code -ne 200) { throw "Local receiver delivery failed: status=$($harnessDelivery.status) code=$($harnessDelivery.response_code) error=$($harnessDelivery.error_code)" }
        if (-not $smokeVerified) { throw 'The smoke could not reproduce x-workama-signature over the captured wire bytes.' }
        if ($capture.verification.verified_with_peppered_secret_hash -ne $true) { throw 'The receiver could not verify x-workama-signature.' }
        if ($capture.verification.tampered_body_rejected -ne $true) { throw 'The signature is not bound to the delivered body.' }
        if ($issuedSecretVerified -or $capture.verification.verified_with_secret -ne $false) { throw 'The issued webhook secret now verifies the signature; re-derive the mutual-trust boundary evidence.' }
        if (-not $headersContractOk) { throw 'The delivered webhook header contract did not match at the receiver.' }

        $harness.mode = 'local_docker_receiver'
        $harness.reason = 'verified'
        $harness.delivery_status = $harnessDelivery.status
        $harness.response_code = [int]$harnessDelivery.response_code
        $harness.capture_count = [int]$captures.count
        $harness.receiver_verified = $true
        $harness.smoke_verified = $true
        $harness.verified_with_issued_secret = $false
        $harness.tampered_body_rejected = $true
        $harness.headers_contract_ok = $true
        $harness.signature_key_source = 'server_key_pepper_derived_secret_hash'
    } finally {
        $null = Invoke-Docker @('rm', '-f', $harnessContainer)
        $null = Invoke-Docker @('network', 'disconnect', '-f', $harnessNetwork, $workerContainer)
        $null = Invoke-Docker @('network', 'rm', $harnessNetwork)
    }
}

$verifiedBoundary = @('oauth.authorization_code_pkce_internal', 'webhook.controlled_worker_delivery')
if ($harness.mode -eq 'local_docker_receiver') {
    # Real socket delivery and a byte-exact signature reproduction are now proven locally.
    # public_https_delivery stays pending because the receiver is plain HTTP on private
    # infrastructure, and signature_mutual_trust stays pending because verification still
    # requires the server pepper rather than the issued whsec_ secret.
    $verifiedBoundary += @('webhook.external_http_socket_delivery', 'webhook.signature_algorithm_reproducible')
}

$evidence = [ordered]@{
    evidence_schema_version = 2; verification_scope = 'local-compose'; protocol_profile = 'workama-open-platform-rest-v1'; verification_target = $baseUrl
    verified_boundary = $verifiedBoundary
    pending_boundary = @('oauth.provider_exchange', 'oauth.public_interoperability', 'webhook.public_https_delivery', 'webhook.signature_mutual_trust')
    staging_gate = 'requires_external_protocol_harness'; public_protocol_verified = $false; signature_mutual_trust_verified = $false
    timestamp = [DateTimeOffset]::UtcNow.ToString('o'); oauth_client_created = [bool]$client.client_id
    client_secret_not_returned = (-not $clientView.client_secret -and -not $clientView.client_secret_hash)
    pkce_exchange_succeeded = [bool]$tokens.access_token; code_replay_status = $replayStatus; oauth_provider_execution = $authorization.provider_execution
    webhook_created = [bool]$webhook.id; webhook_secret_not_returned = (-not $webhookView.secret -and -not $webhookView.secret_hash)
    controlled_worker_queued = $controlledWorkerQueued; controlled_delivery_delivered = ($controlledDelivery.status -eq 'delivered' -and $controlledDelivery.delivery_mode -eq 'controlled_mock')
    controlled_signature_present = [bool]$controlledDelivery.signature; controlled_idempotency_conflict_status = $controlledConflictStatus
    controlled_delivery_replayed = ($controlledReplay.id -eq $controlledDelivery.id -and $controlledReplay.idempotency_replayed -eq $true)
    delivery_idempotent = ($deliveryReplay.id -eq $delivery.id); external_delivery_queued = ($delivery.status -eq 'pending' -and $delivery.delivery_mode -eq 'external')
    public_webhook_execution = $delivery.external_execution; public_webhook_delivery = 'queued_only'
    receiver_harness_mode = $harness.mode; receiver_harness_reason = $harness.reason
    receiver_delivery_status = $harness.delivery_status; receiver_response_code = $harness.response_code
    receiver_capture_count = $harness.capture_count
    receiver_signature_verified = $harness.receiver_verified
    receiver_signature_reproduced_by_smoke = $harness.smoke_verified
    receiver_signature_key_source = $harness.signature_key_source
    receiver_signature_verified_with_issued_secret = $harness.verified_with_issued_secret
    receiver_tampered_body_rejected = $harness.tampered_body_rejected
    receiver_header_contract_ok = $harness.headers_contract_ok
}
$evidenceDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($EvidencePath))
if ($evidenceDirectory -and -not (Test-Path -LiteralPath $evidenceDirectory)) { New-Item -ItemType Directory -Force -Path $evidenceDirectory | Out-Null }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
