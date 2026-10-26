param([switch]$StatusJson)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$freightRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\Database')).Path
$prospectorRoot = 'D:\Agent Basement\Milogin-s-prospector'
$freightLauncher = Join-Path $freightRoot '运价看板-启动并打开.bat'
$prospectorLauncher = Join-Path $prospectorRoot 'start.bat'
$navigatorUrl = 'http://127.0.0.1:8795/'
$centerUrl = 'http://127.0.0.1:55847/'
$centerPostgresConfig = 'C:\ProgramData\MiloginCenter\postgres.json'
$probeTimeoutMs = 1200
$startupTimeoutSeconds = 90

function Test-HttpReady([string]$url, [string]$backend = '') {
    try {
        $response = Invoke-RestMethod -Uri $url -TimeoutSec 2 -ErrorAction Stop
        if ($response.ok -ne $true) { return $false }
        if ($backend -and $response.data.backend -ne $backend) { return $false }
        return $true
    } catch { return $false }
}

function Test-LocalPort([int]$port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        return $result.AsyncWaitHandle.WaitOne($probeTimeoutMs) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Test-ProspectorWindow {
    try {
        $marker = '--prospector-dev-launcher=' + $prospectorRoot
        $processes = (Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" -ErrorAction Stop) |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) }
        foreach ($candidate in $processes) {
            $window = Get-Process -Id $candidate.ProcessId -ErrorAction Stop
            if ([int64]$window.MainWindowHandle -ne 0 -and $window.Responding) { return $true }
        }
        return $false
    } catch { return $false }
}

function Get-ServiceStates {
    $navigator = Test-HttpReady ($navigatorUrl + 'api/health/live') 'postgres'
    $center = Test-HttpReady ($centerUrl + 'health/ready')
    [pscustomobject]@{
        navigator = $navigator
        prospector = Test-ProspectorWindow
        center = $center
        centerDatabase = Test-LocalPort 55846
    }
}

if ($StatusJson) {
    Get-ServiceStates | ConvertTo-Json -Compress
    return
}

$existingPanel = Get-Process powershell -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $PID -and $_.MainWindowTitle -eq '本机服务' } |
    Select-Object -First 1
if ($existingPanel) {
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.Interaction]::AppActivate($existingPanel.Id)
    return
}

$form = [System.Windows.Forms.Form]::new()
$form.Text = '本机服务'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = [System.Drawing.Size]::new(560, 336)
$form.MinimumSize = [System.Drawing.Size]::new(576, 375)
$form.MaximizeBox = $false
$form.FormBorderStyle = 'FixedSingle'
$form.BackColor = [System.Drawing.Color]::FromArgb(247, 249, 251)
$form.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 9)

$title = [System.Windows.Forms.Label]::new()
$title.Text = '本机服务'
$title.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 16, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = [System.Drawing.Color]::FromArgb(26, 43, 59)
$title.SetBounds(24, 18, 390, 35)
$form.Controls.Add($title)

$subtitle = [System.Windows.Forms.Label]::new()
$subtitle.Text = '启动与访问'
$subtitle.ForeColor = [System.Drawing.Color]::FromArgb(99, 112, 124)
$subtitle.SetBounds(25, 55, 250, 22)
$form.Controls.Add($subtitle)

$rows = @{}
function Add-ServiceRow([string]$key, [string]$name, [string]$detail, [int]$top) {
    $panel = [System.Windows.Forms.Panel]::new()
    $panel.SetBounds(20, $top, 520, 66)
    $panel.BackColor = [System.Drawing.Color]::White
    $panel.BorderStyle = 'FixedSingle'
    $nameLabel = [System.Windows.Forms.Label]::new()
    $nameLabel.Text = $name
    $nameLabel.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
    $nameLabel.SetBounds(14, 9, 245, 23)
    $detailLabel = [System.Windows.Forms.Label]::new()
    $detailLabel.Text = $detail
    $detailLabel.ForeColor = [System.Drawing.Color]::FromArgb(103, 115, 127)
    $detailLabel.SetBounds(15, 36, 330, 18)
    $status = [System.Windows.Forms.Label]::new()
    $status.Text = '检查中'
    $status.TextAlign = 'MiddleRight'
    $status.SetBounds(280, 12, 100, 24)
    $button = [System.Windows.Forms.Button]::new()
    $button.FlatStyle = 'Flat'
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = [System.Drawing.Color]::FromArgb(23, 70, 94)
    $button.ForeColor = [System.Drawing.Color]::White
    $button.SetBounds(393, 15, 110, 35)
    $panel.Controls.AddRange(@($nameLabel, $detailLabel, $status, $button))
    $form.Controls.Add($panel)
    $rows[$key] = @{ Status = $status; Button = $button }
}

Add-ServiceRow 'navigator' 'Navigator 运价库' '同事访问的运价网页' 85
Add-ServiceRow 'prospector' 'Prospector' '客户开发桌面应用' 157
Add-ServiceRow 'center' 'Milogin 中心' '账号管理工作台与正式身份库' 229

$foot = [System.Windows.Forms.Label]::new()
$foot.Text = ''
$foot.ForeColor = [System.Drawing.Color]::FromArgb(103, 115, 127)
$foot.SetBounds(25, 307, 435, 20)
$form.Controls.Add($foot)
$refresh = [System.Windows.Forms.Button]::new()
$refresh.Text = '刷新'
$refresh.FlatStyle = 'Flat'
$refresh.ForeColor = [System.Drawing.Color]::FromArgb(23, 70, 94)
$refresh.SetBounds(473, 302, 65, 25)
$form.Controls.Add($refresh)

