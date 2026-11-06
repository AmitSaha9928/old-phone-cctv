@echo off
title OldPhone CCTV
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
rem open the viewer on whatever port config.json says (default 8090)
for /f %%p in ('%PY% -c "import json;print(json.load(open('config.json',encoding='utf-8-sig')).get('http_port',8090))" 2^>nul') do set PORT=%%p
if "%PORT%"=="" set PORT=8090
start "" http://localhost:%PORT%
%PY% server\cctv.py
pause
