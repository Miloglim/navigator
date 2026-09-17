@echo off
rem 停止「人工维护台账（局域网版）」：按命令行特征清掉所有 manual_server 进程，
rem 不只杀 pid 文件里那一个（重复启动过就会留下多份）。
cd /d %~dp0
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'manual_server' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('stopped pid=' + $_.ProcessId) }"
if exist manual.pid del manual.pid
echo [stop] done
