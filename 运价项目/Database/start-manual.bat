@echo off
rem Start the LAN freight-manual board and open the local page.
rem Port 8795 (8790 is the heartbeat board, do not mix). Duplicate clicks
rem are safe: the server probes the port first and exits if it is answered.
cd /d "%~dp0"

rem Three-tier fallback. The YQN OS bundled Python 3.11 is the only reliable
rem interpreter on this machine (system "python" is the WindowsApps store stub
rem that silently exits on double-click, and there is no py launcher installed).
set "PY_YQN=C:\Users\YQN\AppData\Local\Programs\YQN OS\runtime\python\python.exe"

if exist "%PY_YQN%" goto use_yqn
echo [start] YQN OS runtime python.exe not found, trying py launcher...
set "RUN=py -3"
%RUN% manual_server.py --daemon
if errorlevel 1 goto try_path
goto started

:try_path
python manual_server.py --daemon
if errorlevel 1 goto fail
goto started

:use_yqn
"%PY_YQN%" manual_server.py --daemon
if errorlevel 1 goto fail

:started
timeout /t 2 /nobreak >nul
echo [start] board: http://127.0.0.1:8795/  (LAN peers use this machine IP on port 8795)
start "" http://127.0.0.1:8795/
exit /b 0

:fail
echo [start] Failed. Common causes:
echo   1. Port 8795 already answered - service may already be running.
echo      Check netstat: netstat -ano ^| findstr :8795
echo   2. No usable Python 3 interpreter. Expected: %PY_YQN%
echo      or py -3 or python on PATH. See manual.log for the real error.
pause
exit /b 1
