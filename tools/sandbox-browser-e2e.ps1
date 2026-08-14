$ErrorActionPreference = 'Stop'

# tools/sandbox-browser-e2e.ps1 — sandbox-browser 端到端活体测试
#
# 直接启动一个 sandbox-browser 容器（模拟 fleet 行为），通过 agentd client 调用
# BrowserOp RPC，验证 navigate/screenshot/eval/close 基本功能。
# 对齐《520-Agent引擎与运行时设计》§4.3 浏览器 CDP 桥 与
# 《710-API事件与集成契约详细设计》SandboxService BrowserOp 契约。
#
# 输出 JSON evidence 到 quality/evidence/sandbox-browser-e2e.json，结尾输出 PASS/FAIL 汇总。

$containerName = 'workama-e2e-browser'
$image = 'workama-sandbox-browser:local'
$agentdBin = '/usr/local/bin/sandbox-agentd'
$evidenceDir = 'quality/evidence'
$evidencePath = Join-Path $evidenceDir 'sandbox-browser-e2e.json'

# 初始化 evidence（用于失败时也能输出）
$evidence = [ordered]@{
  timestamp = [DateTimeOffset]::UtcNow.ToString('o')
  navigate_ok = $false
  screenshot_ok = $false
  screenshot_magic = $false
  eval_ok = $false
  eval_result = $null
  close_ok = $false
  error = ''
}

function Write-Evidence {
  param([hashtable]$Data)
  if (-not (Test-Path $evidenceDir)) { New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null }
  $Data | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $evidencePath
}

function Invoke-BrowserOp {
  param([hashtable]$Body)
  # PS 5.1 调用原生命令时会剥除双引号，导致 JSON 参数传递给容器内 agentd 时损坏
  # 改用 base64 编码 JSON，在容器内解码后再传给 client，绕过 PS 5.1 转义问题
  $json = $Body | ConvertTo-Json -Compress -Depth 4
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
  $raw = & docker exec $containerName sh -c "$agentdBin client BrowserOp `$(echo $b64 | base64 -d)" 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "BrowserOp $($Body.action) failed (exit=$LASTEXITCODE): $($raw | Out-String)"
  }
  # agentd 成功时向 stdout 输出一行 JSON；Out-String 合并后 Trim 去尾部换行
  $text = ($raw | Out-String).Trim()
  return $text | ConvertFrom-Json
}

