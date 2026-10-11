"""Offline launcher helper tests; no application processes or listeners are touched."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "launch.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_helpers(body):
    # Parse and load only named function declarations. Never invoke launch.ps1's
    # entry point, health checks, Start-Process, or any real process-control call.
    code = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tokens = $null
$parseErrors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile({literal(LAUNCHER)}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) {{ throw ($parseErrors | Out-String) }}
$wanted = @('Get-StudentFlowCodeFingerprint', 'Test-StudentFlowLaunchVersion',
            'Write-StudentFlowLaunchVersion', 'Stop-VerifiedStudentFlowListener', 'Assert-StudentFlowListener')
$functions = $tree.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $true)
foreach ($function in $functions) {{
    if ($wanted -contains $function.Name) {{ . ([scriptblock]::Create($function.Extent.Text)) }}
}}
{body}
"""
    command = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        check=False, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return json.loads(result.stdout.decode("utf-8-sig").strip())


def project(tmp_path):
    root = tmp_path / "synthetic project"
    files = {
        "app.py": "print('app')\n", "src/studentflow/core.py": "value = 1\n",
        "src/studentflow/nested/helper.py": "helper = 1\n", "pyproject.toml": "[project]\n",
        "uv.lock": "version = 1\n", ".streamlit/config.toml": "[server]\n",
        "data/tasks.sqlite3": "local tasks", "credentials/token.json": "local login",
        "README.md": "documentation",
    }
    for relative, text in files.items():
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("relative", [
    "app.py", "src/studentflow/core.py", "src/studentflow/nested/helper.py",
    "pyproject.toml", "uv.lock", ".streamlit/config.toml",
])
def test_fingerprint_changes_when_runtime_inputs_change(tmp_path, relative):
    root = project(tmp_path)
    result = run_helpers(f"""
$before = Get-StudentFlowCodeFingerprint -Root {literal(root)}
[System.IO.File]::AppendAllText({literal(root / relative)}, 'changed')
$after = Get-StudentFlowCodeFingerprint -Root {literal(root)}
@{{before=$before; after=$after}} | ConvertTo-Json -Compress
""")
    assert len(result["before"]) == 64
    assert result["before"] != result["after"]


def test_fingerprint_ignores_saved_data_credentials_and_documentation(tmp_path):
    root = project(tmp_path)
    result = run_helpers(f"""
$before = Get-StudentFlowCodeFingerprint -Root {literal(root)}
foreach ($path in @({literal(root / 'data/tasks.sqlite3')}, {literal(root / 'credentials/token.json')}, {literal(root / 'README.md')})) {{
    [System.IO.File]::AppendAllText($path, 'updated local content')
}}
$after = Get-StudentFlowCodeFingerprint -Root {literal(root)}
@{{before=$before; after=$after}} | ConvertTo-Json -Compress
""")
    assert result["before"] == result["after"]


def test_fingerprint_detects_added_or_removed_python_modules(tmp_path):
    root = project(tmp_path)
    added = root / "src/studentflow/new_module.py"
    result = run_helpers(f"""
$before = Get-StudentFlowCodeFingerprint -Root {literal(root)}
[System.IO.File]::WriteAllText({literal(added)}, 'added = True')
$added = Get-StudentFlowCodeFingerprint -Root {literal(root)}
Remove-Item -LiteralPath {literal(added)}
$removed = Get-StudentFlowCodeFingerprint -Root {literal(root)}
@{{before=$before; added=$added; removed=$removed}} | ConvertTo-Json -Compress
""")
    assert result["before"] != result["added"]
    assert result["before"] == result["removed"]


