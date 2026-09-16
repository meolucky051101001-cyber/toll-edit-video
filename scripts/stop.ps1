$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'processes.ps1')
$projectRoot = Get-ProjectRoot
$stateFile = Join-Path $projectRoot 'data\processes.json'
try {
    if (-not (Test-Path -LiteralPath $stateFile)) { Write-Host 'No recorded project processes.'; exit 0 }
    $records = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $remaining = @()
    foreach ($record in $records) {
        if (-not (Stop-RecordedProcess $record $projectRoot)) { $remaining += $record }
    }
    ConvertTo-Json -InputObject @($remaining) -Depth 3 | Set-Content -LiteralPath $stateFile -Encoding UTF8
    if ($remaining.Count) { throw 'Some recorded processes could not be verified/stopped; records were preserved for diagnosis.' }
    Write-Host 'Project processes stopped. Other Python/Node programs were not touched.'
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
