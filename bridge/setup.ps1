# One-command bring-up for the Friday bridge.
#
#   double-click setup.cmd      (or)      powershell -File setup.ps1
#
# Finds the python scripts by wildcard (so renames never break this), starts the
# bridge and the mock chat page in their own windows, waits for the token file,
# prints the token, and opens the mock page in your default browser.
#
# Nothing here touches Arena. It only proves the bridge + extension work.

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [int]$MockPort = 8799,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
if (-not $here) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Find-One {
    param([string]$Pattern)
    $found = Get-ChildItem -LiteralPath $here -Filter $Pattern -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $found) {
        throw ("not found: " + $Pattern + " in " + $here)
    }
    return $found.FullName
}

function Find-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$svc = Find-One 'arena*.py'
$mock = Find-One 'mock*.py'
$ext = Join-Path $here 'extension'

$repo = Split-Path -Parent $here
$tokenDir = Join-Path $repo 'bus'
$tokenDir = Join-Path $tokenDir '.local'
$tokenPath = Join-Path $tokenDir 'bridge.token'

if ($Stop) {
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | ForEach-Object {
        $line = $_.CommandLine
        if ($line -and ($line -like ('*' + $svc + '*') -or $line -like ('*' + $mock + '*'))) {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed = $killed + 1
        }
    }
    Write-Host ("stopped " + $killed + " bridge/mock process(es)")
    return
}

$py = Find-Python
if (-not $py) {
    Write-Host 'python was not found on PATH.' -ForegroundColor Red
    Write-Host 'Install Python 3 and tick "Add python.exe to PATH", then run this again.'
    exit 1
}

if (-not (Test-Path -LiteralPath $ext)) {
    Write-Host ('extension folder missing: ' + $ext) -ForegroundColor Red
    exit 1
}

Write-Host 'Friday bridge setup'
Write-Host ('  python    : ' + $py)
Write-Host ('  bridge    : ' + $svc)
Write-Host ('  mock page : ' + $mock)
Write-Host ''

# -- start the bridge in its own window (it must outlive this script)
$bridgeLine = '"' + $svc + '" serve --port ' + $Port
Start-Process -FilePath $py -ArgumentList $bridgeLine -WorkingDirectory $here
Write-Host ('started bridge on port ' + $Port)

# -- wait for it to create the token file
$deadline = (Get-Date).AddSeconds(25)
while ((-not (Test-Path -LiteralPath $tokenPath)) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Milliseconds 300
}

if (-not (Test-Path -LiteralPath $tokenPath)) {
    Write-Host 'the token file did not appear - look at the bridge window for an error' -ForegroundColor Red
    exit 1
}

$token = Get-Content -LiteralPath $tokenPath -Raw
$token = $token.Trim()

# -- start the mock page
$mockLine = '"' + $mock + '" --port ' + $MockPort
Start-Process -FilePath $py -ArgumentList $mockLine -WorkingDirectory $here
Start-Sleep -Milliseconds 800
Write-Host ('started mock chat on port ' + $MockPort)

$url = 'http' + '://' + '127.0.0.1' + ':' + $MockPort

Write-Host ''
Write-Host '--------------------------------------------------------------'
Write-Host 'TOKEN - paste this into the extension popup:'
Write-Host ''
Write-Host ('  ' + $token)
Write-Host ''
Write-Host 'THEN:'
Write-Host ('  1. load the extension: ' + $ext)
Write-Host '  2. open the page below and press "Attach to this tab"'
Write-Host ('  3. run:  python "' + $svc + '" ask "hello"')
Write-Host ''
Write-Host ('  ' + $url)
Write-Host '--------------------------------------------------------------'

if (-not $NoBrowser) {
    Start-Process $url
}
