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
    Warn 'No phone on adb. Plug it in by USB (USB debugging on) and run:  py -3 server\cctv.py install-phone'
    Warn 'The app is already set up if you moved this folder from the PC that installed it.'
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
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'OldPhone CCTV.lnk'
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
