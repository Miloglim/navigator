param(
    [switch]$EmitJson,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$CHECK_INTERVAL_MINUTES = 5
$ALERT_REPEAT_MINUTES = 30
$TASK_GRACE_MINUTES = 20
$RUN_OVERLAP_GRACE_MINUTES = 2
$MORNING_START_HOUR = 9
$MORNING_END_HOUR = 13
$AFTERNOON_START_HOUR = 14
$AFTERNOON_END_HOUR = 18
$AFTERNOON_END_MINUTE = 52
$RECEIPT_COMPLETION_DELAY_MINUTES = 5
$EMPTY_RESPONSE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
$NOISE_AUDIT_START = [datetime]"2026-09-25"
$ALERT_EXAMPLE_LIMIT = 3
$RUN_TRACE_START = [datetime]"2026-09-28 13:37:00"
$ABRUPT_GRACE_MINUTES = 15
$ABRUPT_ALERT_MINUTES = 45
$ACTIVE_RUN_MATCH_MINUTES = 2

$Here = $PSScriptRoot
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $Here)
$LogDir = Join-Path $Here "state"
$HeartbeatLogDir = Join-Path (Join-Path $ProjectRoot "ops\codex-heartbeat") "logs"
$StatePath = Join-Path $LogDir "pull-health-state.json"
$RuntimeDir = Join-Path $ProjectRoot "Skill\heartbeat-data\.work"
$ChatSourcesPath = Join-Path $ProjectRoot "Skill\freight-rate-heartbeat\scripts\config\chat_sources.json"
$OperationalExceptionsPath = Join-Path $Here "operational-exceptions.json"

$Tasks = @(
    @{ Name = "运价心跳 上午 A"; Shard = "A"; FrequencyMinutes = 60; Window = "morning" },
    @{ Name = "运价心跳 上午 B"; Shard = "B"; FrequencyMinutes = 60; Window = "morning" },
    @{ Name = "运价心跳 下午 A"; Shard = "A"; FrequencyMinutes = 10; Window = "afternoon" },
    @{ Name = "运价心跳 下午 B"; Shard = "B"; FrequencyMinutes = 10; Window = "afternoon" }
)

function Test-WindowActive {
    param([string]$Window, [datetime]$At)

    if ($Window -eq "morning") {
        return $At.Hour -ge $MORNING_START_HOUR -and $At.Hour -le $MORNING_END_HOUR
    }

    return $At.Hour -ge $AFTERNOON_START_HOUR -and
        ($At.Hour -lt $AFTERNOON_END_HOUR -or ($At.Hour -eq $AFTERNOON_END_HOUR -and $At.Minute -le $AFTERNOON_END_MINUTE))
}

