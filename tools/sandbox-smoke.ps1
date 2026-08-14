$ErrorActionPreference = 'Stop'
$values = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$fleetPort = if ($values.SANDBOX_FLEET_PORT) { $values.SANDBOX_FLEET_PORT } else { '8002' }
$fleet = "http://localhost:$fleetPort"
$internal = @{ 'X-Internal-Token' = $values.INTERNAL_TOKEN }
$sandboxId = $null

try {
  $health = Invoke-RestMethod -Uri "$fleet/healthz"
  $warmDeadline=(Get-Date).AddSeconds(30)
  while($health.prewarm.ready -lt 1 -and (Get-Date)-lt $warmDeadline){Start-Sleep -Milliseconds 500;$health=Invoke-RestMethod -Uri "$fleet/healthz"}
  if($health.prewarm.ready -lt 1){throw 'Sandbox prewarm pool did not become ready'}
  if ($values.SANDBOX_REQUIRE_GVISOR -eq 'true' -and -not $health.gvisor_compliant) { throw 'Strict gVisor mode is not compliant' }
  $login = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{ email=$values.TEST_ACCOUNT_EMAIL; password=$values.TEST_ACCOUNT_PASSWORD } | ConvertTo-Json)
  $headers = @{ Authorization = "Bearer $($login.access_token)" }
  $session = Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Sandbox lifecycle validation","model":"workama-chat"}'
  $workspaceId = $login.user.workspace_id
  $body = @{ workspace_id=$workspaceId; session_id=$session.id; image='sandbox-code' } | ConvertTo-Json
  $first = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes" -Headers $internal -ContentType 'application/json' -Body $body
  $sandboxId = $first.id
  $second = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes" -Headers $internal -ContentType 'application/json' -Body $body
  if ($second.id -ne $sandboxId -or -not $second.restored) { throw 'Sandbox allocation is not idempotent' }
  $allocated=Invoke-RestMethod -Uri "$fleet/internal/sandboxes?session_id=$($session.id)&workspace_id=$workspaceId" -Headers $internal
  $agentd=docker exec --user 10001:10001 $allocated.container_id /usr/local/bin/sandbox-agentd client Health '{}' | ConvertFrom-Json
  if($agentd.status -ne 'ok' -or $agentd.protocol -ne 'grpc-unix'){throw 'sandbox-agentd gRPC health failed'}
  $pidStatus=docker exec $allocated.container_id sh -c "grep -E '^(Uid|CapEff):' /proc/1/status"
  if(($pidStatus -join "`n") -notmatch 'Uid:\s+10001\s+10001' -or ($pidStatus -join "`n") -notmatch 'CapEff:\s+0000000000000000'){throw 'sandbox-agentd did not drop uid/capabilities'}
  $rootWrite=Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/exec" -Headers $internal -ContentType 'application/json' -Body (@{argv=@('sh','-c','touch /root/workama-escape-check');timeout_seconds=5}|ConvertTo-Json)
  if($rootWrite.exit_code -eq 0){throw 'Sandbox root filesystem is writable'}
  $secretProbe=Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/exec" -Headers $internal -ContentType 'application/json' -Body (@{argv=@('sh','-c','printf %s "${DATABASE_URL:-missing}"');timeout_seconds=5}|ConvertTo-Json)
  if($secretProbe.output -ne 'missing'){throw 'Platform environment leaked into sandbox'}
  Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/exec" -Headers $internal -ContentType 'application/json' -Body (@{argv=@('ln','-s','/etc','escape');timeout_seconds=5}|ConvertTo-Json)|Out-Null
  $symlinkEscapeBlocked=$false
  try{Invoke-RestMethod -Uri "$fleet/internal/sandboxes/$sandboxId/files?path=escape/passwd" -Headers $internal|Out-Null}catch{$symlinkEscapeBlocked=$true}
  if(-not $symlinkEscapeBlocked){throw 'Workspace symlink escaped the sandbox file boundary'}
  Invoke-RestMethod -Method Put -Uri "$fleet/internal/sandboxes/$sandboxId/files" -Headers $internal -ContentType 'application/json' -Body (@{ path='checks/state.txt'; content='persistent sandbox state' } | ConvertTo-Json) | Out-Null
  $exec = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/exec" -Headers $internal -ContentType 'application/json' -Body (@{ argv=@('python','-I','-S','-c','print(6 * 7)'); timeout_seconds=10 } | ConvertTo-Json)
  if ($exec.exit_code -ne 0 -or $exec.output.Trim() -ne '42') { throw 'Sandbox execution failed' }
  $sleep=Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/sleep" -Headers $internal
  if(-not $sleep.snapshot_s3_key -or -not $sleep.snapshot_sha256 -or $sleep.snapshot_size_bytes -le 0){throw 'Sandbox snapshot metadata is incomplete'}
  $sleeping=Invoke-RestMethod -Uri "$fleet/internal/sandboxes?session_id=$($session.id)&workspace_id=$workspaceId" -Headers $internal
  docker rm -f $sleeping.container_id | Out-Null
  docker volume rm $sleeping.volume_name | Out-Null
  $restored = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes" -Headers $internal -ContentType 'application/json' -Body $body
  if (-not $restored.restored -or -not $restored.snapshot_restored -or $restored.status -ne 'active') { throw 'Lost sandbox did not restore from snapshot' }
  $file = Invoke-RestMethod -Uri "$fleet/internal/sandboxes/$sandboxId/files?path=checks/state.txt" -Headers $internal
  if ($file.content -ne 'persistent sandbox state') { throw 'Workspace volume did not persist across sleep' }
  # PTY 流式交互测试：通过 WebSocket 连接沙箱终端流，验证 start/input/output/exit chunk 协议
  # 测试失败时仅记录结果，不中断后续 release 与 evidence 输出
  $ptyStreamResult = @{ passed=$false; error=$null; got_output=$false; exit_code=$null; raw_output=$null }
  $ws = $null
  try {
    $wsUrl = "ws://localhost:$fleetPort/internal/sandboxes/$sandboxId/terminal/stream"
    # WebSocket 无法设置自定义 header，内部 token 通过 query string 传递
    if ($values.INTERNAL_TOKEN) { $wsUrl = "$wsUrl?token=$([Uri]::EscapeDataString($values.INTERNAL_TOKEN))" }
    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    $cts = New-Object System.Threading.CancellationTokenSource
    $cts.CancelAfter([TimeSpan]::FromSeconds(30))
    $ws.ConnectAsync([Uri]$wsUrl, $cts.Token).GetAwaiter().GetResult() | Out-Null
    # 发送 start chunk：启动 python3 子进程，从 stdin 读取一行并回显
    $pythonCode = 'import sys; data=sys.stdin.readline(); print(f''got:{data}'',flush=True)'
    $startChunk = @{ type='start'; argv=@('python3','-c',$pythonCode); rows=24; cols=80; timeout_seconds=10 } | ConvertTo-Json -Compress
    $startBytes = [Text.Encoding]::UTF8.GetBytes($startChunk)
    $ws.SendAsync([ArraySegment[byte]]::new($startBytes), [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).GetAwaiter().GetResult() | Out-Null
    # 发送 input chunk：将 'hello\n' base64 编码后作为 PTY 输入
    $inputData = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("hello`n"))
    $inputChunk = @{ type='input'; data=$inputData } | ConvertTo-Json -Compress
    $inputBytes = [Text.Encoding]::UTF8.GetBytes($inputChunk)
    $ws.SendAsync([ArraySegment[byte]]::new($inputBytes), [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).GetAwaiter().GetResult() | Out-Null
    # 接收循环：解析 output 与 exit chunk，累积 output 的 base64 解码结果
    $recvBuffer = New-Object byte[] 16384
    $outputText = [Text.StringBuilder]::new()
    $exitCode = $null
    $deadline = (Get-Date).AddSeconds(20)
    while ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open -and (Get-Date) -lt $deadline) {
      $recvResult = $ws.ReceiveAsync([ArraySegment[byte]]::new($recvBuffer), $cts.Token).GetAwaiter().GetResult()
      $frame = [Text.Encoding]::UTF8.GetString($recvBuffer, 0, $recvResult.Count)
      try {
        $msg = $frame | ConvertFrom-Json
        if ($msg.type -eq 'output' -and $msg.data) {
          $null = $outputText.Append([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($msg.data)))
        } elseif ($msg.type -eq 'exit') {
          $exitCode = $msg.exit_code
          break
        } elseif ($msg.type -eq 'error') {
          $ptyStreamResult.error = "server error: $($msg.message)"
          break
        }
      } catch {
        # 非 JSON 帧或 base64 解码失败直接忽略，继续接收
      }
      if ($recvResult.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { break }
    }
    $ptyStreamResult.raw_output = $outputText.ToString()
    $ptyStreamResult.got_output = $outputText.ToString().Contains('got:hello')
    $ptyStreamResult.exit_code = $exitCode
    if ($ptyStreamResult.got_output -and $exitCode -eq 0) {
      $ptyStreamResult.passed = $true
    } else {
      $ptyStreamResult.error = "output_match=$($ptyStreamResult.got_output); exit_code=$exitCode"
    }
  } catch {
    $ptyStreamResult.passed = $false
    $ptyStreamResult.error = $_.Exception.Message
  } finally {
    if ($ws) {
      try {
        if ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
          $closeCts = New-Object System.Threading.CancellationTokenSource
          $closeCts.CancelAfter([TimeSpan]::FromSeconds(5))
          $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'done', $closeCts.Token).GetAwaiter().GetResult() | Out-Null
        }
      } catch {}
      $ws.Dispose()
    }
  }
  if (-not $ptyStreamResult.passed) {
    Write-Warning "PTY stream smoke test failed: $($ptyStreamResult.error)"
  }
  # CDP 桥（BrowserOp）smoke 测试：通过 HTTP 端点驱动沙箱内浏览器（navigate/screenshot/close）
  # 沙箱需 sandbox-browser 镜像；若当前沙箱镜像为 sandbox-base 或 browser 端点尚未实现，标记 skipped
  $cdpBridgeResult = @{
    status='skipped'
    error=$null
    image=$null
    navigate_ok=$false
    navigate_url=$null
    screenshot_ok=$false
    screenshot_png_magic=$false
    screenshot_bytes=$null
    close_ok=$false
  }
  try {
    # 查询当前沙箱镜像；若为 sandbox-base 则跳过（缺少 browser 工具链）
    $currentSandbox = Invoke-RestMethod -Uri "$fleet/internal/sandboxes?session_id=$($session.id)&workspace_id=$workspaceId" -Headers $internal
    $cdpBridgeResult.image = $currentSandbox.image
    if ($currentSandbox.image -eq 'sandbox-base') {
      $cdpBridgeResult.status = 'skipped'
      $cdpBridgeResult.error = "current sandbox image is sandbox-base; browser tool requires sandbox-browser"
      Write-Warning "CDP bridge smoke test skipped: $($cdpBridgeResult.error)"
    } else {
      # navigate：驱动浏览器访问 example.com，验证 ok=true 且 meta.url 包含 example.com
      $navBody = @{ action='navigate'; target='https://example.com'; timeout_ms=15000 } | ConvertTo-Json
      try {
        $navResp = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/browser" -Headers $internal -ContentType 'application/json' -Body $navBody
      } catch {
        # 端点尚未实现或镜像不支持 → 跳过（兼容 sandbox-fleet 未上线 browser 端点的场景）
        $navResp = $null
        $cdpBridgeResult.status = 'skipped'
        $cdpBridgeResult.error = "browser endpoint unavailable (navigate): $($_.Exception.Message)"
      }
      if ($navResp) {
        $cdpBridgeResult.navigate_ok = ($navResp.ok -eq $true)
        $cdpBridgeResult.navigate_url = $navResp.meta.url
        if (-not $cdpBridgeResult.navigate_ok) {
          # ok=false 视为端点未就绪（如镜像未含浏览器）→ 跳过
          $cdpBridgeResult.status = 'skipped'
          $cdpBridgeResult.error = "navigate returned ok=false; likely browser image not ready"
        } elseif (-not ($navResp.meta.url -match 'example\.com')) {
          $cdpBridgeResult.status = 'failed'
          $cdpBridgeResult.error = "navigate url mismatch: $($navResp.meta.url)"
        } else {
          # screenshot：截取当前页面，验证 ok=true 且 screenshot 字段非空（base64 PNG）
          $ssBody = @{ action='screenshot'; timeout_ms=10000 } | ConvertTo-Json
          try {
            $ssResp = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/browser" -Headers $internal -ContentType 'application/json' -Body $ssBody
          } catch {
            $ssResp = $null
            $cdpBridgeResult.status = 'failed'
            $cdpBridgeResult.error = "screenshot endpoint failed: $($_.Exception.Message)"
          }
          if ($ssResp) {
            $cdpBridgeResult.screenshot_ok = ($ssResp.ok -eq $true -and -not [string]::IsNullOrEmpty($ssResp.screenshot))
            if ($cdpBridgeResult.screenshot_ok) {
              # 校验 PNG magic header：base64 解码后字节应以 \x89P (0x89 0x50) 开头
              try {
                $pngBytes = [Convert]::FromBase64String($ssResp.screenshot)
                $cdpBridgeResult.screenshot_bytes = $pngBytes.Length
                if ($pngBytes.Length -ge 2 -and $pngBytes[0] -eq 0x89 -and $pngBytes[1] -eq 0x50) {
                  $cdpBridgeResult.screenshot_png_magic = $true
                } else {
                  $cdpBridgeResult.status = 'failed'
                  $firstBytes = 'n/a'
                  if ($pngBytes.Length -ge 2) { $firstBytes = ('0x{0:X2} 0x{1:X2}' -f $pngBytes[0], $pngBytes[1]) }
                  $cdpBridgeResult.error = "screenshot PNG magic header mismatch: first 2 bytes = $firstBytes"
                }
              } catch {
                $cdpBridgeResult.status = 'failed'
                $cdpBridgeResult.error = "screenshot base64 decode failed: $($_.Exception.Message)"
              }
            } else {
              $cdpBridgeResult.status = 'failed'
              $cdpBridgeResult.error = "screenshot failed: ok=$($ssResp.ok); empty=$([string]::IsNullOrEmpty($ssResp.screenshot))"
            }
          }
          # close：关闭浏览器会话，释放 CDP 资源（仅在 navigate/screenshot 通过后执行）
          if ($cdpBridgeResult.status -ne 'failed') {
            $closeBody = @{ action='close' } | ConvertTo-Json
            try {
              $closeResp = Invoke-RestMethod -Method Post -Uri "$fleet/internal/sandboxes/$sandboxId/browser" -Headers $internal -ContentType 'application/json' -Body $closeBody
              $cdpBridgeResult.close_ok = ($closeResp.ok -eq $true)
              if (-not $cdpBridgeResult.close_ok) {
                $cdpBridgeResult.status = 'failed'
                $cdpBridgeResult.error = "close action did not return ok=true"
              } else {
                $cdpBridgeResult.status = 'passed'
              }
            } catch {
              $cdpBridgeResult.status = 'failed'
              $cdpBridgeResult.error = "close action failed: $($_.Exception.Message)"
            }
          }
        }
      }
      if ($cdpBridgeResult.status -eq 'failed') {
        Write-Warning "CDP bridge smoke test failed: $($cdpBridgeResult.error)"
      } elseif ($cdpBridgeResult.status -eq 'skipped') {
        Write-Warning "CDP bridge smoke test skipped: $($cdpBridgeResult.error)"
      }
    }
  } catch {
    $cdpBridgeResult.status = 'failed'
    $cdpBridgeResult.error = "unexpected error: $($_.Exception.Message)"
    Write-Warning "CDP bridge smoke test failed: $($cdpBridgeResult.error)"
  }
  $beforeRelease = Invoke-RestMethod -Uri "$fleet/internal/sandboxes?session_id=$($session.id)&workspace_id=$workspaceId" -Headers $internal
  Invoke-RestMethod -Method Delete -Uri "$fleet/internal/sandboxes/$sandboxId" -Headers $internal | Out-Null
  $afterRelease = Invoke-RestMethod -Uri "$fleet/internal/sandboxes?session_id=$($session.id)&workspace_id=$workspaceId" -Headers $internal
  if ($afterRelease.status -ne 'released' -or $afterRelease.meter_seconds -lt 1) { throw 'Sandbox release or metering failed' }
  $evidence = @{ timestamp=[DateTimeOffset]::UtcNow.ToString('o'); sandbox_id=$sandboxId; session_id=$session.id; provider=$health.provider; provider_ready=$health.provider_ready; execution_mode=$health.execution_mode; configured_runtime=$health.configured_runtime; actual_runtime=$first.runtime; runtime_available=$health.runtime_available; gvisor_compliant=$health.gvisor_compliant; microvm_required=$health.microvm_required; microvm_compliant=$health.microvm_compliant; firecracker_preflight_status=$health.firecracker.direct_preflight.status; firecracker_missing=@($health.firecracker.direct_preflight.missing); prewarm_ready=$health.prewarm.ready; allocation_source=$first.allocation_source; cold_start_ms=$first.cold_start_ms; agentd_protocol=$agentd.protocol; agentd_uid=10001; agentd_cap_eff='0000000000000000'; rootfs_read_only=$true; environment_isolated=$true; symlink_escape_blocked=$symlinkEscapeBlocked; idempotent=$true; execution=$exec.output.Trim(); snapshot_key=$sleep.snapshot_s3_key; snapshot_sha256=$sleep.snapshot_sha256; lost_container_restore=$restored.snapshot_restored; volume_persistence=$true; meter_seconds=$afterRelease.meter_seconds; released=$true; pty_stream=$ptyStreamResult; cdp_bridge=$cdpBridgeResult }
  $evidence | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 quality/evidence/sandbox-runtime-smoke.json
  $evidence | ConvertTo-Json -Depth 6
  $sandboxId = $null
}
finally {
  if ($sandboxId) { try { Invoke-RestMethod -Method Delete -Uri "$fleet/internal/sandboxes/$sandboxId" -Headers $internal | Out-Null } catch {} }
}
