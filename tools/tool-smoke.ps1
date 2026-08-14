$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email=$values.TEST_ACCOUNT_EMAIL; password=$values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$catalog = Invoke-RestMethod -Uri 'http://localhost:20200/api/v1/tools' -Headers $headers
$session = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Tool validation","model":"workama-chat"}'
$ticket = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/ws-tickets" -Headers $headers
$url = "ws://localhost:20201/ws/sessions/$($session.id)?ticket=$([uri]::EscapeDataString($ticket.ticket))"
$rawResult = node tools/tool-smoke.mjs $url
if ($LASTEXITCODE -ne 0) { throw "Tool WebSocket smoke failed with exit code $LASTEXITCODE" }
$result = $rawResult | ConvertFrom-Json
if (-not $result.ok) { throw 'Tool WebSocket smoke returned an invalid result' }
$events = Invoke-RestMethod -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/events" -Headers $headers
$artifacts = Invoke-RestMethod -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/artifacts" -Headers $headers
$fleetPort = if ($values.SANDBOX_FLEET_PORT) { $values.SANDBOX_FLEET_PORT } else { '8002' }
$internal = @{ 'X-Internal-Token' = $values.INTERNAL_TOKEN }
$sandbox = Invoke-RestMethod -Uri "http://localhost:$fleetPort/internal/sandboxes?session_id=$($session.id)&workspace_id=$($login.user.workspace_id)" -Headers $internal
$evidence = @{ timestamp=[DateTimeOffset]::UtcNow.ToString('o'); session_id=$session.id; registry_version=$catalog.registry_version; tool_count=$catalog.items.Count; websocket_ok=$result.ok; persisted_event_types=@($events.items.type | Select-Object -Unique); artifact_count=$artifacts.items.Count; sandbox_id=$sandbox.id; sandbox_runtime=$sandbox.runtime; gvisor_compliant=$sandbox.gvisor_compliant }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 quality/evidence/tool-runtime-smoke.json
Invoke-RestMethod -Method Delete -Uri "http://localhost:$fleetPort/internal/sandboxes/$($sandbox.id)" -Headers $internal | Out-Null
$evidence | ConvertTo-Json -Depth 8
