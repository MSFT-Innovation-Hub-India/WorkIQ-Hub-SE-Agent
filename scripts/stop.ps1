# Stop WorkIQ Assistant
$stopped = $false
Get-Process pythonw -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force
    $stopped = $true
}
if ($stopped) {
    Write-Host "WorkIQ Assistant stopped." -ForegroundColor Yellow
} else {
    Write-Host "WorkIQ Assistant is not running." -ForegroundColor Gray
}
