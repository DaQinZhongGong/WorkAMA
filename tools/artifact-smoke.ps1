$ErrorActionPreference = 'Stop'
$values=@{}; Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]]=$matches[2] } }
$login=Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/auth/login' -ContentType 'application/json' -Body (@{email=$values.TEST_ACCOUNT_EMAIL;password=$values.TEST_ACCOUNT_PASSWORD}|ConvertTo-Json)
$headers=@{Authorization="Bearer $($login.access_token)"}; $internal=@{'X-Internal-Token'=$values.INTERNAL_TOKEN}
$session=Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/sessions' -Headers $headers -ContentType 'application/json' -Body '{"title":"Artifact storage validation","model":"workama-chat"}'
$content='MinIO artifact source of truth'; $created=Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/internal/artifacts' -Headers $internal -ContentType 'application/json' -Body (@{workspace_id=$login.user.workspace_id;session_id=$session.id;name='storage-check.txt';content_type='text/plain';content=$content;kind='file'}|ConvertTo-Json)
$artifact=Invoke-RestMethod -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)" -Headers $headers
if (-not $created.storage_ref.StartsWith("artifacts/$($login.user.workspace_id)/") -or $artifact.size_bytes -ne $content.Length -or -not $artifact.content_sha256 -or $artifact.preview.text -ne $content) { throw 'Artifact metadata or object key is incomplete' }
$download=Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)/downloads" -Headers $headers
$downloaded=(Invoke-WebRequest -Uri "http://localhost:20200$($download.url)" -UseBasicParsing).Content
if ($downloaded -ne $content) { throw 'Signed artifact download failed' }
$share=Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)/shares" -Headers $headers -ContentType 'application/json' -Body '{"expires_in_seconds":3600,"max_downloads":1}'
$public=(Invoke-WebRequest -Uri "http://localhost:20200$($share.url)" -UseBasicParsing).Content
if ($public -ne $content) { throw 'Public artifact share failed' }
try { Invoke-WebRequest -Uri "http://localhost:20200$($share.url)" -UseBasicParsing | Out-Null; throw 'Share max_downloads was not enforced' } catch { if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw } }
Invoke-RestMethod -Method Delete -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)" -Headers $headers -ContentType 'application/json' -Body '{"reason":"Artifact storage acceptance cleanup"}' | Out-Null
try { Invoke-WebRequest -Uri "http://localhost:20200$($download.url)" -UseBasicParsing | Out-Null; throw 'Deleted artifact remained downloadable' } catch { if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw } }
Invoke-RestMethod -Method Post -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)/restore" -Headers $headers | Out-Null
$restored=(Invoke-WebRequest -Uri "http://localhost:20200$($download.url)" -UseBasicParsing).Content
if ($restored -ne $content) { throw 'Artifact restore failed' }
Invoke-RestMethod -Method Delete -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)" -Headers $headers -ContentType 'application/json' -Body '{"reason":"Final acceptance cleanup"}' | Out-Null
docker compose --env-file .env -f deploy/compose/docker-compose.yml exec -T postgres psql -v ON_ERROR_STOP=1 -U $values.POSTGRES_USER -d $values.POSTGRES_DB -c "UPDATE ag_artifact SET deleted_at=now()-interval '31 days',purge_after=now()-interval '1 day' WHERE id='$($created.id)'" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Artifact lifecycle fixture failed' }
Invoke-RestMethod -Method Put -Uri 'http://localhost:20200/api/v1/admin/lifecycle-policies/artifact' -Headers $headers -ContentType 'application/json' -Body '{"retention_days":30,"batch_size":100,"status":"enabled","runbook":"Verify legal hold and object deletion before purge."}' | Out-Null
$run=Invoke-RestMethod -Method Post -Uri 'http://localhost:20200/api/v1/admin/lifecycle-runs' -Headers $headers -ContentType 'application/json' -Body '{"resource_type":"artifact","dry_run":false}'
$deadline=(Get-Date).AddSeconds(30); $completed=$null
while((Get-Date)-lt $deadline){$runs=Invoke-RestMethod -Uri 'http://localhost:20200/api/v1/admin/lifecycle-runs' -Headers $headers;$completed=$runs.items|Where-Object id -eq $run.id;if($completed.status -eq 'completed'){break};Start-Sleep -Milliseconds 500}
if($completed.status -ne 'completed' -or $completed.processed_count -ne 1){throw 'Artifact lifecycle purge did not complete'}
try { Invoke-RestMethod -Uri "http://localhost:20200/api/v1/artifacts/$($created.id)" -Headers $headers | Out-Null; throw 'Purged artifact metadata remained' } catch { if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw } }
$evidence=@{timestamp=[DateTimeOffset]::UtcNow.ToString('o');artifact_id=$created.id;s3_key=$created.storage_ref;size_bytes=$artifact.size_bytes;sha256=$artifact.content_sha256;preview=$true;signed_download=$true;share_once=$true;soft_delete_restore=$true;lifecycle_purge=$true}
$evidence|ConvertTo-Json -Depth 8|Set-Content -Encoding utf8 quality/evidence/artifact-storage-smoke.json
$evidence|ConvertTo-Json -Depth 8
