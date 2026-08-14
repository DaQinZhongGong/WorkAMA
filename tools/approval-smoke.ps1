$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email=$values.TEST_ACCOUNT_EMAIL; password=$values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$internal = @{ 'X-Internal-Token' = $values.INTERNAL_TOKEN }
$fleetPort = if ($values.SANDBOX_FLEET_PORT) { $values.SANDBOX_FLEET_PORT } else { '8002' }
$runs = @()

foreach ($decision in @('approved', 'rejected')) {
  $session = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body (@{ title="Approval $decision validation"; model='workama-chat' } | ConvertTo-Json)
  $ticket = Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/ws-tickets" -Headers $headers
  $url = "ws://localhost:20201/ws/sessions/$($session.id)?ticket=$([uri]::EscapeDataString($ticket.ticket))"
  $marker = if ($decision -eq 'approved') { 'approval-ok' } else { 'rejected-command-marker' }
  $run = node tools/approval-smoke.mjs $url $login.access_token $decision $marker | ConvertFrom-Json
  if (-not $run.ok) { throw "$decision approval flow failed" }
  $approval = Invoke-RestMethod -Uri "http://localhost:20200/api/v1/approvals/$($run.approval_id)" -Headers $headers
  $expected = if ($decision -eq 'approved') { 'consumed' } else { 'rejected' }
  if ($approval.status -ne $expected) { throw "$decision approval ended as $($approval.status)" }
  if ($decision -eq 'approved') {
    try {
      Invoke-RestMethod -Method Post -Uri "http://localhost:20200/internal/approvals/$($run.approval_id)/consume" -Headers $internal -ContentType 'application/json' -Body (@{ action_hash=$run.action_hash } | ConvertTo-Json) | Out-Null
      throw 'Consumed approval was replayed'
    } catch {
      if ($_.Exception.Response.StatusCode.value__ -ne 412) { throw }
    }
    $sandbox = Invoke-RestMethod -Uri "http://localhost:$fleetPort/internal/sandboxes?session_id=$($session.id)&workspace_id=$($login.user.workspace_id)" -Headers $internal
    Invoke-RestMethod -Method Delete -Uri "http://localhost:$fleetPort/internal/sandboxes/$($sandbox.id)" -Headers $internal | Out-Null
  }
  $events = Invoke-RestMethod -Uri "http://localhost:20200/api/v1/sessions/$($session.id)/events" -Headers $headers
  if ('error' -in $events.items.type -or 'session.status' -notin $events.items.type) { throw "$decision flow persisted an error or missed session status" }
  $runs += @{ decision=$decision; session_id=$session.id; approval_id=$run.approval_id; final_status=$approval.status; result_status=$run.result_status; event_types=@($events.items.type | Select-Object -Unique) }
}

$hashA = 'a' * 64; $hashB = 'b' * 64; $callId = "call_hash_$([Guid]::NewGuid().ToString('N'))"
$hashApproval = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/internal/approvals' -Headers $internal -ContentType 'application/json' -Body (@{ workspace_id=$login.user.workspace_id; session_id=$session.id; call_id=$callId; requester_id=$login.user.id; tool_name='terminal'; action_hash=$hashA; risk='A3'; preview=@{ purpose='hash binding test' }; ttl_seconds=120 } | ConvertTo-Json -Depth 5)
try {
  Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/internal/approvals' -Headers $internal -ContentType 'application/json' -Body (@{ workspace_id=$login.user.workspace_id; session_id=$session.id; call_id=$callId; requester_id=$login.user.id; tool_name='terminal'; action_hash=$hashB; risk='A3'; preview=@{}; ttl_seconds=120 } | ConvertTo-Json -Depth 5) | Out-Null
  throw 'Duplicate call changed action hash'
} catch { if ($_.Exception.Response.StatusCode.value__ -ne 409) { throw } }
Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/approvals/$($hashApproval.id)/decisions" -Headers $headers -ContentType 'application/json' -Body '{"decision":"approved","reason":"Hash binding acceptance"}' | Out-Null
try {
  Invoke-RestMethod -Method Post -Uri "http://localhost:20200/internal/approvals/$($hashApproval.id)/consume" -Headers $internal -ContentType 'application/json' -Body (@{ action_hash=$hashB } | ConvertTo-Json) | Out-Null
  throw 'Wrong action hash consumed approval'
} catch { if ($_.Exception.Response.StatusCode.value__ -ne 412) { throw } }
Invoke-RestMethod -Method Post -Uri "http://localhost:20200/internal/approvals/$($hashApproval.id)/consume" -Headers $internal -ContentType 'application/json' -Body (@{ action_hash=$hashA } | ConvertTo-Json) | Out-Null

$grant = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/tool-grants' -Headers $headers -ContentType 'application/json' -Body (@{ tool_name='file.write'; scope='workspace'; max_risk='A2' } | ConvertTo-Json)
$listed = Invoke-RestMethod -Uri 'http://localhost:20200/api/v1/tool-grants' -Headers $headers
if ($grant.id -notin $listed.items.id) { throw 'Tool grant was not listed' }
Invoke-RestMethod -Method Delete -Uri "http://localhost:20200/api/v1/tool-grants/$($grant.id)" -Headers $headers -ContentType 'application/json' -Body '{"reason":"Acceptance cleanup"}' | Out-Null
$evidence = @{ timestamp=[DateTimeOffset]::UtcNow.ToString('o'); runs=$runs; replay_blocked=$true; duplicate_hash_mutation_blocked=$true; wrong_hash_consume_blocked=$true; action_hash_bound=$true; grant_created_listed_revoked=$true }
$evidence | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 quality/evidence/tool-approval-smoke.json
$evidence | ConvertTo-Json -Depth 8
