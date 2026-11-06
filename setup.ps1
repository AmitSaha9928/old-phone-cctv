# OldPhone CCTV setup - run once on the PC that will keep the recordings.
#   Right-click > Run with PowerShell   (or: powershell -ExecutionPolicy Bypass -File setup.ps1)
# Everything stays inside this folder except: Python (installed with winget only if missing)
# and an optional "start with Windows" shortcut in your Startup folder.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   !!  $t" -ForegroundColor Yellow }

# 0. Upgrade from the HomeCam prototype, if it is installed on this PC
$startupDir = [Environment]::GetFolderPath('Startup')
$oldLnk = Join-Path $startupDir 'HomeCam.lnk'
if (Test-Path $oldLnk) {
    Step 'Upgrading from HomeCam'
    $oldDir = (New-Object -ComObject WScript.Shell).CreateShortcut($oldLnk).WorkingDirectory
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'cctv\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Ok 'Stopped the old HomeCam server'
    $oldCfg = Join-Path $oldDir 'config.json'
    if ($oldDir -and (Test-Path $oldCfg) -and -not (Test-Path "$root\config.json")) {
        $c = Get-Content $oldCfg -Raw | ConvertFrom-Json
        # keep the old footage where it is (a relative folder was relative to the old install)
        if ($c.recordings_dir -and -not [IO.Path]::IsPathRooted($c.recordings_dir)) {
            $c.recordings_dir = Join-Path $oldDir $c.recordings_dir
        }
        $json = $c | ConvertTo-Json -Depth 6
        [IO.File]::WriteAllText("$root\config.json", $json, (New-Object Text.UTF8Encoding $false))
        Ok "Kept your settings (port $($c.http_port), recordings in $($c.recordings_dir))"
    }
    Remove-Item $oldLnk -Force
    Ok 'Removed the old HomeCam auto-start (its folder is untouched; delete it once you are happy)'
}

# 1. Python
Step 'Python'
$py = $null
foreach ($c in @('py -3', 'python')) {
    $exe, $arg = $c.Split(' ', 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $v = & $exe $arg -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]'3.9') { $py = $c; Ok "Python $v ($c)"; break }
    }
}
if (-not $py) {
    Warn 'Python 3.9+ not found - installing Python 3.13 with winget'
    winget install --id Python.Python.3.13 -e --silent --accept-package-agreements --accept-source-agreements
    $py = 'py -3'
    Warn 'If the next steps fail, close this window and run setup.ps1 again (PATH needs a refresh).'
}
$pyExe, $pyArg = $py.Split(' ', 2)

# 2. FFmpeg + adb (bundled in tools\)
Step 'Tools'
if (Test-Path "$root\tools\ffmpeg.exe") { Ok 'FFmpeg (bundled)' }
elseif (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Ok 'FFmpeg (installed)' }
else { Warn 'FFmpeg missing - installing with winget'; winget install --id Gyan.FFmpeg.Essentials -e --silent --accept-package-agreements --accept-source-agreements }
if (Test-Path "$root\tools\platform-tools\adb.exe") { Ok 'adb (bundled)' } else { Warn 'adb missing from tools\platform-tools' }
$adb = "$root\tools\platform-tools\adb.exe"

# 3. Phone app
Step 'Phone'
$devices = & $adb devices | Select-String "`tdevice$"
if ($devices) {
    Ok "Phone found: $($devices.Line.Split("`t")[0])"
    & $pyExe $pyArg "$root\server\cctv.py" install-phone
} else {
    Ok 'No phone on USB - that is fine if the OldPhone CCTV app is already on your phones.'
    Ok 'A phone that is not paired yet pairs with this PC automatically when it connects.'
}

# 4. Tailscale (HTTPS address for your phone - needed for live video + talking off this PC)
Step 'Tailscale'
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts -and (Test-Path 'C:\Program Files\Tailscale\tailscale.exe')) { $ts = 'C:\Program Files\Tailscale\tailscale.exe' }
$port = 8090
if (Test-Path "$root\config.json") {
    try { $p = (Get-Content "$root\config.json" -Raw | ConvertFrom-Json).http_port; if ($p) { $port = $p } } catch {}
}
if ($ts) {
    & $ts serve --bg $port
    $dns = (& $ts status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
    if ($dns) { Ok "Open on your phone (Tailscale on):  https://$dns" }
    Warn 'If serve asked you to enable HTTPS, follow its link once, then run setup.ps1 again.'
} else {
    Warn 'Tailscale not installed on this PC. Install it from tailscale.com, sign in, run setup.ps1 again.'
}

# 5. Start with Windows
Step 'Autostart'
$lnk = Join-Path $startupDir 'OldPhone CCTV.lnk'
$ans = Read-Host '   Start OldPhone CCTV automatically when you log in? [Y/n]'
if ($ans -notmatch '^[nN]') {
    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $pyw) { $pyw = (Get-Command pyw -ErrorAction SilentlyContinue).Source }
    if (-not $pyw) { $pyw = (Get-Command $pyExe).Source }  # visible console fallback
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sh.TargetPath = $pyw
    $sh.Arguments = "`"$root\server\cctv.py`""
    $sh.WorkingDirectory = $root
    $sh.Save()
    Ok "Added to Startup ($lnk)"
} else { Ok 'Skipped' }

Step 'Done'
Write-Host "   Start now:  double-click `"Start CCTV.bat`", then open http://localhost:$port" -ForegroundColor Green
if ($dns) { Write-Host "   From your phone (Tailscale on):  https://$dns" -ForegroundColor Green }
Write-Host ''
Read-Host '   Press Enter to close'
