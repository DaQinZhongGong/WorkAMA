# WorkAMA Postgres 备份工具（docker-compose 栈）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools/db-backup.ps1                    # 默认 backups/
#   powershell -ExecutionPolicy Bypass -File tools/db-backup.ps1 -OutDir D:\bak -KeepDays 30
#
# 设计：
#   - 二进制安全：pg_dump -Fc 在容器内落盘，再 docker cp 取回（不经宿主管道，
#     规避 PowerShell 文本管道破坏二进制流的经典坑）；
#   - 幂等可审计：文件名含 UTC 时间戳；输出 SHA256 校验和；
#   - 保留策略：默认保留最近 14 天，超出自动清理。
param(
    [string]$ComposeFile = "deploy/compose/docker-compose.yml",
    [string]$EnvFile = "",
    [string]$OutDir = "backups",
    [int]$KeepDays = 14
)
$ErrorActionPreference = "Stop"

if (-not $EnvFile) {
    if (Test-Path "deploy/compose/.env") { $EnvFile = "deploy/compose/.env" } else { $EnvFile = ".env" }
}

$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tmpInContainer = "/tmp/workama-db-$ts.dump"
$outFile = Join-Path $OutDir "workama-db-$ts.dump"

# 1) 容器内导出（自定义格式，支持并行恢复与选择性还原）
$dumpCmd = "pg_dump -U `"`$POSTGRES_USER`" -d `"`$POSTGRES_DB`" -Fc -f $tmpInContainer"
docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres sh -c $dumpCmd
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed (rc=$LASTEXITCODE)" }

# 2) 取回宿主并校验非空
$cname = (docker compose --env-file $EnvFile -f $ComposeFile ps -q postgres)
docker cp "${cname}:$tmpInContainer" $outFile
if ($LASTEXITCODE -ne 0) { throw "docker cp failed" }
$size = (Get-Item $outFile).Length
if ($size -lt 1024) { Remove-Item $outFile -Force; throw "backup suspiciously small ($size bytes); aborted" }

# 3) 清理容器内临时文件 + 写校验和
docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres rm -f $tmpInContainer | Out-Null
$sha = (Get-FileHash $outFile -Algorithm SHA256).Hash.ToLower()
Set-Content -Path "$outFile.sha256" -Value "$sha  $(Split-Path $outFile -Leaf)" -Encoding ASCII

# 4) 保留策略
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $OutDir -Filter "workama-db-*.dump*" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object { Remove-Item $_.FullName -Force; Write-Output "pruned: $($_.Name)" }

Write-Output "BACKUP_OK file=$outFile size=$size sha256=$sha"
