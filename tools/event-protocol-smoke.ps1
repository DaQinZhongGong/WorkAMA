$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email=$values.TEST_ACCOUNT_EMAIL; password=$values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$internal = @{ 'X-Internal-Token' = $values.INTERNAL_TOKEN }
$registry = Invoke-RestMethod -Uri 'http://localhost:20201/internal/event-types' -Headers $internal
if ($registry.count -ne 24 -or $registry.items.Count -ne 24) { throw 'Agent event registry is not exactly 24 types' }

$session = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Event replay validation","model":"workama-chat"}'
$ticket = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/ws-tickets" -Headers $headers
$env:WORKAMA_WS_URL = "ws://localhost:20201/ws/sessions/$($session.id)?ticket=$([uri]::EscapeDataString($ticket.ticket))&after=0"
$runRaw = node tools/event-source.mjs
if ($LASTEXITCODE -ne 0) { throw 'Event source smoke failed' }
$run = $runRaw | ConvertFrom-Json
$ticket2 = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/ws-tickets" -Headers $headers
$env:WORKAMA_WS_URL = "ws://localhost:20201/ws/sessions/$($session.id)?ticket=$([uri]::EscapeDataString($ticket2.ticket))&after=1"
$replayRaw = node tools/event-replay-smoke.mjs
Write-Host "Replay result: $replayRaw"
if ($LASTEXITCODE -ne 0) { throw 'Event replay smoke failed' }
$replay = $replayRaw | ConvertFrom-Json

$bpSession = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Backpressure validation","model":"workama-chat"}'
$sql = "INSERT INTO ag_event(id,session_id,workspace_id,seq,type,payload) SELECT 'evt_bp_' || '$($bpSession.id)' || '_' || g, '$($bpSession.id)', '$($login.user.workspace_id)', g, 'agent.thought', jsonb_build_object('display_summary',repeat('x',64),'step_id',g::text) FROM generate_series(1,1002) g; UPDATE ag_session SET last_seq=1002 WHERE id='$($bpSession.id)';"
docker compose --env-file .env -f deploy/compose/docker-compose.yml exec -T postgres psql -v ON_ERROR_STOP=1 -U $values.POSTGRES_USER -d $values.POSTGRES_DB -c $sql | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Backpressure fixture creation failed' }
$bpTicket = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($bpSession.id)/ws-tickets" -Headers $headers
$env:WORKAMA_WS_URL = "ws://localhost:20201/ws/sessions/$($bpSession.id)?ticket=$([uri]::EscapeDataString($bpTicket.ticket))&after=1"
$bpRaw = node tools/backpressure-smoke.mjs
if ($LASTEXITCODE -ne 0) { throw 'Backpressure smoke failed' }
$backpressure = $bpRaw | ConvertFrom-Json
$fleetPort = if ($values.SANDBOX_FLEET_PORT) { $values.SANDBOX_FLEET_PORT } else { '8002' }
$sourceSandbox = Invoke-RestMethod -Uri "http://localhost:$fleetPort/internal/sandboxes?session_id=$($session.id)&workspace_id=$($login.user.workspace_id)" -Headers $internal
Invoke-RestMethod -Method Delete -Uri "http://localhost:$fleetPort/internal/sandboxes/$($sourceSandbox.id)" -Headers $internal | Out-Null

$evidence = @{ timestamp=[DateTimeOffset]::UtcNow.ToString('o'); registry_count=$registry.count; registry_types=$registry.items; source_run=$run; replay=$replay; backpressure=$backpressure }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 quality/evidence/agent-event-protocol-smoke.json
$evidence | ConvertTo-Json -Depth 8
