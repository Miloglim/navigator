"""Restrict the local agent credential to the service identity and Windows admins."""
import os
import subprocess

ACL_TIMEOUT_SECONDS = 30
ACL_PATH_VARIABLE = "FREIGHT_TOKEN_ACL_PATH"
WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:FREIGHT_TOKEN_ACL_PATH
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetOwner($identity)
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique) {
    $principal = New-Object System.Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
}
[System.IO.File]::SetAccessControl($path, $acl)
"""


def protect_token_file(path):
    if os.name != 'nt':
        path.chmod(0o600)
        return
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', WINDOWS_ACL_SCRIPT],
        env={**os.environ, ACL_PATH_VARIABLE: str(path.resolve())},
        capture_output=True, timeout=ACL_TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError('Agent token file permissions could not be secured; startup refused')
