param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8501,
    [switch]$NoBrowser,
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$appPath = Join-Path $projectDirectory 'app.py'
$pythonPath = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$dataDirectory = Join-Path $projectDirectory 'data'
$appUrl = "http://127.0.0.1:$Port"
$logPath = Join-Path $dataDirectory "launcher.$Port.stderr.log"
$launchMutex = $null
$ownsMutex = $false

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
    if (Get-LocalListener) {
        $serverProcessId = Assert-StudentFlowListener
    }
    else {
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw 'The StudentFlow environment is missing. Run uv sync --locked in the project folder, then open StudentFlow again.'
        }
        [System.IO.Directory]::CreateDirectory($dataDirectory) | Out-Null
        $appArguments = @(
            '-m', 'streamlit', 'run', ('"' + $appPath + '"'),
            '--server.address', '127.0.0.1', '--server.port', "$Port",
            '--server.headless', 'true', '--browser.gatherUsageStats', 'false'
        )
        $startedProcess = Start-Process -FilePath $pythonPath -ArgumentList $appArguments `
            -WorkingDirectory $projectDirectory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataDirectory "launcher.$Port.stdout.log") `
            -RedirectStandardError $logPath
    }

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
    $serverProcessId = Assert-StudentFlowListener
    Write-Output "StudentFlow is ready at $appUrl (process $serverProcessId)."
    if (-not $NoBrowser) { Start-Process -FilePath $appUrl | Out-Null }
}
catch {
    $message = $_.Exception.Message
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
