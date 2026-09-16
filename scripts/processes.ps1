function Get-ProjectRoot { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) }
function Stop-RecordedProcess($Record, [string]$ProjectRoot) {
    if (-not $Record -or -not $Record.id) { return $true }
    $process = Get-Process -Id $Record.id -ErrorAction SilentlyContinue
    if (-not $process) { return $true }
    # PID reuse cannot authorize stopping an unrelated process.
    if ($Record.startTicks) {
        $recordedTicks = [long]$Record.startTicks
    } elseif ($Record.started -is [datetime]) {
        $recordedTicks = $Record.started.ToUniversalTime().Ticks
    } else {
        $recordedTicks = [DateTimeOffset]::Parse([string]$Record.started, [Globalization.CultureInfo]::InvariantCulture).UtcTicks
    }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedTicks) {
        Write-Warning "PID $($Record.id) has been reused. It will not be stopped."
        return $false
    }
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.id)"
    if (-not $info.CommandLine -or -not $info.CommandLine.Contains($ProjectRoot)) {
        Write-Warning "PID $($Record.id) does not match this project. It will not be stopped."
        return $false
    }
    function Stop-ChildTree([int]$ParentId) {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId")
        foreach ($child in $children) {
            $liveChild = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if ($liveChild -and $liveChild.StartTime -ge $process.StartTime) {
                Stop-ChildTree $child.ProcessId
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
    }
    Stop-ChildTree $Record.id
    Stop-Process -Id $Record.id -ErrorAction SilentlyContinue
    Wait-Process -Id $Record.id -Timeout 5 -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $process.Refresh()
        if ($process.HasExited) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}
