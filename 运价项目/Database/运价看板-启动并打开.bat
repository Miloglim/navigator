@echo off
setlocal
set "HERE=%~dp0"
set "LOG=%HERE%manual-startup.log"
set "RUNTIME=%HERE%.runtime\python\python.exe"
if not exist "%RUNTIME%" goto try_venv
"%RUNTIME%" -c "import psycopg" >nul 2>&1
if not errorlevel 1 goto launch

:try_venv
set "RUNTIME=%HERE%.venv\Scripts\python.exe"
if not exist "%RUNTIME%" goto try_codex
"%RUNTIME%" -c "import psycopg" >nul 2>&1
if not errorlevel 1 goto launch

:try_codex
set "RUNTIME=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%RUNTIME%" goto try_yqn
"%RUNTIME%" -c "import psycopg" >nul 2>&1
if not errorlevel 1 goto launch

:try_yqn
set "RUNTIME=%LOCALAPPDATA%\Programs\YQN OS\runtime\python\python.exe"
if not exist "%RUNTIME%" goto no_runtime
"%RUNTIME%" -c "import psycopg" >nul 2>&1
if errorlevel 1 goto no_runtime

:launch
rem Read runtime.json and verify the configured DB backend through the watchdog.
rem Log startup failures instead of hiding them behind an old log tail.
"%RUNTIME%" "%HERE%ops\watchdog.py" >>"%LOG%" 2>&1
if errorlevel 1 goto failed
echo [start] Board ready: http://127.0.0.1:8795/
if /i "%~1"=="--no-open" exit /b 0
start "" "http://127.0.0.1:8795/"
exit /b 0

:no_runtime
echo [!] No available Python runtime can import the PostgreSQL driver psycopg.
echo     Checked Database\.runtime\python, Database\.venv, Codex Python, and YQN OS runtime.
echo     Restore Database\.runtime\python from backup before starting the board.
goto fail_exit

:failed
echo [!] Board startup failed. Current startup errors are in "%LOG%".
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 20 -Encoding UTF8"

:fail_exit
if /i not "%~1"=="--no-open" pause
exit /b 1