$pending = @{}
$states = $null
function Start-ExistingLauncher([string]$path, [string]$arguments, [string]$workingDirectory) {
    if (-not (Test-Path -LiteralPath $path)) { throw '启动程序不存在' }
    $command = '""{0}" {1}"' -f $path, $arguments
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/c', $command) -WorkingDirectory $workingDirectory -WindowStyle Hidden -PassThru
}

function Start-ProductionCenter {
    if (-not (Test-Path -LiteralPath $centerPostgresConfig)) { throw '正式中心数据库配置不存在' }
    $config = Get-Content -LiteralPath $centerPostgresConfig -Raw | ConvertFrom-Json
    if (-not $config.appPassword -or $config.port -ne 55846 -or $config.database -ne 'milogin_center') {
        throw '正式中心数据库配置不完整或目标不匹配'
    }
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $node
    $startInfo.WorkingDirectory = $prospectorRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    [void]$startInfo.ArgumentList.Add((Join-Path $prospectorRoot 'scripts\production-server.mjs'))
    [void]$startInfo.ArgumentList.Add('start')
    $startInfo.Environment['PROSPECTOR_CENTER_MODE'] = 'production'
    $startInfo.Environment['PROSPECTOR_CENTER_PG_DSN'] = "postgresql://milogin_app:$($config.appPassword)@127.0.0.1:55846/milogin_center"
    $startInfo.Environment['PROSPECTOR_CENTER_PUBLIC_ORIGIN'] = 'http://127.0.0.1:55847'
    $startInfo.Environment['PROSPECTOR_CENTER_LOCAL_HTTP'] = '1'
    return [System.Diagnostics.Process]::Start($startInfo)
}

function Update-Panel {
    $script:states = Get-ServiceStates
    $ready = @{ navigator = $states.navigator; prospector = $states.prospector; center = $states.center }
    foreach ($key in @('navigator', 'prospector', 'center')) {
        $row = $rows[$key]
        $running = $ready[$key]
        $row.Status.Text = if ($running) { '运行中' } elseif ($key -eq 'center' -and $states.centerDatabase) { '服务未启动' } else { '未启动' }
        $row.Status.ForeColor = if ($running) { [System.Drawing.Color]::FromArgb(24, 120, 83) } else { [System.Drawing.Color]::FromArgb(119, 130, 140) }
        $row.Button.Text = if ($running) { if ($key -eq 'prospector') { '打开窗口' } else { '打开页面' } } elseif ($pending.ContainsKey($key)) { '启动中' } else { '启动' }
        $row.Button.Enabled = -not $pending.ContainsKey($key)
    }
    if ($pending.ContainsKey('center') -and -not $states.centerDatabase) { $rows.center.Status.Text = '启动数据库中' }
    $foot.Text = '最近检查 ' + (Get-Date -Format 'HH:mm:ss')
}

function Invoke-PanelAction([string]$key) {
    if ($pending.ContainsKey($key)) { return }
    if ($states.$key -and $key -ne 'prospector') {
        Start-Process $(if ($key -eq 'center') { $centerUrl } else { $navigatorUrl })
        return
    }
    try {
        if ($key -eq 'center') {
            $process = Start-ProductionCenter
        } else {
            $path = switch ($key) { navigator { $freightLauncher } prospector { $prospectorLauncher } }
            $arguments = switch ($key) { navigator { '--no-open' } prospector { '' } }
            $workingDirectory = if ($key -eq 'navigator') { $freightRoot } else { $prospectorRoot }
            $process = Start-ExistingLauncher $path $arguments $workingDirectory
        }
        if ($key -eq 'prospector' -and $states.prospector) { return }
        $pending[$key] = @{ Process = $process; Started = Get-Date; Opened = $false }
        $rows[$key].Button.Enabled = $false
        $rows[$key].Button.Text = '启动中'
    } catch {
        [System.Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, '启动失败', 'OK', 'Warning') | Out-Null
    }
}

$rows.navigator.Button.Add_Click({ Invoke-PanelAction 'navigator' })
$rows.prospector.Button.Add_Click({ Invoke-PanelAction 'prospector' })
$rows.center.Button.Add_Click({ Invoke-PanelAction 'center' })
$refresh.Add_Click({ Update-Panel })

$timer = [System.Windows.Forms.Timer]::new()
$timer.Interval = 3000
$timer.Add_Tick({
    Update-Panel
    foreach ($key in @($pending.Keys)) {
        $item = $pending[$key]
        $isReady = $states.$key
        if ($isReady) {
            if ($key -eq 'navigator') { Start-Process $navigatorUrl }
            elseif ($key -eq 'center') { Start-Process $centerUrl }
            $pending.Remove($key)
        } elseif (($item.Process.HasExited -and $item.Process.ExitCode -ne 0) -or ((Get-Date) - $item.Started).TotalSeconds -gt $startupTimeoutSeconds) {
            $pending.Remove($key)
            $rows[$key].Status.Text = '启动失败'
            [System.Windows.Forms.MessageBox]::Show($form, '启动未完成。请检查原项目启动日志或运行状态。', '启动失败', 'OK', 'Warning') | Out-Null
        }
    }
})
$form.Add_Shown({ Update-Panel; $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })
[void]$form.ShowDialog()
