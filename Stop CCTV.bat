@echo off
title Stop OldPhone CCTV
echo Stopping every running copy of OldPhone CCTV...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'cctv\.py' -and $_.ProcessId -ne $PID };" ^
  "if (-not $p) { Write-Host '  OldPhone CCTV was not running.' } else { $p | ForEach-Object { Write-Host ('  stopped ' + $_.Name + ' ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } };" ^
  "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | Where-Object { $_.ExecutablePath -like '*OldPhone CCTV*' -or $_.CommandLine -match 'pipe:0' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo.
echo Done. Start it again with "Start CCTV.bat".
timeout /t 5 >nul
