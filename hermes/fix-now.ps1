#Requires -Version 5.1
<#
    fix-now.ps1 - restart the HermesAI / Kick notifier and judge success from NEW log bytes only.

    Why a file and not a chat block
    -------------------------------
    A pasted block keeps losing pieces: chat clients turn `kick_notifier.log` into a
    link, and dropped lines leave undefined variables ($app, $log). A .ps1 file has
    neither problem, and it can be re-run safely.

    What it does
    ------------
      1. greps the app for the REVOKED key (32cbd69e4b950bf97679) inside code/config,
         because if the key is hardcoded there, editing config.json can never work
      2. finds only the notifier processes (never `taskkill /IM python.exe /F`),
         shows their command lines and asks before killing
      3. finds the entry script, the python interpreter and the right working dir
      4. records the log size, starts the process, waits
      5. reads ONLY the bytes written after that offset -> verdict
      6. writes a one-line RESULT_JSON and a report next to the log

    Usage
    -----
      powershell -NoProfile -ExecutionPolicy Bypass -File fix-now.ps1
      powershell -NoProfile -ExecutionPolicy Bypass -File fix-now.ps1 -AppDir 'D:\HermesAI\OBS'
      powershell -NoProfile -ExecutionPolicy Bypass -File fix-now.ps1 -DryRun      # show, do nothing
      powershell -NoProfile -ExecutionPolicy Bypass -File fix-now.ps1 -NoKill      # start a second copy
      powershell -NoProfile -ExecutionPolicy Bypass -File fix-now.ps1 -WaitSeconds 60

    Exit codes: 0 = VERDICT OK, 1 = NO_FRESH_EVIDENCE, 2 = KEY_REJECTED or setup failure.
#>

[CmdletBinding()]
param(
    [string]$AppDir = 'D:\HermesAI\OBS',
    [string]$LogPath = '',
    [string]$EntryScript = '',
    [string]$PythonPath = '',
    [int]$WaitSeconds = 20,
    [int]$TailLines = 20,
    [switch]$NoKill,
    [switch]$Force,      # do not ask before stopping processes (for unattended runs)
    [switch]$DryRun,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$script:Warnings = 0
$script:Lines = New-Object System.Collections.Generic.List[string]

function Say {
    param([string]$Text, [string]$Color = 'Gray')
    $script:Lines.Add($Text)
    if (-not $Quiet) { Write-Host $Text -ForegroundColor $Color }
}

function Head {
    param([string]$Text)
    Say ''
    Say ('== ' + $Text) 'Cyan'
}

function Ok    { param([string]$Text) Say ('   [OK] ' + $Text) 'Green' }
function Warn  { param([string]$Text) $script:Warnings++; Say ('   [WARN] ' + $Text) 'Yellow' }
function Bad   { param([string]$Text) Say ('   [FAIL] ' + $Text) 'Red' }
function Info  { param([string]$Text) Say ('   ' + $Text) 'Gray' }

# --------------------------------------------------------------------------- setup

Head 'Environment'
Say ('app dir : ' + $AppDir)
Say ('powershell: ' + $PSVersionTable.PSVersion.ToString())

if (-not (Test-Path -LiteralPath $AppDir)) {
    Bad ('app dir not found: ' + $AppDir + '   (pass -AppDir)')
    exit 2
}

$filesDir = Join-Path $AppDir 'files'
if (-not (Test-Path -LiteralPath $filesDir)) { $filesDir = $AppDir }

if (-not $LogPath) {
    $candidates = @(
        (Join-Path $filesDir ('kick_' + 'notifier' + '.log')),
        (Join-Path $AppDir   ('kick_' + 'notifier' + '.log'))
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { $LogPath = $candidate; break }
    }
    if (-not $LogPath) {
        $found = @(Get-ChildItem -LiteralPath $AppDir -Filter '*.log' -Recurse -File -Depth 3 -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
        if ($found.Count -gt 0) { $LogPath = $found[0].FullName; Warn ('log path guessed: ' + $LogPath) }
    }
}
if (-not $LogPath) { Warn 'no log file found yet (will still restart and report stdout/stderr)' }
else { Ok ('log: ' + $LogPath) }

# --------------------------------------------------------------------------- 1) old key

Head '1/5 Old key inside code or config?'
$oldKey = '32cbd69e4b950bf97679'
$newKey = '34bf7a0ff419a2a775b9'
$extensions = @('.py', '.json', '.ini', '.yaml', '.yml', '.env', '.cfg', '.toml')
$hits = @()
$scan = @(Get-ChildItem -LiteralPath $AppDir -Recurse -File -Depth 2 -ErrorAction SilentlyContinue |
    Where-Object { ($extensions -contains $_.Extension) -and $_.Length -lt 2MB -and
                   $_.FullName -notmatch '\\(?:\.venv|venv|site-packages|__pycache__|\.git)\\' })
foreach ($file in $scan) {
    $match = Select-String -LiteralPath $file.FullName -Pattern $oldKey -SimpleMatch -ErrorAction SilentlyContinue
    if ($match) { $hits += $file.FullName }
}
if ($hits.Count -gt 0) {
    Bad ('the REVOKED key still appears in ' + $hits.Count + ' file(s) - a config edit cannot fix these:')
    foreach ($path in $hits) { Say ('        ' + $path) 'Red' }
    Say '        -> replace it there too, or the socket will keep sending the dead key.'
} else {
    Ok 'revoked key not found in code/config (good - the stale process is the problem)'
}

# --------------------------------------------------------------------------- 2) processes

Head '2/5 Notifier processes'
$procList = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^py' -and $_.CommandLine -and
                   ($_.CommandLine -match 'HermesAI|notifier|kick') })

