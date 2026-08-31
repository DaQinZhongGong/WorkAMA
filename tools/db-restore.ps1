# WorkAMA Postgres 恢复工具（docker-compose 栈）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools/db-restore.ps1 -File backups\workama-db-<ts>.dump
#   …加 -Confirm 才会真正执行写操作；否则仅打印计划（dry-run 默认）。
#
# 设计：
#   - pg_restore --clean --if-exists：先删后建，幂等于既有库；
#   - 二进制安全：docker cp 注入容器内 /tmp 后由 pg_restore 读取；
#   - 安全阀：必须显式 -Confirm；恢复期间应用侧连接会被 --clean 短暂影响，
#     生产应在维护窗口执行。
param(
    [Parameter(Mandatory = $true)][string]$File,
    [switch]$Confirm,
    [string]$ComposeFile = "deploy/compose/docker-compose.yml",
    [string]$EnvFile = ""
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $File)) { throw "backup file not found: $File" }
if (-not $EnvFile) {
    if (Test-Path "deploy/compose/.env") { $EnvFile = "deploy/compose/.env" } else { $EnvFile = ".env" }
}

if (-not $Confirm) {
    Write-Output "DRY-RUN: would restore '$File' into compose postgres (pg_restore --clean --if-exists)."
    Write-Output "Re-run with -Confirm to execute."
    return
}

$tmpInContainer = "/tmp/workama-restore.dump"
$cname = (docker compose --env-file $EnvFile -f $ComposeFile ps -q postgres)
docker cp $File "${cname}:$tmpInContainer"
if ($LASTEXITCODE -ne 0) { throw "docker cp failed" }

$restoreCmd = "pg_restore -U `"`$POSTGRES_USER`" -d `"`$POSTGRES_DB`" --clean --if-exists --no-owner --no-privileges $tmpInContainer"
docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres sh -c $restoreCmd
$rc = $LASTEXITCODE
docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres rm -f $tmpInContainer | Out-Null
if ($rc -ne 0) { throw "pg_restore reported errors (rc=$rc)；请检查上方明细（--clean 对不存在对象会跳过）" }
Write-Output "RESTORE_OK file=$File"
