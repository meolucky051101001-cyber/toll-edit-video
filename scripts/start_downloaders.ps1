param([switch]$NoWait)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'processes.ps1')
$projectRoot = Get-ProjectRoot
Set-Location -LiteralPath $projectRoot

$stateFile = Join-Path $projectRoot 'data\downloader_processes.json'
$startedProcesses = @()

function Test-DownloaderEndpoint([int]$port, [string]$expectedPattern) {
    try {
        $url = "http://127.0.0.1:$port/openapi.json"
        $resp = Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 2 -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
        if ($resp.StatusCode -eq 200 -and $resp.Content -match $expectedPattern) {
            return $true
        }
    } catch {}
    return $false
}

function Test-PortOpen([int]$port) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
        $wait = $iar.AsyncWaitHandle.WaitOne(500, $false)
        if ($wait) {
            $tcp.EndConnect($iar)
            $tcp.Close()
            return $true
        }
        $tcp.Close()
        return $false
    } catch {
        return $false
    }
}

try {
    foreach ($folder in @('data', 'logs')) {
        New-Item -ItemType Directory -Path (Join-Path $projectRoot $folder) -Force | Out-Null
    }

    $xhsDir = Join-Path $projectRoot 'scratch\XHS-Downloader'
    $douyinDir = Join-Path $projectRoot 'scratch\Douyin_TikTok_Download_API'

    $xhsPython = Join-Path $xhsDir '.venv\Scripts\python.exe'
    $douyinPython = Join-Path $douyinDir '.venv\Scripts\python.exe'

    $xhsLauncher = Join-Path $projectRoot 'scripts\launchers\launch_xhs.py'
    $douyinLauncher = Join-Path $projectRoot 'scripts\launchers\launch_douyin.py'

    # 1. Check & Start XHS-Downloader (port 5556)
    $xhsPortOpen = Test-PortOpen 5556
    if (-not $xhsPortOpen) {
        if (-not (Test-Path -LiteralPath $xhsPython)) {
            throw "Missing virtualenv for XHS-Downloader at '$xhsPython'. Please run scripts\setup_downloaders.ps1 first."
        }
        Write-Host "Starting XHS-Downloader on 127.0.0.1:5556..." -ForegroundColor Cyan
        $xhsProc = Start-Process -FilePath $xhsPython -ArgumentList @("`"$xhsLauncher`"") `
            -WorkingDirectory $xhsDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $projectRoot 'logs\xhs-downloader.log') `
            -RedirectStandardError (Join-Path $projectRoot 'logs\xhs-downloader-error.log')

        $startedProcesses += @{
            id = $xhsProc.Id
            started = $xhsProc.StartTime.ToUniversalTime().ToString('o')
            startTicks = [string]$xhsProc.StartTime.ToUniversalTime().Ticks
            service = 'xhs-downloader'
            port = 5556
            proc = $xhsProc
            pattern = 'XHS-Downloader|/xhs/detail'
        }
    } else {
        # Verify existing service on port 5556
        if (Test-DownloaderEndpoint 5556 'XHS-Downloader|/xhs/detail') {
            Write-Host "Port 5556 is already active and verified as XHS-Downloader." -ForegroundColor Green
        } else {
            throw "Port 5556 is occupied by an unrecognized application or failed XHS identity check."
        }
    }

    # 2. Check & Start Douyin_TikTok_Download_API (port 5555)
    $douyinPortOpen = Test-PortOpen 5555
    if (-not $douyinPortOpen) {
        if (-not (Test-Path -LiteralPath $douyinPython)) {
            throw "Missing virtualenv for Douyin_TikTok_Download_API at '$douyinPython'. Please run scripts\setup_downloaders.ps1 first."
        }
        Write-Host "Starting Douyin_TikTok_Download_API on 127.0.0.1:5555..." -ForegroundColor Cyan
        $douyinProc = Start-Process -FilePath $douyinPython -ArgumentList @("`"$douyinLauncher`"") `
            -WorkingDirectory $douyinDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $projectRoot 'logs\douyin-downloader.log') `
            -RedirectStandardError (Join-Path $projectRoot 'logs\douyin-downloader-error.log')

        $startedProcesses += @{
            id = $douyinProc.Id
            started = $douyinProc.StartTime.ToUniversalTime().ToString('o')
            startTicks = [string]$douyinProc.StartTime.ToUniversalTime().Ticks
            service = 'douyin-downloader'
            port = 5555
            proc = $douyinProc
            pattern = '/api/hybrid/video_data|/hybrid/video_data'
        }
    } else {
        # Verify existing service on port 5555
        if (Test-DownloaderEndpoint 5555 '/api/hybrid/video_data|/hybrid/video_data') {
            Write-Host "Port 5555 is already active and verified as Douyin_TikTok_Download_API." -ForegroundColor Green
        } else {
            throw "Port 5555 is occupied by an unrecognized application or failed Douyin identity check."
        }
    }

    # 3. Health & readiness validation with independent 20-second timeout per process
    if (-not $NoWait -and $startedProcesses.Count -gt 0) {
        Write-Host "Verifying health and readiness of started services via local openapi..." -ForegroundColor Gray

        foreach ($p in $startedProcesses) {
            $serviceName = $p.service
            $port = $p.port
            $procObj = $p.proc
            $expectedPattern = $p.pattern
            $healthUrl = "http://127.0.0.1:$port/openapi.json"

            $timeoutSeconds = 20
            $deadline = (Get-Date).AddSeconds($timeoutSeconds)
            $isReady = $false

            while ((Get-Date) -lt $deadline) {
                if ($procObj.HasExited) {
                    $errLogPath = Join-Path $projectRoot "logs\$serviceName-error.log"
                    $errContent = if (Test-Path -LiteralPath $errLogPath) { Get-Content -LiteralPath $errLogPath -Tail 20 } else { '' }
                    throw "Process $serviceName (PID $($p.id)) exited prematurely with exit code $($procObj.ExitCode): $errContent"
                }

                try {
                    $resp = Invoke-WebRequest -Uri $healthUrl -Method Get -TimeoutSec 1 -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
                    if ($resp.StatusCode -eq 200 -and $resp.Content -match $expectedPattern) {
                        $isReady = $true
                        Write-Host "Service $serviceName on port $port is ready (HTTP $($resp.StatusCode), schema verified)." -ForegroundColor Green
                        break
                    }
                } catch {
                    Start-Sleep -Milliseconds 500
                }
            }

            if (-not $isReady) {
                throw "Timeout waiting for service $serviceName on port $port to respond at $healthUrl within $timeoutSeconds seconds."
            }
        }
    }

    if ($startedProcesses.Count -gt 0) {
        $recordsToSave = @()
        foreach ($p in $startedProcesses) {
            $recordsToSave += @{
                id = $p.id
                started = $p.started
                startTicks = $p.startTicks
                service = $p.service
                port = $p.port
            }
        }

        $existing = @()
        if (Test-Path -LiteralPath $stateFile) {
            try {
                $raw = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
                foreach ($item in $raw) {
                    if ($item.id -and $item.port) {
                        $existing += @{
                            id = $item.id
                            started = $item.started
                            startTicks = $item.startTicks
                            service = $item.service
                            port = $item.port
                        }
                    }
                }
            } catch {}
        }
        $combined = @($existing) + @($recordsToSave)
        ConvertTo-Json -InputObject @($combined) -Depth 3 | Set-Content -LiteralPath $stateFile -Encoding UTF8
    }

    Write-Host "Successfully verified and started all local video download services." -ForegroundColor Green
} catch {
    foreach ($record in $startedProcesses) {
        Stop-RecordedProcess $record $projectRoot | Out-Null
    }
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
