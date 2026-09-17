"""purge_files_recyclebin.py — 把 Database/images/ 与 Skill/heartbeat-data/.work/ocr_cache/ 里所有文件送回收站"""
import sys, io, base64, subprocess
sys.stdout.reconfigure(encoding='utf-8')

PS_SCRIPT = r'''
Add-Type -AssemblyName Microsoft.VisualBasic
$dirs = @(
  'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\images',
  'C:\Users\YQN\Documents\正式版运价库\运价项目\Skill\heartbeat-data\.work\ocr_cache'
)
$total = 0; $failed = 0
foreach ($d in $dirs) {
  if (-not (Test-Path $d)) { "skip (not exists): $d"; continue }
  $files = Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue
  "processing $d : $($files.Count) files"
  foreach ($f in $files) {
    try {
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($f.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
      $total++
    } catch {
      $failed++
      "FAIL: $($f.Name) - $($_.Exception.Message)"
    }
  }
}
"SENT-TO-RECYCLE-BIN=$total FAILED=$failed"
'''

encoded = base64.b64encode(PS_SCRIPT.encode('utf-16-le')).decode('ascii')
r = subprocess.run(
    ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
    capture_output=True, text=True, encoding='utf-8', errors='replace')
print(r.stdout)
if r.stderr.strip():
    print('STDERR:', r.stderr)
print('exit code:', r.returncode)