if ($procList.Count -eq 0) {
    Warn 'no running notifier process found (it may already be dead)'
} else {
    Say ('found ' + $procList.Count + ' process(es):')
    foreach ($proc in $procList) {
        Say ('   pid ' + $proc.ProcessId + '  ' + $proc.Name + '  ' + $proc.CommandLine)
    }
}

# --------------------------------------------------------------------------- 3) entry / python / cwd

Head '3/5 Entry script, interpreter, working dir'
if ($EntryScript) {
    if (-not (Test-Path -LiteralPath $EntryScript)) { Bad ('entry script not found: ' + $EntryScript); exit 2 }
} else {
    $pyFiles = @(Get-ChildItem -LiteralPath $AppDir -Recurse -File -Depth 2 -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -eq '.py' -and $_.Length -lt 2MB -and
                       $_.FullName -notmatch '\\(?:\.venv|venv|site-packages|__pycache__|\.git)\\' })
    $best = $null
    $bestScore = -1
    foreach ($file in $pyFiles) {
        $score = 0
        if ($file.Name -match '^(kick_notifier|kick_notify|notifier|kick_bot|main|start|run|app)\.py$') { $score += 3 }
        elseif ($file.Name -match 'notifier|kick') { $score += 1 }
        if ($file.Name -match '^(test_|conftest)') { $score -= 3 }
        try {
            if (Select-String -LiteralPath $file.FullName -Pattern 'pusher:|ChatMessageEvent|KickWebSocket|kick_notifier' -Quiet -ErrorAction Stop) { $score += 2 }
            if (Select-String -LiteralPath $file.FullName -Pattern 'if __name__' -Quiet -ErrorAction Stop) { $score += 2 }
        } catch { }
        if ($score -gt $bestScore) { $bestScore = $score; $best = $file }
    }
    if (-not $best) {
        Bad 'cannot find any .py file - pass -EntryScript <path to the notifier script>'
        exit 2
    }
    $EntryScript = $best.FullName
    Ok ('entry (score ' + $bestScore + '): ' + $EntryScript)
}
if (-not (Test-Path -LiteralPath $EntryScript)) { Bad ('entry script not found: ' + $EntryScript); exit 2 }