try {
  # 前置：docker 守护进程运行
  & docker info *> $null
  if ($LASTEXITCODE -ne 0) { throw 'docker daemon is not running' }

  # 前置：sandbox-browser 镜像存在
  $imageCheck = & docker images --format '{{.Repository}}:{{.Tag}}' $image 2>$null
  if (-not $imageCheck) { throw "image $image not found; build it first" }

  # 清理同名残留容器（容器不存在时 docker rm 会写 stderr 并返回非零，
  # 但 $ErrorActionPreference='Stop' 会把 stderr 当作终止错误抛出，故临时切回 Continue）
  $prevPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & docker rm -f $containerName *> $null } catch {}
  $ErrorActionPreference = $prevPref

  # 步骤 1：启动临时 sandbox-browser 容器（模拟 fleet 行为）
  Write-Host '[1/4] 启动 sandbox-browser 容器...'
  & docker run -d --name $containerName --security-opt seccomp=unconfined --cap-add=SYS_ADMIN $image serve *> $null
  if ($LASTEXITCODE -ne 0) { throw "docker run failed with exit code $LASTEXITCODE" }

  # 步骤 2：等待 agentd 健康（轮询 Health RPC，最多 30 秒）
  Write-Host '[2/4] 等待 agentd 健康...'
  $healthy = $false
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    try {
      $healthRaw = & docker exec $containerName $agentdBin client Health '{}' 2>&1
      if ($LASTEXITCODE -eq 0) {
        $health = ($healthRaw | Out-String).Trim() | ConvertFrom-Json
        if ($health.status -eq 'ok') { $healthy = $true; break }
      }
    } catch {}
    Start-Sleep -Milliseconds 500
  }
  if (-not $healthy) { throw 'agentd did not become healthy within 30 seconds' }

  # 步骤 3：直接调用 BrowserOp RPC 验证基本功能
  Write-Host '[3/4] 调用 BrowserOp RPC...'

  # navigate about:blank → 期望 ok=true
  try {
    $nav = Invoke-BrowserOp -Body @{ action='navigate'; target='about:blank'; timeout_ms=15000 }
    $evidence.navigate_ok = ($nav.ok -eq $true)
  } catch {
    if ($evidence.error) { $evidence.error += '; ' }
    $evidence.error += "navigate: $($_.Exception.Message)"
  }

  # screenshot → 期望 ok=true 且 screenshot 字段以 iVBORw0KGgo 开头（PNG base64 magic）
  try {
    $ss = Invoke-BrowserOp -Body @{ action='screenshot'; timeout_ms=10000 }
    $evidence.screenshot_ok = ($ss.ok -eq $true -and -not [string]::IsNullOrEmpty($ss.screenshot))
    if ($evidence.screenshot_ok) {
      $evidence.screenshot_magic = $ss.screenshot.StartsWith('iVBORw0KGgo')
    }
  } catch {
    if ($evidence.error) { $evidence.error += '; ' }
    $evidence.error += "screenshot: $($_.Exception.Message)"
  }

  # eval 1+2 → 期望 ok=true 且 meta.result=3
  try {
    $ev = Invoke-BrowserOp -Body @{ action='eval'; params=@{ expression='1+2' }; timeout_ms=5000 }
    $evidence.eval_ok = ($ev.ok -eq $true)
    if ($evidence.eval_ok) {
      $evidence.eval_result = [int]$ev.meta.result
    }
  } catch {
    if ($evidence.error) { $evidence.error += '; ' }
    $evidence.error += "eval: $($_.Exception.Message)"
  }

  # close → 期望 ok=true
  try {
    $cl = Invoke-BrowserOp -Body @{ action='close' }
    $evidence.close_ok = ($cl.ok -eq $true)
  } catch {
    if ($evidence.error) { $evidence.error += '; ' }
    $evidence.error += "close: $($_.Exception.Message)"
  }

  # 步骤 4：清理容器
  Write-Host '[4/4] 清理容器...'
  $prevPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & docker rm -f $containerName *> $null } catch {}
  $ErrorActionPreference = $prevPref

  # 输出 evidence
  Write-Evidence -Data $evidence

  # 汇总
  $allPass = $evidence.navigate_ok -and $evidence.screenshot_ok -and $evidence.screenshot_magic `
    -and $evidence.eval_ok -and ($evidence.eval_result -eq 3) -and $evidence.close_ok

  Write-Host ''
  Write-Host '===== sandbox-browser E2E 汇总 ====='
  Write-Host "navigate_ok      : $($evidence.navigate_ok)"
  Write-Host "screenshot_ok    : $($evidence.screenshot_ok)"
  Write-Host "screenshot_magic : $($evidence.screenshot_magic)"
  Write-Host "eval_ok          : $($evidence.eval_ok)"
  Write-Host "eval_result      : $($evidence.eval_result)"
  Write-Host "close_ok         : $($evidence.close_ok)"
  Write-Host ''
  if ($allPass) {
    Write-Host '结果: PASS'
    exit 0
  } else {
    Write-Host '结果: FAIL'
    exit 1
  }
}
catch {
  $evidence.error = $_.Exception.Message
  Write-Evidence -Data $evidence
  # 确保容器被清理
  $prevPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & docker rm -f $containerName *> $null } catch {}
  $ErrorActionPreference = $prevPref
  Write-Host ''
  Write-Host '===== sandbox-browser E2E 汇总 ====='
  Write-Host "结果: FAIL - $($evidence.error)"
  exit 1
}