def test_manifest_requires_current_process_and_matching_code(tmp_path):
    path = tmp_path / "launcher.8501.version.json"
    result = run_helpers(f"""
$path = {literal(path)}
$missing = Test-StudentFlowLaunchVersion -Path $path -ProcessId 1234 -Fingerprint ('a' * 64)
Write-StudentFlowLaunchVersion -Path $path -ProcessId 1234 -Fingerprint ('a' * 64)
$matching = Test-StudentFlowLaunchVersion -Path $path -ProcessId 1234 -Fingerprint ('a' * 64)
$newProcess = Test-StudentFlowLaunchVersion -Path $path -ProcessId 9876 -Fingerprint ('a' * 64)
$newCode = Test-StudentFlowLaunchVersion -Path $path -ProcessId 1234 -Fingerprint ('b' * 64)
[System.IO.File]::WriteAllText($path, '{{invalid json')
$invalid = Test-StudentFlowLaunchVersion -Path $path -ProcessId 1234 -Fingerprint ('a' * 64)
@{{missing=$missing; matching=$matching; newProcess=$newProcess; newCode=$newCode; invalid=$invalid}} | ConvertTo-Json -Compress
""")
    assert result == {"missing": False, "matching": True, "newProcess": False, "newCode": False, "invalid": False}
    assert list(tmp_path.glob("*.tmp")) == []


def test_restart_reverifies_owner_and_only_stops_expected_process():
    result = run_helpers("""
$script:stopped = @()
function Assert-StudentFlowListener { return 1234 }
function Get-LocalListener { return $false }
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) $script:stopped += $Id }
Stop-VerifiedStudentFlowListener -ExpectedProcessId 1234
$blocked = $false
try { Stop-VerifiedStudentFlowListener -ExpectedProcessId 9999 }
catch { $blocked = $true }
@{stopped=@($script:stopped); blocked=$blocked} | ConvertTo-Json -Compress
""")
    assert result == {"stopped": [1234], "blocked": True}


def test_unrelated_port_owner_never_reaches_stop_process():
    result = run_helpers("""
$script:stopped = $false
$Port = 8501
$appPath = 'C:\\synthetic\\studentflow\\app.py'
function Get-NetTCPConnection { return [pscustomobject]@{OwningProcess=1234} }
function Get-CimInstance { return [pscustomobject]@{CommandLine='python -m streamlit run C:\\other-app\\app.py'} }
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) $script:stopped = $true }
$blocked = $false
try { Stop-VerifiedStudentFlowListener -ExpectedProcessId 1234 }
catch { $blocked = $true }
@{stopped=$script:stopped; blocked=$blocked} | ConvertTo-Json -Compress
""")
    assert result == {"stopped": False, "blocked": True}


def test_shutdown_listener_disappearance_between_snapshot_and_owner_lookup_succeeds():
    result = run_helpers("""
$script:stopped = @()
$script:ownerChecks = 0
$script:listenerChecks = 0
function Assert-StudentFlowListener {
    $script:ownerChecks += 1
    if ($script:ownerChecks -eq 1) { return 1234 }
    throw 'No matching TCP listener instance was found.'
}
function Get-LocalListener {
    $script:listenerChecks += 1
    return $script:listenerChecks -eq 1
}
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) $script:stopped += $Id }
Stop-VerifiedStudentFlowListener -ExpectedProcessId 1234
@{stopped=@($script:stopped); ownerChecks=$script:ownerChecks; listenerChecks=$script:listenerChecks} | ConvertTo-Json -Compress
""")
    assert result == {"stopped": [1234], "ownerChecks": 2, "listenerChecks": 2}


def test_shutdown_different_port_owner_is_blocked_without_stopping_it():
    result = run_helpers("""
$script:stopped = @()
$script:ownerChecks = 0
function Assert-StudentFlowListener {
    $script:ownerChecks += 1
    if ($script:ownerChecks -eq 1) { return 1234 }
    return 9876
}
function Get-LocalListener { return $true }
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) $script:stopped += $Id }
$blocked = $false
try { Stop-VerifiedStudentFlowListener -ExpectedProcessId 1234 }
catch { $blocked = $true }
@{stopped=@($script:stopped); blocked=$blocked} | ConvertTo-Json -Compress
""")
    assert result == {"stopped": [1234], "blocked": True}
