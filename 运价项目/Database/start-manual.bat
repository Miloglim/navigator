@echo off
rem Keep both shortcuts on the same backend-aware startup path.
chcp 65001 >nul
call "%~dp0运价看板-启动并打开.bat" %*
exit /b %errorlevel%
