param(
    [string]$BaseUrl = $(if ($env:WORKAMA_LIVE_BASE_URL) { $env:WORKAMA_LIVE_BASE_URL } else { "http://localhost:20200" }),
    [string]$EvidencePath = "quality/evidence/channel-extensions-smoke.json"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [hashtable]$Headers = @{},
        [hashtable]$Body = $null
    )
    $request = @{ Method = $Method; Uri = "$BaseUrl$Path"; Headers = $Headers; ErrorAction = "Stop" }
    if ($null -ne $Body) { $request.ContentType = "application/json"; $request.Body = $Body | ConvertTo-Json -Depth 12 }
    Invoke-RestMethod @request
}

function Assert-True { param([bool]$Condition, [string]$Message); if (-not $Condition) { throw $Message } }

$suffix = [Guid]::NewGuid().ToString("N")
$registered = Invoke-Json -Method POST -Path "/api/v1/auth/register" -Body @{ email = "extensions-$suffix@example.com"; password = "WorkAMA-Live-2026!"; display_name = "Extensions Smoke" }
$auth = Invoke-Json -Method POST -Path "/api/v1/auth/verify-email" -Body @{ token = $registered.debug_token }
$headers = @{ Authorization = "Bearer $($auth.access_token)" }

$pool = Invoke-Json -Method POST -Path "/api/v1/gateway/account-pools" -Headers $headers -Body @{ name = "Controlled pool $suffix"; provider = "custom"; sticky_ttl_seconds = 600; billing_policy = @{ mode = "per_request" } }
$account = Invoke-Json -Method POST -Path "/api/v1/gateway/account-pools/$($pool.id)/accounts" -Headers $headers -Body @{ display_name = "Controlled account"; account_ref = "mock-account-$suffix"; region = "local"; weight = 100; quota_remaining = 100 }
Assert-True (-not $account.account_ref_enc -and -not $account.account_ref_hash) "Account credential material leaked"
$lease = Invoke-Json -Method POST -Path "/api/v1/gateway/account-pools/$($pool.id)/leases" -Headers $headers -Body @{ session_key = "sticky-session-$suffix"; model = "workama-chat" }
$leaseReplay = Invoke-Json -Method POST -Path "/api/v1/gateway/account-pools/$($pool.id)/leases" -Headers $headers -Body @{ session_key = "sticky-session-$suffix"; model = "workama-chat" }
Assert-True ($lease.account_id -eq $leaseReplay.account_id) "Sticky lease did not select the same account"
Assert-True ($leaseReplay.status -eq "replayed") "Sticky lease replay was not detected"
Invoke-Json -Method POST -Path "/api/v1/gateway/account-pools/$($pool.id)/leases/sticky-session-$suffix/release" -Headers $headers | Out-Null

$im = Invoke-Json -Method POST -Path "/api/v1/im/channels" -Headers $headers -Body @{ kind = "feishu"; name = "Controlled Feishu $suffix"; endpoint = "mock://im/feishu"; signing_secret = "mock-signing-$suffix"; agent_id = "agent-demo"; config = @{ mode = "controlled" } }
$event = Invoke-Json -Method POST -Path "/api/v1/im/channels/$($im.id)/events" -Headers $headers -Body @{ external_message_id = "inbound-$suffix"; sender_ref = "feishu-user-$suffix"; content = "Please summarize this"; metadata = @{ channel = "controlled" } }
$eventReplay = Invoke-Json -Method POST -Path "/api/v1/im/channels/$($im.id)/events" -Headers $headers -Body @{ external_message_id = "inbound-$suffix"; sender_ref = "feishu-user-$suffix"; content = "Please summarize this"; metadata = @{} }
Assert-True ($event.execution_mode -eq "controlled_mock") "Controlled IM event did not use the controlled mode"
Assert-True ($eventReplay.replayed -eq $true) "IM inbound idempotency was not detected"
$outbound = Invoke-Json -Method POST -Path "/api/v1/im/channels/$($im.id)/messages" -Headers $headers -Body @{ external_message_id = "outbound-$suffix"; content = "Controlled response"; metadata = @{ token = "must-not-persist" } }
Assert-True ($outbound.status -eq "delivered") "Controlled IM outbound was not delivered"
$external = Invoke-Json -Method POST -Path "/api/v1/im/channels" -Headers $headers -Body @{ kind = "telegram"; name = "External Telegram $suffix"; endpoint = "https://telegram.example.test/bot"; config = @{} }
Assert-True ($external.status -eq "pending_external") "External IM endpoint was not isolated"

$manifest = Invoke-Json -Method GET -Path "/api/v1/public/miniapp/manifest"
Assert-True ($manifest.credential_storage -eq "memory_only") "Miniapp manifest does not declare memory-only credentials"
$bootstrap = Invoke-Json -Method GET -Path "/api/v1/miniapp/bootstrap" -Headers $headers
$miniSession = Invoke-Json -Method POST -Path "/api/v1/miniapp/sessions" -Headers $headers
$miniMessage = Invoke-Json -Method POST -Path "/api/v1/miniapp/sessions/$($miniSession.id)/messages" -Headers $headers -Body @{ content = "Ask from the miniapp" }
$miniMessages = Invoke-Json -Method GET -Path "/api/v1/miniapp/sessions/$($miniSession.id)/messages" -Headers $headers
$subscriptions = Invoke-Json -Method POST -Path "/api/v1/miniapp/subscriptions" -Headers $headers -Body @{ topics = @("agent.completed", "billing.low_balance") }
Assert-True ($miniMessage.execution_mode -eq "controlled_mock") "Miniapp message did not use controlled execution"
Assert-True ($miniMessages.items.Count -eq 2) "Miniapp message history is incomplete"
Assert-True ($subscriptions.status -eq "pending_external") "Miniapp subscription boundary was hidden"

$evidence = [ordered]@{
    ok = $true
    account_pool_created = $true
    account_credential_not_exposed = $true
    sticky_lease_replayed = ($leaseReplay.status -eq "replayed")
    controlled_im_inbound = ($event.execution_mode -eq "controlled_mock")
    controlled_im_idempotency = ($eventReplay.replayed -eq $true)
    controlled_im_outbound_delivered = ($outbound.status -eq "delivered")
    external_im_pending = ($external.status -eq "pending_external")
    miniapp_manifest_memory_only = ($manifest.credential_storage -eq "memory_only")
    miniapp_session_messages = ($miniMessages.items.Count -eq 2)
    miniapp_subscription_pending_external = ($subscriptions.status -eq "pending_external")
    provider_exchange_pending_external = $true
}
$parent = Split-Path -Parent $EvidencePath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 -Path $EvidencePath
$evidence | ConvertTo-Json -Depth 12
