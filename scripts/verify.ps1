$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$runnerPath = Join-Path $PSScriptRoot 'verify.py'

& uv --directory $repositoryRoot run --locked python $runnerPath @args
exit $LASTEXITCODE
