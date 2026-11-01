# [RETIRED] Historical Pipeline V2 Telegram token setup script.
# Retired so it can never overwrite Tool V1's BOT_TOKEN or BOT_EXPECTED_USERNAME in backend\.env.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

[Console]::Error.WriteLine('[RETIRED] scripts/setup_telegram_bot_v2.ps1 has been retired to prevent overwriting Tool V1 BOT_TOKEN in backend\.env.')
[Console]::Error.WriteLine('Configure Tool V1 credentials directly in backend\.env when services are stopped.')
exit 1