if (-not $PythonPath) {
    $venvCandidates = @()
    foreach ($rel in @('venv', '.venv')) {
        foreach ($root in @($AppDir, (Split-Path -Parent $AppDir))) {
            $venvCandidates += (Join-Path (Join-Path (Join-Path $root $rel) 'Scripts') 'python.exe')
        }
    }
    foreach ($candidate in $venvCandidates) {
        if (Test-Path -LiteralPath $candidate) { $PythonPath = $candidate; break }
    }
    if (-not $PythonPath) {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) { $PythonPath = $command.Source }
    }
    if (-not $PythonPath) {
        $command = Get-Command py -ErrorAction SilentlyContinue
        if ($command) { $PythonPath = $command.Source }
    }
    if (-not $PythonPath) {
        Bad 'python not found - pass -PythonPath <path to python.exe>'
        exit 2
    }
}
if (-not (Test-Path -LiteralPath $PythonPath)) { Bad ('python not found: ' + $PythonPath); exit 2 }
Ok ('python: ' + $PythonPath)

# run from the folder that makes `from backend...` imports work
$workDir = $AppDir
$entryDir = Split-Path -Parent $EntryScript
$parentDir = Split-Path -Parent $entryDir
if (Test-Path -LiteralPath (Join-Path $parentDir 'backend')) { $workDir = $parentDir }
elseif (Test-Path -LiteralPath (Join-Path $entryDir 'backend')) { $workDir = $entryDir }
elseif (Test-Path -LiteralPath (Join-Path $AppDir 'backend')) { $workDir = $AppDir }
Ok ('working dir: ' + $workDir)

$outFile = Join-Path $filesDir ('hermes_' + 'run_out' + '.log')
$errFile = Join-Path $filesDir ('hermes_' + 'run_err' + '.log')

if ($DryRun) {
    Head 'Dry run'
    Info ('would stop : ' + $procList.Count + ' process(es)')
    Info ('would start: "' + $PythonPath + '" "' + $EntryScript + '"   (cwd=' + $workDir + ')')
    $offsetNow = 0
    if ($LogPath -and (Test-Path -LiteralPath $LogPath)) { $offsetNow = (Get-Item -LiteralPath $LogPath).Length }
    Info ('would read log bytes after offset: ' + $offsetNow)
    Info ('would write stdout to: ' + $outFile)
    Say ''
    Say 'RESULT_JSON ' + (@{ verdict = 'DRY_RUN'; entry = $EntryScript; python = $PythonPath; cwd = $workDir } | ConvertTo-Json -Compress)
    exit 0
}

# --------------------------------------------------------------------------- 4) restart

Head '4/5 Restart'
if ($procList.Count -gt 0 -and -not $NoKill) {
    $answer = 'y'
    if (-not $Force) {
        $answer = Read-Host ('   stop ' + $procList.Count + ' process(es)? (y/N)')
    }
    if ($answer -eq 'y') {
        foreach ($proc in $procList) {
            try {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                Ok ('stopped pid ' + $proc.ProcessId)
            } catch {
                Warn ('could not stop pid ' + $proc.ProcessId + ': ' + $_.Exception.Message)
            }
        }
        Start-Sleep -Seconds 2
    } else {
        Warn 'kept the old process(es) running - two copies may fight over the socket'
    }
} elseif ($NoKill) {
    Info 'kill skipped (-NoKill)'
}

$offset = 0
if ($LogPath -and (Test-Path -LiteralPath $LogPath)) {
    $offset = (Get-Item -LiteralPath $LogPath).Length
}
Ok ('log offset before restart: ' + $offset + ' bytes (only newer bytes count)')

$started = $null
try {
    $started = Start-Process -FilePath $PythonPath -ArgumentList ('"' + $EntryScript + '"') `
        -WorkingDirectory $workDir -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
        -PassThru -WindowStyle Hidden -ErrorAction Stop
} catch {
    Warn ('could not start hidden with redirects (' + $_.Exception.Message + ') - retrying plainly')
    try {
        $started = Start-Process -FilePath $PythonPath -ArgumentList ('"' + $EntryScript + '"') `
            -WorkingDirectory $workDir -PassThru -ErrorAction Stop
    } catch {
        Bad ('failed to start: ' + $_.Exception.Message)
        exit 2
    }
}
Ok ('started pid ' + $started.Id)
Info ('waiting ' + $WaitSeconds + ' s ...')
Start-Sleep -Seconds $WaitSeconds

# --------------------------------------------------------------------------- 5) fresh evidence

