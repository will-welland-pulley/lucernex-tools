# Every few minutes: run a sync if someone added a row to the "Request a sync" Google Sheet.
# Quiet unless a sync actually runs; then it logs like run_sync.ps1 and pops up on failure.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "data\sync.log"
$python = "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe"

$output = & $python -u lxsync.py watch 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
if ($output) {
    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (requested) ====" | Out-File $log -Append -Encoding utf8
    $output | Where-Object { $_ -notmatch '^\s*\[\d+/\d+\]' } | Out-File $log -Append -Encoding utf8
}
if ($code -ne 0) {
    $reason = if ($output -match "session expired") {
        "Lucernex session expired. Open a terminal in lucernex-tools and run: python lxsync.py login"
    } else {
        "Requested sync failed. See data\sync.log"
    }
    msg $env:USERNAME /TIME:0 "Walmart Lucernex Tracker: $reason"
}
exit $code
