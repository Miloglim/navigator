@echo off
setlocal
set "HERE=%~dp0"
set "RUNTIME=%HERE%.runtime\python\python.exe"
set "LOG=%HERE%manual-startup.log"
if not exist "%RUNTIME%" goto no_runtime
"%RUNTIME%" -c "import psycopg" >nul 2>&1
if errorlevel 1 goto no_driver
"%RUNTIME%" "%HERE%ops\watchdog.py" --force >>"%LOG%" 2>&1
if errorlevel 1 goto failed
echo [restart] Board ready with project runtime: http://127.0.0.1:8795/
start "" "http://127.0.0.1:8795/"
exit /b 0

:no_runtime
echo [!] Project runtime missing: "%RUNTIME%"
goto fail_exit

:no_driver
echo [!] Project runtime cannot import psycopg.
goto fail_exit

:failed
echo [!] Restart failed. Current startup errors are in "%LOG%".
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 20 -Encoding UTF8"

:fail_exit
pause
exit /b 1
