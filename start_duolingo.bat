@echo off
setlocal

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

set PROFILE=%~dp0chrome-duolingo-profile
set PORT=9223

echo Launching Chrome on port %PORT% with profile %PROFILE%...
start "" %CHROME% --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" https://www.duolingo.com/super

echo Waiting for Chrome to be ready...
ping 127.0.0.1 -n 5 >nul

echo Starting duolingo.py...
cd /d %~dp0
set DUOLINGO_CDP_PORT=%PORT%
set DUOLINGO_CDP_URL=http://localhost:%PORT%
python duolingo.py

endlocal
pause
