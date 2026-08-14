$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content -LiteralPath '.env' -Encoding utf8 | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() } }
if (-not $values.TEST_ACCOUNT_EMAIL -or -not $values.TEST_ACCOUNT_PASSWORD) { throw 'TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required.' }
$baseUrl = if ($env:WORKAMA_BASE_URL) { $env:WORKAMA_BASE_URL.TrimEnd('/') } else { 'http://localhost:20200' }
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/auth/login" -ContentType 'application/json' -Body (@{ email = $values.TEST_ACCOUNT_EMAIL; password = $values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$exportKey = "audit-export-smoke-$suffix"
$deliveryKey = "siem-delivery-smoke-$suffix"
function Get-ErrorStatus($errorRecord) { if ($errorRecord.Exception.Response -and $errorRecord.Exception.Response.StatusCode) { return [int]$errorRecord.Exception.Response.StatusCode }; return 0 }
$serviceAccount = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/service-accounts" -Headers $headers -ContentType 'application/json' -Body (@{ name = "Audit Chain Smoke $suffix"; purpose = 'realtime audit chain verification'; scopes = @('platform:read'); idempotency_key = "audit-chain-account-$suffix" } | ConvertTo-Json -Depth 8)
$events = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/v1/enterprise/audit/events" -Headers $headers
$realtimeAudit = @($events.items | Where-Object { $_.resource_id -eq $serviceAccount.id -and $_.event_type -eq 'service_account.created' }) | Select-Object -First 1
$exportBody = @{ limit = 100; format = 'jsonl' } | ConvertTo-Json
$export = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/audit/exports?format=jsonl&idempotency_key=$exportKey" -Headers $headers -ContentType 'application/json' -Body $exportBody
$replay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/audit/exports?format=jsonl&idempotency_key=$exportKey" -Headers $headers -ContentType 'application/json' -Body $exportBody
$siem = Invoke-RestMethod -Method Put -Uri "$baseUrl/api/v1/enterprise/siem" -Headers $headers -ContentType 'application/json' -Body (@{ name = 'Smoke SIEM'; endpoint = 'mock://siem/ingest'; enabled = $true; events = @('*'); credential = 'smoke-secret' } | ConvertTo-Json)
$delivery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/siem/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'audit.test'; idempotency_key = $deliveryKey } | ConvertTo-Json)
$deliveryReplay = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/siem/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'audit.test'; idempotency_key = $deliveryKey } | ConvertTo-Json)
$publicSiem = Invoke-RestMethod -Method Put -Uri "$baseUrl/api/v1/enterprise/siem" -Headers $headers -ContentType 'application/json' -Body (@{ name = "Smoke Public SIEM $suffix"; endpoint = 'https://siem.example.com/ingest'; enabled = $true; events = @('*'); credential = 'smoke-secret' } | ConvertTo-Json)
$publicDelivery = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/enterprise/siem/tests" -Headers $headers -ContentType 'application/json' -Body (@{ event_type = 'audit.test'; idempotency_key = "siem-public-smoke-$suffix" } | ConvertTo-Json)
$unsafeStatus = 0
try { Invoke-RestMethod -Method Put -Uri "$baseUrl/api/v1/enterprise/siem" -Headers $headers -ContentType 'application/json' -Body (@{ name = 'Unsafe SIEM'; endpoint = 'http://127.0.0.1:9200' } | ConvertTo-Json) | Out-Null } catch { $unsafeStatus = Get-ErrorStatus $_ }
if (-not $serviceAccount.id -or -not $realtimeAudit -or -not $events.items -or -not $export.content_hash -or -not $replay.idempotency_replayed -or -not $siem.credential_configured -or $siem.credential -or $delivery.status -ne 'delivered' -or $delivery.external_execution -ne 'completed' -or $delivery.delivery_mode -ne 'controlled_mock' -or -not $delivery.signature.StartsWith('sha256=') -or $delivery.id -ne $deliveryReplay.id -or $publicDelivery.status -ne 'pending_external' -or $publicDelivery.external_execution -ne 'pending' -or $publicDelivery.delivery_mode -ne 'external' -or $unsafeStatus -ne 422) { throw 'Audit/SIEM contract failed.' }
$evidence = [ordered]@{ timestamp = [DateTimeOffset]::UtcNow.ToString('o'); audit_events_read = $true; realtime_audit_chain = [bool]$realtimeAudit; export_created = [bool]$export.id; export_hash_present = [bool]$export.content_hash; export_idempotent = [bool]$replay.idempotency_replayed; siem_hash_only = (-not $siem.credential); siem_delivery_controlled = ($delivery.status -eq 'delivered' -and $delivery.external_execution -eq 'completed'); siem_hmac_present = $delivery.signature.StartsWith('sha256='); siem_delivery_idempotent = ($delivery.id -eq $deliveryReplay.id); public_siem_claimable = ($publicDelivery.status -eq 'pending_external' -and $publicDelivery.external_execution -eq 'pending' -and $publicDelivery.delivery_mode -eq 'external'); unsafe_endpoint_status = $unsafeStatus }
$evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'quality/evidence/audit-export-smoke.json' -Encoding utf8
$evidence | ConvertTo-Json -Depth 12
