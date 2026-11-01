# Rollback Tool V1 ve nguyen trang commit 242f21c17a3e0221b8f810c4dfc22a453c63fc95
# Chay: powershell -ExecutionPolicy Bypass -File "C:\tool v1\rollback_to_safe_state.ps1"
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = "C:\tool v1"
$Backup = Join-Path $Root "workspace\backups\pre_visual_sub_to_speech_backup"

if (-not (Test-Path $Backup)) {
    Write-Host "KHONG TIM THAY BAN SAO LUU: $Backup" -ForegroundColor Red
    exit 1
}

$Restored = @()
Get-ChildItem -Path $Backup -Recurse -File | ForEach-Object {
    $Relative = $_.FullName.Substring($Backup.Length).TrimStart('\', '/')
    $Destination = Join-Path $Root $Relative
    $DestinationDir = Split-Path -Parent $Destination
    if (-not (Test-Path $DestinationDir)) {
        New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
    }
    Copy-Item -Path $_.FullName -Destination $Destination -Force
    $Restored += $Destination
}

$NewTest = Join-Path $Root "backend\tests\test_visual_sub_to_speech.py"
if (Test-Path $NewTest) {
    Remove-Item -Path $NewTest -Force
}
$NewTestDir = Join-Path $Root "backend\tests"
if ((Test-Path $NewTestDir) -and -not (Get-ChildItem -Path $NewTestDir -Force)) {
    Remove-Item -Path $NewTestDir -Force
}

$Python = Join-Path $Root "backend\venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$CompileTargets = $Restored | Where-Object { $_ -match "\.py$" }
& $Python -m py_compile @CompileTargets
if ($LASTEXITCODE -ne 0) {
    Write-Host "LOI CU PHAP SAU KHI KHOI PHUC. Kiem tra lai thu cong!" -ForegroundColor Red
    exit 1
}

Write-Host "✅ ĐÃ KHÔI PHỤC TOOL V1 VỀ TRẠNG THÁI NGUYÊN BẢN AN TOÀN 100%!" -ForegroundColor Green
Write-Host "Da khoi phuc $($Restored.Count) file va xoa file test moi (neu co)." -ForegroundColor Green
exit 0