Head '5/5 Fresh log bytes'
$newText = ''
if ($LogPath -and (Test-Path -LiteralPath $LogPath)) {
    try {
        $stream = [IO.File]::Open($LogPath, 'Open', 'Read', 'ReadWrite')
        try {
            $stream.Seek($offset, 'Begin') | Out-Null
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
            $newText = $reader.ReadToEnd()
            $reader.Dispose()
        } finally {
            $stream.Dispose()
        }
    } catch {
        Warn ('could not read new log bytes: ' + $_.Exception.Message)
    }
}

$newLines = @($newText -split "`r?`n" | Where-Object { $_ -ne '' })
Ok ('new bytes: ' + $newText.Length + '   new lines: ' + $newLines.Count)
if ($newLines.Count -gt 0) {
    foreach ($line in ($newLines | Select-Object -Last $TailLines)) { Say ('   | ' + $line) }
} else {
    Info 'no new log lines - checking captured stdout/stderr of the new process:'
    foreach ($file in @($outFile, $errFile)) {
        if (Test-Path -LiteralPath $file) {
            Say ('   --- ' + $file + ' ---')
            $tail = @(Get-Content -LiteralPath $file -Tail $TailLines -ErrorAction SilentlyContinue)
            if ($tail.Count -eq 0) { Say '   (empty)' }
            foreach ($line in $tail) { Say ('   | ' + $line) }
        }
    }
}

# The app writes its log lines to the captured stderr/stdout of THIS process,
# not only to the log file. Both files are truncated by Start-Process, so
# everything in them is new. The verdict must read them too, otherwise a
# healthy run is reported as NO_FRESH_EVIDENCE.
# The child process still holds these files open, so read them with
# FileShare.ReadWrite (same as the log read above) and never let a locked
# file break the verdict.
$capturedText = ''
foreach ($file in @($outFile, $errFile)) {
    if (Test-Path -LiteralPath $file) {
        try {
            $fs = [IO.File]::Open($file, 'Open', 'Read', 'ReadWrite')
            try {
                $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8, $true)
                $capturedText += $sr.ReadToEnd() + "`n"
                $sr.Dispose()
            } finally {
                $fs.Dispose()
            }
        } catch {
            $capturedText += '(could not read ' + $file + ': ' + $_.Exception.Message + ')' + "`n"
        }
    }
}
$evidence = $newText + "`n" + $capturedText

$verdict = 'NO_FRESH_EVIDENCE'
if ($evidence -match 'pusher:error|\b40(01|03|09)\b') { $verdict = 'KEY_REJECTED' }
elseif ($evidence -match 'connection_established|subscription_succeeded|ChatMessageEvent|Subscribed to chatrooms') { $verdict = 'OK' }

$color = 'Yellow'
if ($verdict -eq 'OK') { $color = 'Green' }
elseif ($verdict -eq 'KEY_REJECTED') { $color = 'Red' }

Say ''
Say ('VERDICT: ' + $verdict) $color
if ($verdict -eq 'KEY_REJECTED') {
    Say '   the NEW process is still rejected - read the pusher:error line above:'
    Say '   * mentions the OLD key -> something still reads it (see step 1)'
    Say '   * mentions the NEW key -> Kick may have rotated again (x.com/notjoker)'
}
if ($verdict -eq 'NO_FRESH_EVIDENCE') {
    Say '   the app wrote nothing new - check the captured stdout/stderr above for an import error.'
}
Say ('RESULT_JSON ' + (@{
    verdict   = $verdict
    new_bytes = $newText.Length
    new_lines = $newLines.Count
    entry     = $EntryScript
    python    = $PythonPath
    cwd       = $workDir
    log       = $LogPath
    pid       = $started.Id
} | ConvertTo-Json -Compress))

$reportPath = Join-Path $filesDir ('hermes_' + 'fix_now_report' + '.txt')
try {
    $enc = New-Object System.Text.UTF8Encoding($true)
    [IO.File]::WriteAllText($reportPath, ($script:Lines -join "`r`n"), $enc)
    Say ''
    Say ('report: ' + $reportPath) 'DarkGray'
} catch {
    Warn ('could not write the report: ' + $_.Exception.Message)
}

if ($verdict -eq 'OK') { exit 0 }
if ($verdict -eq 'KEY_REJECTED') { exit 2 }
exit 1