function Get-LatestFile {
    param([string]$Pattern)

    Get-ChildItem -LiteralPath $HeartbeatLogDir -File -Filter $Pattern -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-LatestReceipt {
    param([string]$Shard, [datetime]$StartedAt, [datetime]$CompletedAt)

    # Bind the receipt to the task's actual run. Historical repair jobs can
    # write a newer file in an older run directory and must not mask failure.
    $runDir = Join-Path $RuntimeDir ("run_{0}_{1}" -f $StartedAt.ToString("MMdd"), $Shard)
    $pattern = "pull_receipt_{0}-*.json" -f $StartedAt.ToString("yyyyMMdd")
    Get-ChildItem -LiteralPath $runDir -File -Filter $pattern -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $StartedAt.AddMinutes(-1) -and
                       $_.LastWriteTime -le $CompletedAt.AddMinutes(1) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Test-ReceiptComplete {
    param([System.IO.FileInfo]$Receipt, [string]$Shard)

    try {
        $sourceConfig = Get-Content -LiteralPath $ChatSourcesPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $expectedSlugs = @($sourceConfig.groups | Where-Object { $_.shard -eq $Shard } | ForEach-Object slug)
        $payload = Get-Content -LiteralPath $Receipt.FullName -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
        $groups = $payload.groups
        if ($expectedSlugs.Count -eq 0 -or $null -eq $groups) {
            return @{ Complete = $false; Detail = "source configuration or receipt groups unavailable" }
        }

        $missing = @($expectedSlugs | Where-Object { !$groups.ContainsKey($_) })
        $failed = @($expectedSlugs | Where-Object { $groups.ContainsKey($_) -and $groups[$_].status -ne "ok" })
        $unverifiedEmpty = @($expectedSlugs | Where-Object {
            $groups.ContainsKey($_) -and $groups[$_].status -eq "ok" -and
            $groups[$_].raw_sha256 -eq $EMPTY_RESPONSE_SHA256
        })
        if ($missing.Count -gt 0 -or $failed.Count -gt 0 -or $unverifiedEmpty.Count -gt 0) {
            return @{ Complete = $false; Detail = "missing=$($missing -join ',') failed=$($failed -join ',') unverified_empty=$($unverifiedEmpty -join ',')" }
        }
        return @{ Complete = $true; Detail = "" }
    }
    catch {
        return @{ Complete = $false; Detail = "receipt parse error: $($_.Exception.Message)" }
    }
}

function Get-MessageFingerprint {
    param($Message)
    $text = [string]$Message.c + "`n" + (@($Message.img) -join ',')
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    return [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}
function Test-SuspiciousNoise {
    param([string]$Shard, [datetime]$At, [string[]]$Covered)

    # Scan all incident-era caches, so a missed message cannot disappear from
    # the alarm simply because the calendar day changed.
    $priceLike = '(?i)(?:USD\s*\$?\s*\d{3,5}|\$\s*\d{3,5}|\b\d{3,5}\s*/\s*\d{3,5}|\b\d{4,5}\s*\+\s*PCS)'
    $hits = [System.Collections.Generic.HashSet[string]]::new()
    $exceptions = Get-Content -LiteralPath $OperationalExceptionsPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    $messageDirs = @(Get-ChildItem -LiteralPath $RuntimeDir -Directory -Filter 'msgs_*' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match ('^msgs_(\d{4})_' + $Shard + '$') -and
                       $_.LastWriteTime -ge $NOISE_AUDIT_START })
    foreach ($messagesDir in $messageDirs) {
        $decisionsDir = Join-Path $RuntimeDir ($messagesDir.Name -replace '^msgs_', 'llm_out_')
        if (!(Test-Path -LiteralPath $decisionsDir)) { continue }
        foreach ($file in (Get-ChildItem -LiteralPath $messagesDir.FullName -File -Filter '*.json')) {
            try {
                $messages = @(Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)
                foreach ($m in $messages) {
                    if ([string]$m.id -in $Covered) { continue }
                    if ($exceptions.ContainsKey([string]$m.id) -and
                        $exceptions[[string]$m.id].fingerprint -eq (Get-MessageFingerprint $m)) { continue }
                    $suspicious = [string]$m.c -match $priceLike
                    if (!$suspicious) {
                        foreach ($mediaId in @($m.img)) {
                            if (!$mediaId) { continue }
                            $cachePath = Join-Path $RuntimeDir ("ocr_cache\" + ([string]$mediaId).TrimStart('@') + ".json")
                            if (!(Test-Path -LiteralPath $cachePath)) { $suspicious = $true; break }
                            $ocrText = Get-Content -LiteralPath $cachePath -Raw -Encoding UTF8
                            if ($ocrText -match '"type"\s*:\s*"(?:rate_matrix|price_card|space_table|space_matrix)"' -or
                                $ocrText -match $priceLike) { $suspicious = $true; break }
                        }
                    }
                    if (!$suspicious) { continue }
                    $safeId = [regex]::Replace([string]$m.id, '[^A-Za-z0-9_.-]', '_')
                    $decisionPath = Join-Path $decisionsDir ($safeId + '.json')
                    if (!(Test-Path -LiteralPath $decisionPath)) {
                        [void]$hits.Add("$($m.id) missing=decision")
                        continue
                    }
                    $decision = Get-Content -LiteralPath $decisionPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($decision.decision -in @('noise', 'records') -and -not
                        ($decision.noise_review -eq 'inquiry_only' -and [string]$m.c -match '问下|还能定吗|询问')) {
                        $detail = [string]$m.id
                        if ($decision.missing_fields) { $detail += " missing=$($decision.missing_fields)" }
                        [void]$hits.Add($detail)
                    }
                }
            }
            catch {
                return @{ Count = -1; Examples = @($file.Name, $_.Exception.Message) }
            }
        }
    }
    return @{ Count = $hits.Count; Examples = @($hits | Sort-Object | Select-Object -First $ALERT_EXAMPLE_LIMIT) }
}

function Read-State {
    if (!(Test-Path -LiteralPath $StatePath)) {
        return @{}
    }

    try {
        return Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    }
    catch {
        return @{}
    }
}

function Write-Event {
    param([string]$Kind, [string[]]$Issues, [datetime]$At)

    $stamp = $At.ToString("yyyyMMdd-HHmmss")
    $path = Join-Path $LogDir ("pull-health-{0}-{1}.md" -f $Kind, $stamp)
    $lines = @( ("# HEARTBEAT_{0}" -f $Kind), ("time: {0}" -f $At.ToString("s")), "" ) + @($Issues)
    $lines | Set-Content -LiteralPath $path -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$now = Get-Date
$issues = [System.Collections.Generic.List[string]]::new()
$activeRunStarts = [System.Collections.Generic.List[datetime]]::new()

foreach ($definition in $Tasks) {
    try {
        $task = Get-ScheduledTask -TaskName $definition.Name
        $info = Get-ScheduledTaskInfo -TaskName $definition.Name
        if ($task.State -eq 'Running') { $activeRunStarts.Add($info.LastRunTime) }
    }
    catch {
        $issues.Add("P1 task missing: $($definition.Name)")
        continue
    }

    if ($task.Principal.UserId -ne "YQN" -or $task.Principal.LogonType -ne "Interactive") {
        $issues.Add("P1 execution identity changed: $($definition.Name) user=$($task.Principal.UserId) logon=$($task.Principal.LogonType)")
    }
    if ($task.Settings.DisallowStartIfOnBatteries -or $task.Settings.StopIfGoingOnBatteries) {
        $issues.Add("P1 battery stop policy enabled: $($definition.Name)")
    }

    $activeWindow = Test-WindowActive -Window $definition.Window -At $now
    $latestLog = Get-LatestFile -Pattern ("heartbeat-{0}-*.md" -f $definition.Shard)
    if ($task.State -eq "Running") {
        # LastRunTime may be overwritten by a trigger ignored during an active run.
        $activeLog = Get-LatestFile -Pattern ("heartbeat-{0}-*.log" -f $definition.Shard)
        $startedAt = if ($activeLog -and
            !(Test-Path -LiteralPath ([IO.Path]::ChangeExtension($activeLog.FullName, '.result.json')))) {
            $activeLog.CreationTime
        } else { $info.LastRunTime }
        if ($startedAt -lt $now.AddMinutes(-($definition.FrequencyMinutes + $RUN_OVERLAP_GRACE_MINUTES))) {
            $issues.Add("P1 collection run overran next trigger: $($definition.Name) started=$($startedAt.ToString('s')) minutes=$([int]($now-$startedAt).TotalMinutes) last_result=$($info.LastTaskResult)")
        }
        continue
    }
    if ($info.LastTaskResult -ne 0) {
        if ($null -eq $latestLog -or $latestLog.LastWriteTime -lt $info.LastRunTime) {
            $failedRunPath = Join-Path $RuntimeDir ("run_{0}_{1}\state.json" -f $info.LastRunTime.ToString("MMdd"), $definition.Shard)
            $impact = ""
            if (Test-Path -LiteralPath $failedRunPath) {
                try {
                    $failedRun = Get-Content -LiteralPath $failedRunPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $impact = " stage=$($failedRun.stage) pulled=$([int]$failedRun.pulled_total) submitted=$([int]$failedRun.records_submitted)"
                }
                catch { $impact = " state=unreadable" }
            }
            $issues.Add("P1 task failed: $($definition.Name) exit=$($info.LastTaskResult) at $($info.LastRunTime.ToString('s'))$impact")
        }
        continue
    }

    if (!$activeWindow) {
        continue
    }

    if ($null -eq $latestLog -or $latestLog.LastWriteTime -lt $now.AddMinutes(-($definition.FrequencyMinutes + $TASK_GRACE_MINUTES))) {
        $issues.Add("P1 completion log stale: $($definition.Name)")
        continue
    }

    # This check is read-only and independent of the Agent's parsing path.
    # Task Scheduler can report exit 0 even when the business run is on review hold.
    $runStatePath = Join-Path $RuntimeDir ("run_{0}_{1}\state.json" -f $now.ToString("MMdd"), $definition.Shard)
    if (Test-Path -LiteralPath $runStatePath) {
        try {
            $runState = Get-Content -LiteralPath $runStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($runState.stage -in @("USER_REVIEW", "PULL", "PARSED", "LLM_PENDING", "BUILD", "PRECHECK", "SUBMIT")) {
                $issues.Add("P1 business run incomplete: $($definition.Name) stage=$($runState.stage) at $($info.LastRunTime.ToString('s'))")
            }
        }
        catch {
            $issues.Add("P1 business run state unreadable: $($definition.Name) $($_.Exception.Message)")
        }
    }
    else {
        $issues.Add("P1 business run state missing: $($definition.Name)")
    }

    if ($latestLog.LastWriteTime -lt $now.AddMinutes(-$RECEIPT_COMPLETION_DELAY_MINUTES)) {
        $receipt = Get-LatestReceipt -Shard $definition.Shard -StartedAt $info.LastRunTime -CompletedAt $latestLog.LastWriteTime
        if ($null -eq $receipt -or $receipt.LastWriteTime -lt $latestLog.LastWriteTime.AddMinutes(-$CHECK_INTERVAL_MINUTES)) {
            $issues.Add("P1 pull receipt missing after completed run: $($definition.Name)")
        }
        else {
            $receiptCheck = Test-ReceiptComplete -Receipt $receipt -Shard $definition.Shard
            if (!$receiptCheck.Complete) {
                $issues.Add("P1 incomplete pull receipt: $($definition.Name) $($receiptCheck.Detail)")
            }
        }
    }
}

try {
    $recoveryTask = Get-ScheduledTask -TaskName '运价心跳中断恢复'
    $recoveryInfo = Get-ScheduledTaskInfo -TaskName '运价心跳中断恢复'
    if ($recoveryTask.State -eq 'Disabled') { $issues.Add('P1 interrupted-run recovery disabled') }
    elseif ($recoveryTask.State -ne 'Running' -and $recoveryInfo.LastTaskResult -ne 0 -and
            $recoveryInfo.LastTaskResult -ne 267011) {
        $issues.Add("P1 interrupted-run recovery failed: exit=$($recoveryInfo.LastTaskResult)")
    }
    if ($recoveryInfo.LastRunTime -lt $now.AddMinutes(-15) -and
        ((Test-WindowActive -Window 'morning' -At $now) -or (Test-WindowActive -Window 'afternoon' -At $now))) {
        $issues.Add('P1 interrupted-run recovery stale: no run within 15 minutes')
    }
}
catch { $issues.Add("P1 interrupted-run recovery task missing: $($_.Exception.GetType().Name)") }
# A CLI/pwsh process killed by console control never writes its result marker.
# Keep that evidence visible after a later retry overwrites LastTaskResult.
$abrupt = @(Get-ChildItem -LiteralPath $HeartbeatLogDir -File -Filter 'heartbeat-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $candidateFile = $_
                   $candidateFile.CreationTime -ge $RUN_TRACE_START -and
                   $_.LastWriteTime -le $now.AddMinutes(-$ABRUPT_GRACE_MINUTES) -and
                   $_.LastWriteTime -ge $now.AddMinutes(-$ABRUPT_ALERT_MINUTES) -and
                   @($activeRunStarts | Where-Object { [Math]::Abs(($_ - $candidateFile.CreationTime).TotalMinutes) -le $ACTIVE_RUN_MATCH_MINUTES }).Count -eq 0 -and
                   !(Test-Path -LiteralPath ([IO.Path]::ChangeExtension($_.FullName, '.result.json'))) })
$state = Read-State
$previouslyReported = @($state.recovered_interruptions | Where-Object { $_ })
$recovered = [System.Collections.Generic.List[string]]::new()
$unrecovered = [System.Collections.Generic.List[string]]::new()
$successfulRuns = @(Get-ChildItem -LiteralPath $HeartbeatLogDir -File -Filter 'heartbeat-*.result.json' -ErrorAction SilentlyContinue |
    Where-Object { $_.CreationTime -ge $RUN_TRACE_START -and $_.CreationTime -ge $now.AddMinutes(-$ABRUPT_ALERT_MINUTES) })
foreach ($file in $abrupt) {
    $shard = if ($file.BaseName -match '^heartbeat-([AB])-') { $Matches[1] } else { '' }
    $hasRecovery = $false
    foreach ($resultFile in $successfulRuns) {
        if ($resultFile.CreationTime -le $file.CreationTime -or
            $resultFile.BaseName -notmatch ('^heartbeat-' + $shard + '-')) { continue }
        try {
            $result = Get-Content -LiteralPath $resultFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$result.exit_code -eq 0) { $hasRecovery = $true; break }
        }
        catch { $issues.Add("P1 run result marker unreadable: $($resultFile.Name)") }
    }
    if ($hasRecovery) { $recovered.Add($file.BaseName) }
    else { $unrecovered.Add($file.BaseName) }
}
if ($unrecovered.Count -gt 0) {
    $issues.Add("P1 heartbeat process interrupted without verified restart: count=$($unrecovered.Count) runs=$((@($unrecovered | Select-Object -First $ALERT_EXAMPLE_LIMIT)) -join ',')")
}
$newRecoveries = @($recovered | Where-Object { $_ -notin $previouslyReported })
if ($newRecoveries.Count -gt 0) {
    if (!$DryRun) {
        Write-Event -Kind 'RECOVERED-INTERRUPT' -Issues @("Interrupted runs later completed successfully: count=$($newRecoveries.Count) runs=$($newRecoveries -join ',')") -At $now
    }
    $state.recovered_interruptions = @($previouslyReported + $newRecoveries)
}
# Read authoritative pending source IDs in a separate read-only process.
# No Agent decision or mutable per-run state can manufacture coverage.
$covered = [System.Collections.Generic.HashSet[string]]::new()
$coverageAvailable = $false
$coverageScript = Join-Path $Here 'pending-coverage.py'
$python = 'C:\Users\YQN\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
try {
    $coverageOutput = & $python $coverageScript --receipts-dir (Join-Path $RuntimeDir 'submit_receipts') 2>&1
    $coverage = $coverageOutput | Out-String | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$coverage.ok) {
        throw "ledger query failed: $($coverage.error)"
    }
    if ([int]$coverage.data.malformed -gt 0) {
        $issues.Add("P1 pending payload unreadable: count=$($coverage.data.malformed)")
    }
    foreach ($id in $coverage.data.source_counts.PSObject.Properties.Name) {
        if ([int]$coverage.data.source_counts.$id -gt 0) { [void]$covered.Add([string]$id) }
    }
    foreach ($receiptIssue in @($coverage.data.receipt_issues)) {
        if ($null -ne $receiptIssue) {
            $coverageSeverity = if (@($receiptIssue.issues | Where-Object { $_.reason -ne 'legacy_partial_unverified' }).Count) { 'P1' } else { 'P2' }
            $issues.Add("$coverageSeverity pending item coverage: $($receiptIssue.file) $($receiptIssue.issues | ConvertTo-Json -Compress -Depth 8)")
        }
    }
    $coverageAvailable = $true
}
catch {
    $issues.Add("P1 pending coverage unavailable: $($_.Exception.Message)")
}
# Per-entry and legacy receipt checks are returned by pending-coverage.py above.
# Sidecar review of accepted decisions, independent of the Agent parser.
if ($coverageAvailable) {
    foreach ($shard in @("A", "B")) {
        $noise = Test-SuspiciousNoise -Shard $shard -At $now -Covered @($covered)
        if ($noise.Count -lt 0) {
            $issues.Add("P1 noise audit unreadable: $shard $($noise.Examples -join ' ')")
        }
        elseif ($noise.Count -gt 0) {
            $issues.Add("P1 price/image messages without pending coverage: $shard count=$($noise.Count) ids=$($noise.Examples -join ',')")
        }
    }
}

$fingerprint = [string]::Join("`n", @($issues | Sort-Object))
$lastFingerprint = [string]($state.fingerprint)
$lastAlertAt = if ($state.last_alert_at) { [datetime]$state.last_alert_at } else { [datetime]::MinValue }

if ($issues.Count -gt 0) {
    if ($fingerprint -ne $lastFingerprint -or $lastAlertAt -lt $now.AddMinutes(-$ALERT_REPEAT_MINUTES)) {
        if (!$DryRun) { Write-Event -Kind "ALERT" -Issues $issues.ToArray() -At $now }
        $state.last_alert_at = $now.ToString("s")
    }
    $state.fingerprint = $fingerprint
}
elseif ($lastFingerprint) {
    if (!$DryRun) { Write-Event -Kind "RECOVERED" -Issues @("All scheduled pull checks are healthy.") -At $now }
    $state.fingerprint = ""
    $state.last_alert_at = ""
}

$state.checked_at = $now.ToString("s")
if (!$DryRun) { $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8 }

if ($EmitJson) {
    [PSCustomObject]@{
        CheckedAt = $now.ToString("s")
        Healthy = $issues.Count -eq 0
        DryRun = [bool]$DryRun
        IssueCount = $issues.Count
        Issues = $issues
    } | ConvertTo-Json -Depth 3
}
