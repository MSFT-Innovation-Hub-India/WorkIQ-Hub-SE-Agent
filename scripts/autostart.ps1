# Add/remove WorkIQ Assistant from Windows auto-start
# Usage:  .\autostart.ps1 install     — start at login
#         .\autostart.ps1 uninstall   — remove from login

param(
    [Parameter(Position=0)]
    [ValidateSet("install", "uninstall")]
    [string]$Action = "install"
)

$startup = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
$vbsPath = Join-Path $startup "WorkIQAssistant.vbs"
$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonw = Join-Path $projectDir ".venv\Scripts\pythonw.exe"
$agent = Join-Path $projectDir "meeting_agent.py"

if ($Action -eq "install") {
    if (-not (Test-Path $pythonw)) {
        Write-Host "pythonw.exe not found at $pythonw" -ForegroundColor Red
        exit 1
    }

    $vbs = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "$projectDir"
WshShell.Run """$pythonw"" ""$agent""", 0, False
"@
    Set-Content -Path $vbsPath -Value $vbs -Encoding UTF8
    Write-Host "Auto-start installed: $vbsPath" -ForegroundColor Green
    Write-Host "WorkIQ Assistant will start automatically at login." -ForegroundColor Green
}
elseif ($Action -eq "uninstall") {
    if (Test-Path $vbsPath) {
        Remove-Item $vbsPath
        Write-Host "Auto-start removed." -ForegroundColor Yellow
    } else {
        Write-Host "Auto-start entry not found." -ForegroundColor Gray
    }
}
