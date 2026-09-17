@echo off
rem Freight-rate LAN board: start the daemon if needed, wait until the port really
rem answers, then open the page.
rem
rem Why this script used to fail on every click: the interpreter path contains a
rem space ("YQN OS") and %PY% was expanded WITHOUT quotes, so cmd tried to run
rem   C:\Users\YQN\AppData\Local\Programs\YQN
rem and died with "not recognized as an internal or external command". That error
rem went into >nul together with the daemon output, so the only thing left on
rem screen was the probe failure below - which looked like "the board is broken".
setlocal
set "HERE=%~dp0"
set "URL=http://127.0.0.1:8795/"
set "SERVER=%HERE%manual_server.py"
set "LOG=%HERE%manual.log"

rem Preferred interpreter: YQN OS embedded Python 3.11. This machine has no py
rem launcher and PATH "python" is the WindowsApps store stub (exits silently).
set "RUNTIME=C:\Users\YQN\AppData\Local\Programs\YQN OS\runtime\python\python.exe"
if exist "%RUNTIME%" goto have_runtime
where py >nul 2>&1 && (set "RUNTIME=py" & goto have_runtime)
where python >nul 2>&1 && (set "RUNTIME=python" & goto have_runtime)
echo [!] No usable Python found. Expected one of:
echo       C:\Users\YQN\AppData\Local\Programs\YQN OS\runtime\python\python.exe
echo       py  /  python on PATH
pause
exit /b 1

:have_runtime
rem Quotes matter here - keep them, or the space in the path breaks the command.
"%RUNTIME%" "%SERVER%" --daemon >nul 2>&1
rem Exiting non-zero is normal: the daemon refuses to start a second time when
rem port 8795 already answers, which means the board is already up.

rem The detached child needs about 0.5-2s to boot before it binds the port, so a
rem single instant probe is a coin flip even when everything is fine. Retry a bit.
set /a TRIES=0
:probe
curl -s -m 2 -o nul "%URL%" && goto open
set /a TRIES+=1
if %TRIES% geq 8 goto report
rem sleep ~1s (timeout aborts the whole script when stdin is redirected, ping does not care)
ping -n 2 127.0.0.1 >nul
goto probe

:open
start "" "%URL%"
exit /b 0

:report
echo [!] Board did not answer on port 8795 within 8s.
echo       runtime : "%RUNTIME%"
echo       server  : "%SERVER%"
echo       last 15 lines of manual.log:
echo       ------------------------------------------------------------------
powershell -NoProfile -Command "if (Test-Path -LiteralPath '%LOG%') { Get-Content -LiteralPath '%LOG%' -Tail 15 -Encoding UTF8 } else { '(no manual.log yet - the daemon never got as far as writing it)' }"
echo       ------------------------------------------------------------------
echo       Empty tail above means the daemon died before binding. Reproduce it in
echo       the foreground to see the real error:
echo         "%RUNTIME%" "%SERVER%"
echo       (a port clash prints "port 8795 already in use" instead of failing silently)
pause
exit /b 1
