param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$baselinePython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $baselinePython)) {
    throw 'Missing .venv. Follow README setup instructions first.'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $baselinePython -X utf8 -m research_baseline @Arguments
    $baselineExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $baselineExitCode
