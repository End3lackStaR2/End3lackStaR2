# One-command bring-up for the Friday bridge.
#
#   double-click setup.cmd          <- recommended, it bypasses the script policy
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#
# "./setup.ps1" alone fails on a stock Windows machine:
#   "cannot be loaded because running scripts is disabled on this system"
# (Windows client default policy is Restricted). setup.cmd handles that for you.
#
# What it does: finds the python scripts by wildcard (so renames never break it),
# starts the bridge and the mock chat page in their own windows, waits for the
# token file, VERIFIES the bridge actually answers with that token, prints the
# token, and opens the mock page.

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [int]$MockPort = 8799,
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$Ask,
    [string]$AskText = 'hello from setup',
    [int]$AskWait = 120
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

function Test-Port {
    param([int]$Number)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Number, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-Health {
    param([int]$Number, [string]$Token)
    $headers = @{ 'X-Bridge-Token' = $Token }
    $url = 'http' + '://' + '127.0.0.1' + ':' + $Number + '/health'
    try {
        return Invoke-RestMethod -Uri $url -Headers $headers -Method GET -TimeoutSec 5
    } catch {
        return $null
    }
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

$reused = $false

# -- if the port is already taken, find out whether it is ours
if (Test-Port $Port) {
    $existing = $null
    if (Test-Path -LiteralPath $tokenPath) {
        $existing = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    }
    if ($existing) {
        $probe = Get-Health $Port $existing
        if ($probe -and $probe.ok) {
            Write-Host ('a bridge is already running on port ' + $Port + ' and accepts the saved token.') -ForegroundColor Yellow
            Write-Host 'Reusing it. Run this script with -Stop first if you want a fresh one.'
            $reused = $true
        }
    }
    if (-not $reused) {
        Write-Host ('port ' + $Port + ' is already in use by something else.') -ForegroundColor Red
        Write-Host ('Close it, or run:  setup.cmd -Port ' + ($Port + 1))
        exit 1
    }
}

if (-not $reused) {
    # -- start the bridge in its own window (it must outlive this script)
    $bridgeLine = '"' + $svc + '" serve --port ' + $Port
    Start-Process -FilePath $py -ArgumentList $bridgeLine -WorkingDirectory $here
    Write-Host ('started bridge on port ' + $Port)
}

# -- wait for the token file
$deadline = (Get-Date).AddSeconds(25)
while ((-not (Test-Path -LiteralPath $tokenPath)) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Milliseconds 300
}

if (-not (Test-Path -LiteralPath $tokenPath)) {
    Write-Host 'the token file did not appear - look at the bridge window for an error' -ForegroundColor Red
    exit 1
}

$token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()

# -- prove it really answers before telling the user anything
$healthy = $null
$deadline = (Get-Date).AddSeconds(15)
while ((-not $healthy) -and ((Get-Date) -lt $deadline)) {
    $healthy = Get-Health $Port $token
    if (-not $healthy) { Start-Sleep -Milliseconds 400 }
}

if (-not $healthy) {
    Write-Host ''
    Write-Host ('the bridge started but does not answer on port ' + $Port + '.') -ForegroundColor Red
    Write-Host 'Look at its window. Usual causes: the port is taken, Python is not allowed'
    Write-Host 'through the firewall, or the wrong token file is being read.'
    exit 1
}
Write-Host ('bridge verified: http://127.0.0.1:' + $Port + ' answered /health')

# -- start the mock page
if (-not (Test-Port $MockPort)) {
    $mockLine = '"' + $mock + '" --port ' + $MockPort
    Start-Process -FilePath $py -ArgumentList $mockLine -WorkingDirectory $here
    Start-Sleep -Milliseconds 800
}
Write-Host ('mock chat on port ' + $MockPort)

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
Write-Host ('  3. run:  setup.cmd -Ask hello')
Write-Host ''
Write-Host ('  ' + $url)
Write-Host '--------------------------------------------------------------'

if (-not $NoBrowser) {
    Start-Process $url
}

if ($Ask) {
    Write-Host ''
    Write-Host ('asking through the bridge (waiting up to ' + $AskWait + 's)...')
    Write-Host 'this only works after you pressed "Attach to this tab" in the extension'
    & $py $svc ask $AskText --wait $AskWait --port $Port --token-file $tokenPath
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'PROVEN: bridge + extension + page round trip works.' -ForegroundColor Green
    } else {
        Write-Host 'no answer - is the tab attached? see the checklist above.' -ForegroundColor Yellow
        exit 1
    }
}
