param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'processes.ps1')
$projectRoot = Get-ProjectRoot
Set-Location -LiteralPath $projectRoot
$stateFile = Join-Path $projectRoot 'data\processes.json'
$startedProcesses = @()
try {
    $pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $nextScript = Join-Path $projectRoot 'frontend\node_modules\next\dist\bin\next'
    if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $nextScript) -or -not (Test-Path -LiteralPath 'frontend\.next\BUILD_ID')) {
        throw 'Setup is missing. Run scripts\setup_windows.bat first.'
    }
    $backendListening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    $frontendListening = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
    if ($backendListening -and $frontendListening) {
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 2 -ErrorAction Stop
            $page = Invoke-WebRequest 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($health.app -eq 'ai-video-research-tool' -and $page.StatusCode -eq 200) {
                Write-Host 'Video Research is already running: http://localhost:3000' -ForegroundColor Green
                if (-not $NoBrowser) {
                    if (Test-Path 'C:\Program Files\Google\Chrome\Application\chrome.exe') {
                        Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList '--app="http://localhost:3000"'
                    } else {
                        Start-Process 'http://localhost:3000'
                    }
                }
                exit 0
            }
        } catch {}
    }

    foreach ($port in @(3000, 8000)) {
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($listener) { throw "Port $port is already in use. If this tool is running, open http://localhost:3000 or run stop.bat. No other process was stopped." }
    }

    # Start video download sidecars (Douyin & XHS) if present
    $startDownloadersScript = Join-Path $PSScriptRoot 'start_downloaders.ps1'
    if (Test-Path -LiteralPath $startDownloadersScript) {
        try {
            Write-Host 'Checking and starting video downloader services (Douyin & XHS)...' -ForegroundColor Cyan
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startDownloadersScript -NoWait
        } catch {
            Write-Warning "Downloader services could not be auto-started: $_"
        }
    }
    foreach ($folder in @('data', 'logs')) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $backend = Start-Process -FilePath $pythonExe -ArgumentList @('-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8000') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'logs\backend.log') -RedirectStandardError (Join-Path $projectRoot 'logs\backend-error.log')
    $startedProcesses += @{ id = $backend.Id; started = $backend.StartTime.ToUniversalTime().ToString('o'); startTicks = [string]$backend.StartTime.ToUniversalTime().Ticks; service = 'backend' }
    $startedProcesses | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $stateFile -Encoding UTF8
    $nodeExe = (Get-Command node.exe).Source
    $frontend = Start-Process -FilePath $nodeExe -ArgumentList @(('"' + $nextScript + '"'),'start','--hostname','127.0.0.1','--port','3000') -WorkingDirectory (Join-Path $projectRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'logs\frontend.log') -RedirectStandardError (Join-Path $projectRoot 'logs\frontend-error.log')
    $startedProcesses += @{ id = $frontend.Id; started = $frontend.StartTime.ToUniversalTime().ToString('o'); startTicks = [string]$frontend.StartTime.ToUniversalTime().Ticks; service = 'frontend' }
    $startedProcesses | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $stateFile -Encoding UTF8
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($backend.HasExited -or $frontend.HasExited) { throw 'A server stopped during startup. See logs\backend-error.log and logs\frontend-error.log.' }
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 2
            $page = Invoke-WebRequest 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 2
            if ($health.app -eq 'ai-video-research-tool' -and $page.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw 'Startup timed out. See the logs folder.' }
    Write-Host 'Video Research is running: http://localhost:3000' -ForegroundColor Green
    if (-not $NoBrowser) {
        if (Test-Path 'C:\Program Files\Google\Chrome\Application\chrome.exe') {
            Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList '--app="http://localhost:3000"'
        } else {
            Start-Process 'http://localhost:3000'
        }
    }
} catch {
    foreach ($record in $startedProcesses) { Stop-RecordedProcess $record $projectRoot | Out-Null }
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
