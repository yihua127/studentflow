param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8501,
    [switch]$NoBrowser,
    [switch]$NoDialog,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$appPath = Join-Path $projectDirectory 'app.py'
$pythonPath = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$dataDirectory = Join-Path $projectDirectory 'data'
$appUrl = "http://127.0.0.1:$Port"
$logPath = Join-Path $dataDirectory "launcher.$Port.stderr.log"
$versionPath = Join-Path $dataDirectory "launcher.$Port.version.json"
$launchMutex = $null
$ownsMutex = $false
$launchStage = 'Checking project files'
$launchTarget = $appPath

function Format-StudentFlowLaunchError {
    param(
        [System.Management.Automation.ErrorRecord]$Failure,
        [string]$Stage,
        [string]$Target
    )
    # Inspect only exception codes. Never dump invocation arguments, environment,
    # ErrorRecord.TargetObject, or Exception.Data into a message or log.
    $exception = $Failure.Exception
    $hresult = '0x{0:X8}' -f $exception.HResult
    $nativeCode = $null
    for ($depth = 0; $exception -and $depth -lt 16; $depth++) {
        $codeHex = '0x{0:X8}' -f $exception.HResult
        if ($exception -is [System.ComponentModel.Win32Exception]) {
            $nativeCode = $exception.NativeErrorCode
            $hresult = $codeHex
            break
        }
        # Some wrappers retain HRESULT_FROM_WIN32 without a Win32Exception.
        if ($codeHex -match '^0x8007([0-9A-Fa-f]{4})$') {
            $nativeCode = [Convert]::ToInt32($Matches[1], 16)
            $hresult = $codeHex
        }
        $exception = $exception.InnerException
    }
    $lines = @(
        'StudentFlow could not complete startup.',
        "Stage: $Stage",
        "Target: $Target",
        "HRESULT: $hresult"
    )
    if ($null -ne $nativeCode) { $lines += "Windows error: $nativeCode" }
    if (@(577, 1260, 4551) -contains $nativeCode) {
        if ($nativeCode -eq 577) {
            $lines += 'Windows could not verify the digital signature of a required file.'
        }
        else { $lines += 'Windows application control blocked the launch.' }
        if ($Stage -eq 'Starting Python') {
            $lines += 'Rebuild the StudentFlow environment with an official, digitally signed Python installation trusted by this device. Ask the device administrator if approval is required.'
        }
        elseif ($Stage -eq 'Opening browser') {
            $lines += 'The local app is ready. Open its address in an approved browser, or ask the device administrator to review the blocked browser.'
        }
        else { $lines += 'Ask the device administrator to review the blocked file.' }
        $lines += 'Keep Windows security protections enabled.'
    }
    else {
        # Launcher and Windows errors contain the useful failure reason. Do not
        # serialize the ErrorRecord, which can include the whole invocation.
        $lines += "Details: $($Failure.Exception.Message)"
    }
    return $lines -join [Environment]::NewLine
}

function Get-LocalListener {
    # IPGlobalProperties works in ordinary user accounts, including PS 5.1.
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0
}

function Assert-StudentFlowListener {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        Select-Object -First 1
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
    if (-not $owner -or $owner.CommandLine -notmatch [regex]::Escape($appPath) -or
        $owner.CommandLine -notmatch '\bstreamlit\b') {
        throw "Port $Port is being used by another application. Close that application or use another port."
    }
    return $connection.OwningProcess
}

function Get-StudentFlowCodeFingerprint {
    param([string]$Root)
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]'\/')
    $files = @(
        foreach ($relative in @('app.py', 'pyproject.toml', 'uv.lock', '.streamlit\config.toml')) {
            $candidate = Join-Path $rootPath $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Get-Item -LiteralPath $candidate
            }
        }
        $sourcePath = Join-Path $rootPath 'src\studentflow'
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            Get-ChildItem -LiteralPath $sourcePath -Filter '*.py' -File -Recurse
        }
    )
    $entries = [string[]]@(
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($rootPath.Length).TrimStart([char[]]'\/').Replace('\', '/')
            $fileHasher = [System.Security.Cryptography.SHA256]::Create()
            $stream = [System.IO.File]::OpenRead($file.FullName)
            try {
                $fileHash = ([System.BitConverter]::ToString($fileHasher.ComputeHash($stream))).Replace('-', '')
            }
            finally {
                $stream.Dispose()
                $fileHasher.Dispose()
            }
            "$relative`:$fileHash"
        }
    )
    [Array]::Sort($entries, [StringComparer]::Ordinal)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes(($entries -join "`n")))
        return ([System.BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
    }
    finally { $hasher.Dispose() }
}

function Test-StudentFlowLaunchVersion {
    param([string]$Path, [int]$ProcessId, [string]$Fingerprint)
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
        $saved = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return ($saved.schema_version -eq 1 -and [string]$saved.process_id -eq [string]$ProcessId -and
                $saved.code_fingerprint -eq $Fingerprint)
    }
    catch { return $false }
}

