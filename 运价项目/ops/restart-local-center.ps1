param()

$ErrorActionPreference = 'Stop'
$CenterRoot = 'D:\Agent Basement\Milogin-s-prospector'
$CenterConfig = 'C:\ProgramData\MiloginCenter\postgres.json'
$Node = (Get-Command node.exe -ErrorAction Stop).Source

if (!(Test-Path -LiteralPath $CenterConfig)) { throw 'Formal center database config is missing.' }
$config = Get-Content -LiteralPath $CenterConfig -Raw | ConvertFrom-Json
if (!$config.appPassword -or $config.port -ne 55846 -or $config.database -ne 'milogin_center') {
    throw 'Formal center database config is incomplete or targets the wrong database.'
}

$env:PROSPECTOR_CENTER_MODE = 'production'
$env:PROSPECTOR_CENTER_PG_DSN = "postgresql://milogin_app:$($config.appPassword)@127.0.0.1:55846/milogin_center"
$env:PROSPECTOR_CENTER_PUBLIC_ORIGIN = 'http://127.0.0.1:55847'
$env:PROSPECTOR_CENTER_LOCAL_HTTP = '1'

Push-Location $CenterRoot
try {
    & $Node 'scripts\production-server.mjs' stop
    if ($LASTEXITCODE -ne 0) {
        try {
            $live = Invoke-RestMethod -Uri 'http://127.0.0.1:55847/health/live' -TimeoutSec 5
        } catch {
            $live = $null  # A stopped center is the normal cold-start path.
        }
        if ($live) {
            $pidToStop = [int]$live.data.pid
            $process = Get-Process -Id $pidToStop -ErrorAction Stop
            if ($process.ProcessName -notin @('node', 'node.exe')) { throw 'Healthy center PID is not a Node process; refusing to stop it.' }
            Stop-Process -Id $pidToStop -Force
            $deadline = (Get-Date).AddSeconds(10)
            while ((Get-Date) -lt $deadline) {
                try { Invoke-RestMethod -Uri 'http://127.0.0.1:55847/health/live' -TimeoutSec 1 | Out-Null; Start-Sleep -Milliseconds 250 }
                catch { break }
            }
        }
    }
    & $Node 'scripts\production-server.mjs' start
    if ($LASTEXITCODE -ne 0) { throw 'Formal center start failed.' }
} finally {
    Pop-Location
}

Start-Process 'http://127.0.0.1:55847/'
