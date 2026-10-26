param(
    [string[]]$TaskNames = @('运价心跳 上午 A', '运价心跳 上午 B', '运价心跳 下午 A', '运价心跳 下午 B'),
    [string]$StatePath = (Join-Path $PSScriptRoot 'state\recovery-state.json')
)
$ErrorActionPreference = 'Stop'
$InterruptedExitCode = [uint32]3221225786 # 0xC000013A
$MinimumAgeMinutes = 2
$MaximumAgeMinutes = 30
$MaximumRetriesPerShardHour = 2
$now = Get-Date
$state = @{ attempts = @() }
if (Test-Path -LiteralPath $StatePath) {
    $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    if (!$state.ContainsKey('attempts')) { $state.attempts = @() }
}
$recent = @($state.attempts | Where-Object { [datetime]$_.at -ge $now.AddHours(-1) })
$events = [System.Collections.Generic.List[object]]::new()
foreach ($name in $TaskNames) {
    try {
        $task = Get-ScheduledTask -TaskName $name
        $info = Get-ScheduledTaskInfo -TaskName $name
        if ($task.State -ne 'Ready' -or [uint32]$info.LastTaskResult -ne $InterruptedExitCode) { continue }
        $age = ($now - $info.LastRunTime).TotalMinutes
        if ($age -lt $MinimumAgeMinutes -or $age -gt $MaximumAgeMinutes) { continue }
        $shard = if ($name -match '([AB])$') { $Matches[1] } else { $name }
        $sameShard = @($recent | Where-Object { $_.shard -eq $shard })
        $runKey = $name + '|' + $info.LastRunTime.ToString('o')
        if ($sameShard.Count -ge $MaximumRetriesPerShardHour -or
            @($recent | Where-Object { $_.run_key -eq $runKey }).Count -gt 0) { continue }
        Start-ScheduledTask -TaskName $name
        $event = @{ at = $now.ToString('o'); shard = $shard; run_key = $runKey; task = $name;
                    action = 'restart'; previous_exit = '0xC000013A' }
        $recent += $event
        $events.Add($event)
    }
    catch {
        $events.Add(@{ at = $now.ToString('o'); task = $name; action = 'error';
                       error = $_.Exception.GetType().Name })
    }
}
$state.attempts = @($recent)
$directory = Split-Path -Parent $StatePath
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$temporary = $StatePath + '.tmp'
$state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $StatePath -Force
foreach ($event in $events) {
    $eventLog = Join-Path $directory 'recovery-events.log'
    $level = if ($event.action -eq 'error') { 'ERROR' } else { 'WARN' }
    $line = "[$($event.at)] [$level] [pull-recovery] task=$($event.task) action=$($event.action) previous_exit=$($event.previous_exit)"
    $line | Add-Content -LiteralPath $eventLog -Encoding UTF8
    $line
}
$hasError = @($events | Where-Object action -eq 'error').Count -gt 0
@{ ok = !$hasError;
   data = @{ actions = @($events); checked_at = $now.ToString('o') } } | ConvertTo-Json -Depth 8
if ($hasError) { exit 1 }
