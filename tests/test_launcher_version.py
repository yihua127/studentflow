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
            'Write-StudentFlowLaunchVersion', 'Stop-VerifiedStudentFlowListener', 'Assert-StudentFlowListener',
            'Format-StudentFlowLaunchError')
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


@pytest.mark.parametrize("native_code", [577, 1260, 4551])
def test_policy_error_in_nested_exception_identifies_python_and_keeps_protections(native_code):
    result = run_helpers(rf"""
$native = [System.ComponentModel.Win32Exception]::new({native_code}, 'Synthetic localized failure')
$wrapped = [System.InvalidOperationException]::new('Synthetic outer failure', $native)
$wrapped.Data['environment'] = 'PRIVATE-ENVIRONMENT-NOT-FOR-DISPLAY'
$failure = [System.Management.Automation.ErrorRecord]::new(
    $wrapped, 'SyntheticStartFailure', [System.Management.Automation.ErrorCategory]::PermissionDenied,
    'PRIVATE-INVOCATION-NOT-FOR-DISPLAY')
$message = Format-StudentFlowLaunchError -Failure $failure -Stage 'Starting Python' -Target 'C:\synthetic\.venv\Scripts\python.exe'
@{{message=$message}} | ConvertTo-Json -Compress
""")
    message = result["message"]
    assert "Stage: Starting Python" in message
    assert r"Target: C:\synthetic\.venv\Scripts\python.exe" in message
    assert "HRESULT: 0x" in message and f"Windows error: {native_code}" in message
    assert "official, digitally signed Python" in message
    assert "trusted by this device" in message
    assert "Keep Windows security protections enabled." in message
    assert "PRIVATE" not in message
    if native_code == 577:
        assert "could not verify the digital signature" in message
    else:
        assert "Windows application control blocked" in message


def test_policy_hresult_without_win32_exception_is_decoded():
    hresult = 0x800711C7 - 2**32
    result = run_helpers(rf"""
$exception = [System.Runtime.InteropServices.ExternalException]::new('Synthetic failure', {hresult})
$failure = [System.Management.Automation.ErrorRecord]::new(
    $exception, 'SyntheticFailure', [System.Management.Automation.ErrorCategory]::PermissionDenied, $null)
$message = Format-StudentFlowLaunchError -Failure $failure -Stage 'Starting Python' -Target 'C:\synthetic\python.exe'
@{{message=$message}} | ConvertTo-Json -Compress
""")
    assert "HRESULT: 0x800711C7" in result["message"]
    assert "Windows error: 4551" in result["message"]
    assert "official, digitally signed Python" in result["message"]


def test_browser_policy_error_does_not_recommend_replacing_python():
    result = run_helpers("""
$exception = [System.ComponentModel.Win32Exception]::new(1260, 'Synthetic browser block')
$failure = [System.Management.Automation.ErrorRecord]::new(
    $exception, 'SyntheticFailure', [System.Management.Automation.ErrorCategory]::PermissionDenied, $null)
$message = Format-StudentFlowLaunchError -Failure $failure -Stage 'Opening browser' -Target 'http://127.0.0.1:8501'
@{message=$message} | ConvertTo-Json -Compress
""")
    message = result["message"]
    assert "Stage: Opening browser" in message
    assert "Target: http://127.0.0.1:8501" in message
    assert "local app is ready" in message and "approved browser" in message
    assert "Python" not in message
    assert "Keep Windows security protections enabled." in message


def test_other_native_error_preserves_reason_without_claiming_policy_block():
    result = run_helpers(r"""
$exception = [System.ComponentModel.Win32Exception]::new(2, 'Synthetic file was not found.')
$exception.Data['token'] = 'PRIVATE-TOKEN-NOT-FOR-DISPLAY'
$failure = [System.Management.Automation.ErrorRecord]::new(
    $exception, 'SyntheticFailure', [System.Management.Automation.ErrorCategory]::ObjectNotFound,
    'PRIVATE-COMMAND-LINE-NOT-FOR-DISPLAY')
$message = Format-StudentFlowLaunchError -Failure $failure -Stage 'Starting Python' -Target 'C:\synthetic\python.exe'
@{message=$message} | ConvertTo-Json -Compress
""")
    message = result["message"]
    assert "Windows error: 2" in message
    assert "Details: Synthetic file was not found." in message
    assert "application control blocked" not in message
    assert "digitally signed Python" not in message
    assert "PRIVATE" not in message


def test_launcher_validation_error_keeps_actionable_reason():
    result = run_helpers("""
try { throw 'Port 8501 is being used by another application. Close that application or use another port.' }
catch { $failure = $_ }
$message = Format-StudentFlowLaunchError -Failure $failure -Stage 'Checking local service' -Target 'http://127.0.0.1:8501'
@{message=$message} | ConvertTo-Json -Compress
""")
    message = result["message"]
    assert "Stage: Checking local service" in message
    assert "Port 8501 is being used by another application" in message
    assert "HRESULT: 0x" in message
    assert "Windows error:" not in message
