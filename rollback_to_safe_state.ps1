# [RETIRED] Historical source rollback script.
# Does not copy files, delete tests, or modify the working tree.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

[Console]::Error.WriteLine('[RETIRED] rollback_to_safe_state.ps1 has been retired because it copied a dated backup over source files and deleted tests.')
[Console]::Error.WriteLine('Use git revert on the specific commit or scripts\rollback_py310.bat for Python runtime environment rollback.')
exit 1
