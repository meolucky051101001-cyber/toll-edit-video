$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'processes.ps1')
$projectRoot = Get-ProjectRoot
$stateFile = Join-Path $projectRoot 'data\downloader_processes.json'

try {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        Write-Host "No recorded downloader processes are currently running."
        exit 0
    }

    $raw = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $records = @()
    foreach ($item in $raw) {
        if ($item.id) {
            $records += $item
        } elseif ($item.value) {
            foreach ($sub in $item.value) {
                if ($sub.id) { $records += $sub }
            }
        }
    }
    $remaining = @()

    foreach ($record in $records) {
        if (-not (Stop-RecordedProcess $record $projectRoot)) {
            $remaining += $record
        }
    }

    ConvertTo-Json -InputObject @($remaining) -Depth 3 | Set-Content -LiteralPath $stateFile -Encoding UTF8

    if ($remaining.Count -gt 0) {
        throw "Some downloader processes could not be stopped or verified; records preserved."
    }

    Write-Host "Successfully stopped all local downloader services." -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
