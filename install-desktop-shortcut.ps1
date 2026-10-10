$ErrorActionPreference = 'Stop'
$launcherPath = Join-Path $PSScriptRoot 'launch.ps1'
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw 'Run this installer from the StudentFlow project folder.'
}

$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDirectory 'StudentFlow.lnk'
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if ((Test-Path -LiteralPath $shortcutPath) -and
        $shortcut.Arguments -notlike "*$launcherPath*") {
        throw 'A different StudentFlow shortcut already exists on the desktop. Rename it before running this installer.'
    }
    $shortcut.TargetPath = $powershellPath
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcherPath + '"'
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Open StudentFlow'
    $iconPath = Join-Path $PSScriptRoot 'assets\studentflow-icon.ico'
    if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
        $shortcut.IconLocation = $iconPath + ',0'
    }
    else {
        $shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',13'
    }
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Output "Created desktop shortcut: $shortcutPath"
}
finally {
    if ($shortcut) { [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) | Out-Null }
    [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null
}
