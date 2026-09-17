param([int]$Port = 8795)
# 放行局域网访问人工维护台账（默认端口 8795）。需要管理员权限的 PowerShell。
$name = "FreightManual-$Port"
$existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($existing) {
  Write-Output "[firewall] rule already exists: $name"
} else {
  New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
  Write-Output "[firewall] inbound rule added: $name (TCP $Port)"
}
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  ForEach-Object { Write-Output ("[firewall] LAN url: http://" + $_.IPAddress + ":$Port/") }
