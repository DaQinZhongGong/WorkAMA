[CmdletBinding()]
param(
  [string]$Project = "workama"
)

$ErrorActionPreference = "Stop"
if ($Project -notmatch '^workama(?:$|[-_][a-z0-9][a-z0-9_-]*)$') {
  throw "Project must be workama-prefixed."
}

docker run --rm -e PYTHONPATH=/src/apps/cli -v "${PWD}:/src" -w /src python:3.12-slim python -m unittest discover -s apps/cli/tests -v
if ($LASTEXITCODE -ne 0) { throw "CLI tests failed with exit code $LASTEXITCODE" }
Write-Host "WorkAMA CLI tests passed for project '$Project'."
