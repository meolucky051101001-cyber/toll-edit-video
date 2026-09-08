Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envPath = Join-Path $projectRoot 'backend\.env'
$markerPath = Join-Path $projectRoot 'workspace\telegram_v2_setup.json'
$legacyUsername = 'autodub_mibo_bot'

function Set-EnvValue {
    param([string[]]$Lines, [string]$Name, [string]$Value)

    $prefix = $Name + '='
    $updated = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($line in $Lines) {
        if ($line.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $updated.Add($prefix + $Value)
            $found = $true
        }
        else {
            $updated.Add($line)
        }
    }
    if (-not $found) {
        $updated.Add($prefix + $Value)
    }
    return $updated.ToArray()
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Kết nối AutoDub Video Bot V2'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(620, 285)
$form.MinimumSize = $form.Size
$form.MaximizeBox = $false
$form.TopMost = $true

$title = New-Object System.Windows.Forms.Label
$title.Text = 'Cấu hình Telegram Bot riêng cho Pipeline V2'
$title.Font = New-Object System.Drawing.Font('Segoe UI', 14, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(24, 20)
$title.AutoSize = $true
$form.Controls.Add($title)

$help = New-Object System.Windows.Forms.Label
$help.Text = 'Mở BotFather > bot mới > API Token, copy token rồi dán bên dưới. Token chỉ được lưu cục bộ vào backend\.env.'
$help.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$help.Location = New-Object System.Drawing.Point(26, 60)
$help.Size = New-Object System.Drawing.Size(550, 42)
$form.Controls.Add($help)

$tokenLabel = New-Object System.Windows.Forms.Label
$tokenLabel.Text = 'Token bot mới:'
$tokenLabel.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$tokenLabel.Location = New-Object System.Drawing.Point(26, 112)
$tokenLabel.AutoSize = $true
$form.Controls.Add($tokenLabel)

$tokenBox = New-Object System.Windows.Forms.TextBox
$tokenBox.Location = New-Object System.Drawing.Point(26, 138)
$tokenBox.Size = New-Object System.Drawing.Size(550, 28)
$tokenBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$tokenBox.UseSystemPasswordChar = $true
$form.Controls.Add($tokenBox)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = 'Kiểm tra và kết nối V2'
$saveButton.Location = New-Object System.Drawing.Point(350, 187)
$saveButton.Size = New-Object System.Drawing.Size(226, 36)
$saveButton.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = 'Hủy'
$cancelButton.Location = New-Object System.Drawing.Point(242, 187)
$cancelButton.Size = New-Object System.Drawing.Size(96, 36)
$form.Controls.Add($cancelButton)
$cancelButton.Add_Click({ $form.Close() })

$saveButton.Add_Click({
    $token = $tokenBox.Text.Trim()
    if ($token -notmatch '^\d+:[A-Za-z0-9_-]{30,}$') {
        [System.Windows.Forms.MessageBox]::Show(
            'Token không đúng định dạng. Hãy copy lại token từ BotFather.',
            'Token không hợp lệ', 'OK', 'Warning'
        ) | Out-Null
        return
    }

    $saveButton.Enabled = $false
    $saveButton.Text = 'Đang kiểm tra...'
    try {
        $response = Invoke-RestMethod -Uri ('https://api.telegram.org/bot' + $token + '/getMe') -Method Get -TimeoutSec 30
        if (-not $response.ok -or -not $response.result.username) {
            throw 'Telegram did not return a bot identity.'
        }

        $username = [string]$response.result.username
        if ($username.Equals($legacyUsername, [System.StringComparison]::OrdinalIgnoreCase)) {
            [System.Windows.Forms.MessageBox]::Show(
                ('Đây vẫn là token của bot V1 @' + $username + '. Hãy dùng token của bot mới.'),
                'Nhầm token V1', 'OK', 'Error'
            ) | Out-Null
            return
        }

        $choice = [System.Windows.Forms.MessageBox]::Show(
            ('Token thuộc về @' + $username + '. Kết nối bot này với AutoDub V2?'),
            'Xác nhận bot V2', 'YesNo', 'Question'
        )
        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) {
            return
        }

        $lines = if (Test-Path -LiteralPath $envPath) { @(Get-Content -LiteralPath $envPath) } else { @() }
        $lines = Set-EnvValue -Lines $lines -Name 'BOT_TOKEN' -Value $token
        $lines = Set-EnvValue -Lines $lines -Name 'BOT_EXPECTED_USERNAME' -Value $username

        $tempPath = $envPath + '.telegram-v2.tmp'
        [System.IO.File]::WriteAllLines($tempPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tempPath -Destination $envPath -Force

        $markerDirectory = Split-Path -Parent $markerPath
        if (-not (Test-Path -LiteralPath $markerDirectory)) {
            New-Item -ItemType Directory -Path $markerDirectory | Out-Null
        }
        @{
            username = $username
            bot_id = [string]$response.result.id
            configured_at = [DateTime]::UtcNow.ToString('o')
        } | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8

        [System.Windows.Forms.MessageBox]::Show(
            ('Đã lưu an toàn @' + $username + ' cho AutoDub V2. Quay lại Codex để hoàn tất khởi động.'),
            'Kết nối thành công', 'OK', 'Information'
        ) | Out-Null
        $form.Close()
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show(
            'Không xác minh được token với Telegram. Kiểm tra Internet và copy lại token từ BotFather.',
            'Kết nối thất bại', 'OK', 'Error'
        ) | Out-Null
    }
    finally {
        $saveButton.Enabled = $true
        $saveButton.Text = 'Kiểm tra và kết nối V2'
        $token = $null
        $tokenBox.Clear()
    }
})

$form.AcceptButton = $saveButton
$form.CancelButton = $cancelButton
[void]$form.ShowDialog()
