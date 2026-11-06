# Builds dist\OldPhone-CCTV-<version>-windows.zip: server, viewer, phone app, scripts, FFmpeg and adb.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File dev\make_release.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$version = (& python "$root\server\cctv.py" --version).Trim()
$name = "OldPhone-CCTV-$version-windows"
$out = Join-Path $root "dist\$name"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force "$out\tools\platform-tools", "$out\phone\modes", "$out\server", "$out\web" | Out-Null

Copy-Item "$root\server\*.py" "$out\server"
Copy-Item "$root\web\*" "$out\web" -Recurse
# Phone scripts must have LF line endings or Android's shell rejects them
foreach ($sh in Get-ChildItem "$root\phone\modes\*.sh") {
    $t = [IO.File]::ReadAllText($sh.FullName) -replace "`r`n", "`n"
    [IO.File]::WriteAllText("$out\phone\modes\$($sh.Name)", $t, (New-Object Text.UTF8Encoding $false))
}
$apk = "$root\phone\OldPhoneCCTV.apk"
if (-not (Test-Path $apk)) { throw "Build the phone app first: android\build.ps1" }
Copy-Item $apk "$out\phone"
Copy-Item "$root\README.md", "$root\LICENSE" $out

# Windows scripts with CRLF line endings
foreach ($f in 'setup.ps1', 'Start CCTV.bat', 'Stop CCTV.bat') {
    $t = [IO.File]::ReadAllText("$root\$f") -replace "`r?`n", "`r`n"
    [IO.File]::WriteAllText("$out\$f", $t, (New-Object Text.UTF8Encoding $false))
}

# Bundled tools: FFmpeg (essentials build) and adb from the local machine
$ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ff) { $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue | ForEach-Object { Get-Item $_.Source } }
if (-not $ff) { throw 'ffmpeg.exe not found on this machine' }
Copy-Item $ff.FullName "$out\tools"
$lic = Get-ChildItem (Split-Path $ff.DirectoryName -Parent) -Filter LICENSE* -ErrorAction SilentlyContinue | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName "$out\tools\FFMPEG-LICENSE.txt" }
$pt = "$env:LOCALAPPDATA\Android\Sdk\platform-tools"
Copy-Item "$pt\adb.exe", "$pt\AdbWinApi.dll", "$pt\AdbWinUsbApi.dll" "$out\tools\platform-tools"
if (Test-Path "$pt\NOTICE.txt") { Copy-Item "$pt\NOTICE.txt" "$out\tools\platform-tools" }

# Never ship private files
Get-ChildItem $out -Recurse -Force -Include config.json, *.keystore, *.jks, events.db, __pycache__ | Remove-Item -Recurse -Force

$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $out -DestinationPath $zip
Write-Host ("OK -> {0} ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
