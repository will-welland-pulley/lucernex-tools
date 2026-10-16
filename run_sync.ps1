# Scheduled entry point: runs the sync, keeps a log, and pops up a notice if it fails
# (most often because the Lucernex session expired and needs `python lxsync.py login`).
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "data\sync.log"
$python = "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe"

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
$output = & $python -u lxsync.py sync 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
# Drop the per-project progress lines; keep the summary and changes.
$output | Where-Object { $_ -notmatch '^\s*\[\d+/\d+\]' } | Out-File $log -Append -Encoding utf8

if ($code -ne 0) {
    $reason = if ($output -match "session expired") {
        "Lucernex session expired. Open a terminal in lucernex-tools and run: python lxsync.py login"
    } else {
        "Lucernex sync failed. See data\sync.log"
    }
    msg $env:USERNAME /TIME:0 "Walmart Lucernex Tracker: $reason"
}
exit $code
