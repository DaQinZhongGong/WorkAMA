$ErrorActionPreference = 'Stop'

$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$secret = if ($values.BILLING_MOCK_WEBHOOK_SECRET) { $values.BILLING_MOCK_WEBHOOK_SECRET } else { 'workama-mock-provider-secret' }
function Get-Hmac([string]$payload) {
    $hmac = [System.Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($secret))
    try { return (($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($payload)) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $hmac.Dispose() }
}
$current = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/subscription" -Headers $headers
$catalog = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/price-catalog" -Headers $headers
$methodsBefore = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/payment-methods" -Headers $headers
$target = if ($current.plan_code -eq 'pro') { 'team' } elseif ($current.plan_code -eq 'team') { 'free' } else { 'pro' }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$checkoutBody = @{ plan_code = $target; provider = 'mock'; idempotency_key = "subscription-smoke-$suffix" } | ConvertTo-Json
$checkout = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/subscription/checkout" -Headers $headers -ContentType 'application/json' -Body $checkoutBody
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/subscription/checkout" -Headers $headers -ContentType 'application/json' -Body $checkoutBody
if (-not $replay.replayed -or $replay.payment.id -ne $checkout.payment.id) { throw 'Subscription checkout idempotency replay failed.' }
$eventBody = @{ event_id = "subscription-event-$suffix"; payment_id = $checkout.payment.id; status = 'succeeded'; amount = [string]$checkout.payment.amount; currency = [string]$checkout.payment.currency } | ConvertTo-Json -Compress
$callbackHeaders = @{ 'X-Provider-Signature' = (Get-Hmac $eventBody) }
$callback = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/providers/mock/callbacks" -Headers $callbackHeaders -ContentType 'application/json' -Body $eventBody
$callbackReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/providers/mock/callbacks" -Headers $callbackHeaders -ContentType 'application/json' -Body $eventBody
if (-not $callback.accepted -or $callback.replayed -or -not $callbackReplay.replayed) { throw 'Signed provider callback or replay contract failed.' }
$confirmed = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/orders/$($checkout.order.id)" -Headers $headers
$after = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/subscription" -Headers $headers
$grants = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/grants" -Headers $headers
$targetGrant = @($grants.items | Where-Object { $_.source -eq 'subscription' -and $_.status -eq 'active' -and $_.remaining_amount -gt 0 }) | Select-Object -First 1
if ($confirmed.payment.payment_status -ne 'succeeded' -or $confirmed.order.status -ne 'succeeded' -or $after.plan_code -ne $target -or -not $targetGrant) { throw 'Subscription payment confirmation or monthly credit grant evidence is incomplete.' }
$beforeGrantExpiry = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/account" -Headers $headers
$grantId = [string]$targetGrant.id
$workspaceId = [string]$current.workspace_id
if ($grantId -notmatch '^[A-Za-z0-9_-]+$' -or $workspaceId -notmatch '^[A-Za-z0-9_-]+$') { throw 'Credit grant identifiers failed the local smoke safety check.' }
$dbUser = if ($values.POSTGRES_USER) { $values.POSTGRES_USER } else { 'workama' }
$dbName = if ($values.POSTGRES_DB) { $values.POSTGRES_DB } else { 'workama' }
$expireSql = "UPDATE bill_credit_grant SET expires_at=now()-interval '1 second' WHERE id='$grantId' AND workspace_id='$workspaceId'"
docker compose --env-file .env -f deploy/compose/docker-compose.yml exec -T postgres psql -v ON_ERROR_STOP=1 -U $dbUser -d $dbName -c $expireSql | Out-Null
$expiredAccount = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/account" -Headers $headers
$expiredGrants = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/grants" -Headers $headers
$expiredGrant = @($expiredGrants.items | Where-Object { $_.id -eq $grantId }) | Select-Object -First 1
if (-not $expiredGrant -or $expiredGrant.status -ne 'expired' -or [decimal]$expiredGrant.remaining_amount -ne 0 -or [decimal]$expiredAccount.total_balance -ge [decimal]$beforeGrantExpiry.total_balance) { throw 'Expired monthly credit grant was not reclaimed from the account.' }
$cancelled = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/subscription/cancel" -Headers $headers
$resumed = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/subscription/resume" -Headers $headers
$invoices = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/invoices" -Headers $headers
if (-not $cancelled.cancel_at_period_end -or $resumed.cancel_at_period_end -or @($invoices.items).Count -lt 1) { throw 'Subscription cancellation/resume or invoice evidence is incomplete.' }
$invoiceRequest = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/invoice-requests" -Headers $headers -ContentType 'application/json' -Body (@{ order_id = $checkout.order.id; tax_profile = @{ region = 'CN'; tax_mode = 'exclusive' }; idempotency_key = "invoice-request-$suffix" } | ConvertTo-Json)
$invoiceDownload = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/invoices/$($invoices.items[0].id)/downloads" -Headers $headers
$method = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/billing/payment-method-setups" -Headers $headers -ContentType 'application/json' -Body (@{ provider = 'mock'; method_type = 'card'; token = "mock-token-$suffix"; display_label = 'Mock wallet' } | ConvertTo-Json)
$methodsAfter = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/billing/payment-methods" -Headers $headers
$reconciliationBody = @{ business_date = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd'); workspace_id = $current.workspace_id } | ConvertTo-Json
$reconciliation = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/admin/billing/reconciliations" -Headers $headers -ContentType 'application/json' -Body $reconciliationBody
$reconciliationList = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/admin/billing/reconciliations" -Headers $headers
if ($invoiceRequest.provider_execution -ne 'pending_external' -or $invoiceDownload.status -ne 'pending_external' -or $method.secret_storage -ne 'hash_only' -or @($methodsAfter.items).Count -le @($methodsBefore.items).Count -or $reconciliation.status -ne 'completed' -or @($reconciliationList.items).Count -lt 1) { throw 'Commercial billing boundary evidence is incomplete.' }

$evidence = @{
    timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    initial_plan = $current.plan_code
    target_plan = $target
    payment_status = $confirmed.payment.payment_status
    idempotency_replayed = [bool]$replay.replayed
    provider_callback_verified = [bool]$callback.accepted
    provider_callback_replayed = [bool]$callbackReplay.replayed
    order_snapshot_immutable = ($confirmed.order.price_snapshot -ne $null -and $confirmed.order.version -ge 1)
    cancel_at_period_end = [bool]$cancelled.cancel_at_period_end
    resumed = -not [bool]$resumed.cancel_at_period_end
    invoice_count = @($invoices.items).Count
    invoice_request_pending_external = ($invoiceRequest.provider_execution -eq 'pending_external')
    invoice_download_pending_external = ($invoiceDownload.status -eq 'pending_external')
    payment_method_hash_only = ($method.secret_storage -eq 'hash_only')
    admin_reconciliation_completed = ($reconciliation.status -eq 'completed' -and @($reconciliationList.items).Count -ge 1)
    monthly_grant_created = [bool]$targetGrant
    monthly_grant_expiry_present = [bool]$targetGrant.expires_at
    monthly_grant_expired_and_reclaimed = ($expiredGrant.status -eq 'expired' -and [decimal]$expiredGrant.remaining_amount -eq 0 -and [decimal]$expiredAccount.total_balance -lt [decimal]$beforeGrantExpiry.total_balance)
}
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/subscription-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