function Write-StudentFlowLaunchVersion {
    param([string]$Path, [int]$ProcessId, [string]$Fingerprint)
    $saved = [ordered]@{
        schema_version = 1
        process_id = $ProcessId
        code_fingerprint = $Fingerprint
        saved_at = [DateTime]::UtcNow.ToString('o')
    }
    $temporaryPath = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporaryPath, ($saved | ConvertTo-Json), [System.Text.Encoding]::UTF8)
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Stop-VerifiedStudentFlowListener {
    param([int]$ExpectedProcessId)
    # Recheck ownership immediately before stopping; never trust a saved PID alone.
    $currentProcessId = Assert-StudentFlowListener
    if ($currentProcessId -ne $ExpectedProcessId) {
        throw 'The local application changed while restarting. Open StudentFlow again.'
    }
    Stop-Process -Id $ExpectedProcessId -Force -ErrorAction Stop
    $shutdownDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (Get-LocalListener) {
        try { $remainingProcessId = Assert-StudentFlowListener }
        catch {
            # The listener may disappear between the socket snapshot and owner
            # lookup. Confirm that it is gone; an occupied port still fails closed.
            if (-not (Get-LocalListener)) { break }
            throw
        }
        if ($remainingProcessId -ne $ExpectedProcessId) {
            throw 'The local application changed while restarting. Open StudentFlow again.'
        }
        if ([DateTime]::UtcNow -ge $shutdownDeadline) {
            throw 'StudentFlow could not close for the update. Close it and try again.'
        }
        Start-Sleep -Milliseconds 200
    }
}

function Test-StudentFlowReady {
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("$appUrl/_stcore/health")
        $request.Proxy = $null
        $request.Timeout = 1500
        $request.ReadWriteTimeout = 1500
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { return $response.StatusCode -eq 200 -and $reader.ReadToEnd().Trim() -eq 'ok' }
        finally { $reader.Dispose() }
    }
    catch { return $false }
    finally { if ($response) { $response.Dispose() } }
}

try {
    if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
        throw 'StudentFlow could not be found. Create the desktop shortcut again from the project folder.'
    }
    $launchStage = 'Waiting for another startup'
    $launchTarget = $PSCommandPath
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$projectDirectory|$Port".ToLowerInvariant()))
        $mutexSuffix = ([System.BitConverter]::ToString($digest)).Replace('-', '')
    }
    finally { $hasher.Dispose() }
    $launchMutex = New-Object System.Threading.Mutex($false, "Local\StudentFlow-$mutexSuffix")
    try { $ownsMutex = $launchMutex.WaitOne(45000) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'StudentFlow is still starting. Please try again in a moment.' }

    $startedProcess = $null
    $launchStage = 'Checking application version'
    $launchTarget = $projectDirectory
    $codeFingerprint = Get-StudentFlowCodeFingerprint -Root $projectDirectory
    $launchStage = 'Checking local service'
    $launchTarget = $appUrl
    if (Get-LocalListener) {
        $serverProcessId = Assert-StudentFlowListener
        if ($Restart -or -not (Test-StudentFlowLaunchVersion -Path $versionPath `
                -ProcessId $serverProcessId -Fingerprint $codeFingerprint)) {
            $launchStage = 'Checking Python environment'
            $launchTarget = $pythonPath
            if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
                throw 'The StudentFlow environment is missing. Run uv sync --locked in the project folder, then open StudentFlow again.'
            }
            $launchStage = 'Restarting local service'
            Stop-VerifiedStudentFlowListener -ExpectedProcessId $serverProcessId
        }
    }
    if (-not (Get-LocalListener)) {
        $launchStage = 'Checking Python environment'
        $launchTarget = $pythonPath
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw 'The StudentFlow environment is missing. Run uv sync --locked in the project folder, then open StudentFlow again.'
        }
        $launchStage = 'Preparing startup logs'
        $launchTarget = $dataDirectory
        [System.IO.Directory]::CreateDirectory($dataDirectory) | Out-Null
        $appArguments = @(
            '-m', 'streamlit', 'run', ('"' + $appPath + '"'),
            '--server.address', '127.0.0.1', '--server.port', "$Port",
            '--server.headless', 'true', '--browser.gatherUsageStats', 'false'
        )
        $launchStage = 'Starting Python'
        $launchTarget = $pythonPath
        $startedProcess = Start-Process -FilePath $pythonPath -ArgumentList $appArguments `
            -WorkingDirectory $projectDirectory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataDirectory "launcher.$Port.stdout.log") `
            -RedirectStandardError $logPath
    }

    $launchStage = 'Waiting for application'
    $launchTarget = $pythonPath
    $deadline = [DateTime]::UtcNow.AddSeconds(35)
    while (-not (Test-StudentFlowReady)) {
        if ($startedProcess -and $startedProcess.HasExited) {
            throw "StudentFlow could not start. Startup details are saved in: $logPath"
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "StudentFlow is taking longer than expected to start. Startup details are saved in: $logPath"
        }
        Start-Sleep -Milliseconds 400
    }
    $launchStage = 'Checking local service'
    $launchTarget = $appUrl
    $serverProcessId = Assert-StudentFlowListener
    $launchStage = 'Saving startup version'
    $launchTarget = $versionPath
    [System.IO.Directory]::CreateDirectory($dataDirectory) | Out-Null
    Write-StudentFlowLaunchVersion -Path $versionPath -ProcessId $serverProcessId -Fingerprint $codeFingerprint
    Write-Output "StudentFlow is ready at $appUrl (process $serverProcessId)."
    if (-not $NoBrowser) {
        $launchStage = 'Opening browser'
        $launchTarget = $appUrl
        Start-Process -FilePath $appUrl | Out-Null
    }
}
catch {
    $message = Format-StudentFlowLaunchError -Failure $_ -Stage $launchStage -Target $launchTarget
    if ($NoDialog) { Write-Error $message -ErrorAction Continue }
    else {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $message, 'StudentFlow', [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
    exit 1
}
finally {
    if ($ownsMutex) { $launchMutex.ReleaseMutex() }
    if ($launchMutex) { $launchMutex.Dispose() }
}
