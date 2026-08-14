$ErrorActionPreference = 'Stop'
$compose = @('--env-file', '.env', '-f', 'deploy/compose/docker-compose.yml')
docker compose @compose run --rm -v "${PWD}:/src" -e WORKAMA_API_BASE_URL=http://platform-api:8000 platform-api python /src/tools/saml-acs-smoke.py
if ($LASTEXITCODE -ne 0) { throw "SAML ACS smoke failed with exit code $LASTEXITCODE" }
