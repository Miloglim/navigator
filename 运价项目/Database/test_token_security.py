"""Exercise ACL enforcement and token compatibility without touching live secrets."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from manual_server import ensure_token
from ops.token_security import protect_token_file


class TokenSecurityTests(unittest.TestCase):
    def test_existing_token_remains_valid_and_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.txt'
            original = ensure_token(path)
            self.assertEqual(ensure_token(path), original)
            if os.name == 'nt':
                script = '$a=[System.IO.File]::GetAccessControl($env:FREIGHT_TOKEN_ACL_PATH); if(-not $a.AreAccessRulesProtected -or @($a.Access | Where-Object IsInherited).Count){exit 1}; $a.Access.IdentityReference | ForEach-Object {$_.Translate([System.Security.Principal.SecurityIdentifier]).Value}'
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], env={**os.environ, 'FREIGHT_TOKEN_ACL_PATH': str(path)}, capture_output=True, text=True, check=True)
                allowed = set(result.stdout.split())
                self.assertEqual(len(allowed), 3)
                self.assertIn('S-1-5-18', allowed)
                self.assertIn('S-1-5-32-544', allowed)
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL failure path')
    def test_security_failure_refuses_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.txt'
            path.touch()
            with patch('ops.token_security.subprocess.run') as run:
                run.return_value.returncode = 1
                with self.assertRaises(RuntimeError):
                    protect_token_file(path)


if __name__ == '__main__':
    unittest.main()
