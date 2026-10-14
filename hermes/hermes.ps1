#Requires -Version 5.1
<#
    hermes.ps1 - HermesAI / Kick notifier: fix, verify, restart, report.

    Why this exists
    ---------------
    Kick rotated the public Pusher app key. The old key
    (32cbd69e4b950bf97679) is being revoked; the replacement key is
    34bf7a0ff419a2a775b9 on cluster us2.
    Source of truth: Kick staff announcement, x.com/notjoker, 2026-10-08.

    A key rotation fails in a very specific way inside the OBS app:
        pusher:error code=4001 "App key ... not in this cluster" then the
        socket closes and the worker reconnects in a loop forever.
    Rewriting config.json alone is not enough when the app is still running,
    and the log keeps OLD lines, so "tail the log" is not proof.

    What this script does instead
    -----------------------------
      1. Finds the REAL config.json / log file / python / notifier script
         (no more guessing paths).
      2. Opens a live WebSocket to Pusher and tests BOTH keys against us2.
         The winner is decided by evidence, not by a Twitter post alone.
      3. Writes kick_pusher.json (both keys, primary first) and patches
         config.json in place (backup kept).
      4. Stops the stale python process and restarts it with the SAME
         command line it was launched with.
      5. Reads only log bytes written AFTER the restart and returns a
         verdict: OK / KEY_REJECTED / NO_FRESH_EVIDENCE.
      6. Writes hermes_report.json + hermes_report_TH.txt so the next person
         (or AI agent) does not have to re-litigate the same evidence.

    Modes
    -----
      fix     (default) full repair + verify
      verify  live key probe + log evidence, changes nothing
      doctor  read-only diagnosis (paths, keys, config, log, process, token)
      logs    print the resolved log path and tail it
      report  re-write the report files from the current log
      watch   loop: if a key is rejected again, rotate + restart automatically

    Examples
    --------
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 fix
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 doctor
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 fix -DryRun
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 logs -TailLines 40
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 fix -Thai
      powershell -NoProfile -ExecutionPolicy Bypass -File hermes.ps1 -SelfTest

    Exit codes: 0 = OK, 1 = warnings / needs a human, 2 = failure.
#>

[CmdletBinding()]
param(
    [ValidateSet('fix', 'verify', 'doctor', 'logs', 'report', 'watch')]
    [string]$Mode = 'fix',

    [string]$InstallDir = 'D:\HermesAI\OBS',

    # Optional explicit overrides. Empty = auto-detect.
    [string]$LogPath = '',
    [string]$ConfigPath = '',
    [string]$PythonPath = '',
    [string]$LaunchCommand = '',

    # Keys under test. Defaults come from the Kick announcement (2026-10-08).
    [string]$Key = '34bf7a0ff419a2a775b9',
    [string]$LegacyKey = '32cbd69e4b950bf97679',
    [string]$Cluster = 'us2',

    [int]$WaitSeconds = 45,
    [int]$TailLines = 20,
    [int]$Interval = 20,

    [switch]$DryRun,
    [switch]$NoRestart,
    [switch]$NoConfigPatch,
    [switch]$Thai,
    [switch]$Open,
    [switch]$Quiet,
    [switch]$SelfTest,
    [switch]$Online
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }
try { Add-Type -AssemblyName System.Net.WebSockets.Client -ErrorAction Stop } catch { }

# ---------------------------------------------------------------------------
# State + tiny output layer
# ---------------------------------------------------------------------------

$SCRIPT:Version = '1.0.0'
$SCRIPT:SourceNote = 'Kick staff announcement (x.com/notjoker, 2026-10-08): old Pusher app key revoked, new key 34bf7a0ff419a2a775b9 on cluster us2.'
$SCRIPT:UseThai = [bool]$Thai
$SCRIPT:En = New-Object System.Collections.Generic.List[string]
$SCRIPT:Th = New-Object System.Collections.Generic.List[string]
$SCRIPT:WarnCount = 0
$SCRIPT:FailCount = 0
$SCRIPT:Evidence = @{}

function Get-T {
    param([string]$En, [string]$Th)
    if ($SCRIPT:UseThai) { return $Th }
    return $En
}

function Write-Line {
    param([string]$Text, [string]$Color = 'Gray')
    if (-not $Quiet) { Write-Host $Text -ForegroundColor $Color }
}

function Step {
    param([string]$En, [string]$Th)
    $SCRIPT:En.Add("")
    $SCRIPT:En.Add("== $En")
    $SCRIPT:Th.Add("")
    $SCRIPT:Th.Add("== $Th")
    Write-Line ("`n== " + (Get-T $En $Th)) 'Cyan'
}

function Info {
    param([string]$En, [string]$Th)
    $SCRIPT:En.Add("   $En")
    $SCRIPT:Th.Add("   $Th")
    Write-Line ("   " + (Get-T $En $Th)) 'Gray'
}

function Ok {
    param([string]$En, [string]$Th)
    $SCRIPT:En.Add("   [OK] $En")
    $SCRIPT:Th.Add("   [OK] $Th")
    Write-Line ("   [OK] " + (Get-T $En $Th)) 'Green'
}

function Warn {
    param([string]$En, [string]$Th)
    $SCRIPT:WarnCount++
    $SCRIPT:En.Add("   [WARN] $En")
    $SCRIPT:Th.Add("   [WARN] $Th")
    Write-Line ("   [WARN] " + (Get-T $En $Th)) 'Yellow'
}

function Fail {
    param([string]$En, [string]$Th)
    $SCRIPT:FailCount++
    $SCRIPT:En.Add("   [FAIL] $En")
    $SCRIPT:Th.Add("   [FAIL] $Th")
    Write-Line ("   [FAIL] " + (Get-T $En $Th)) 'Red'
}

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

function New-Segment {
    param([byte[]]$Bytes)
    return (New-Object 'System.ArraySegment[byte]' -ArgumentList @(, $Bytes))
}

function Write-TextFile {
    param([string]$Path, [string]$Text, [switch]$Bom)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $enc = [System.Text.UTF8Encoding]::new([bool]$Bom.IsPresent)
    [IO.File]::WriteAllText($Path, $Text, $enc)
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = [IO.File]::ReadAllText($Path)
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Split-CommandLine {
    param([string]$CommandLine)
    $cl = ''
    if ($CommandLine) { $cl = $CommandLine.Trim() }
    if ($cl.Length -eq 0) { return @{ Exe = ''; Args = '' } }
    if ($cl.StartsWith('"')) {
        $end = $cl.IndexOf('"', 1)
        if ($end -lt 0) { return @{ Exe = $cl.Trim('"'); Args = '' } }
        return @{ Exe = $cl.Substring(1, $end - 1); Args = $cl.Substring($end + 1).Trim() }
    }
    $sp = $cl.IndexOf(' ')
    if ($sp -lt 0) { return @{ Exe = $cl; Args = '' } }
    return @{ Exe = $cl.Substring(0, $sp); Args = $cl.Substring($sp + 1).Trim() }
}

function Backup-File {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak = "$Path.bak-$stamp"
    try {
        Copy-Item -LiteralPath $Path -Destination $bak -Force
        return $bak
    } catch {
        return ''
    }
}

function Get-CacheFile {
    param([string]$InstallDir)
    return (Join-Path $InstallDir 'hermes.paths.json')
}

function Save-Cache {
    param([hashtable]$T)
    try {
        $cache = [ordered]@{
            updated_at_utc = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
            install_dir    = $T.InstallDir
            log_path       = $T.LogPath
            config_path    = $T.ConfigPath
            python_path    = $T.PythonPath
            script_path    = $T.ScriptPath
            cluster        = $T.Cluster
            keys           = @($T.PrimaryKey, $T.FallbackKey)
        }
        Write-TextFile -Path (Get-CacheFile $T.InstallDir) -Text ($cache | ConvertTo-Json -Depth 6)
    } catch { }
}

# ---------------------------------------------------------------------------
# Target resolution: paths, python, notifier script, launch command
# ---------------------------------------------------------------------------

function Get-NotifierProcesses {
    param([string]$InstallDir, [string]$ScriptLeaf)
    $result = New-Object System.Collections.Generic.List[object]
    $procs = @()
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name like 'py%'" -ErrorAction Stop)
    } catch {
        try { $procs = @(Get-Process | Where-Object { $_.ProcessName -match '^py' }) } catch { $procs = @() }
    }
    if ($procs.Count -eq 0) {
        # packaged builds (pyinstaller / nssm service / renamed exe): scan everything
        try { $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop) } catch { $procs = @() }
    }
    foreach ($p in $procs) {
        $cmd = $null
        if ($p.PSObject.Properties['CommandLine']) { $cmd = [string]$p.CommandLine }
        $name = [string]$p.Name
        if (-not $name) { $name = [string]$p.ProcessName }
        $match = $false
        if ($cmd) {
            if ($ScriptLeaf -and $cmd -match [regex]::Escape($ScriptLeaf)) { $match = $true }
            if ($cmd -match 'notifier' -and $InstallDir -and $cmd -match [regex]::Escape($InstallDir)) { $match = $true }
        }
        if ($match) {
            $result.Add([pscustomobject]@{ ProcessId = $p.ProcessId; Name = $name; CommandLine = $cmd })
        }
    }
    return $result
}

function Resolve-Targets {
    $t = @{}
    $t.Cluster = $Cluster
    $t.PrimaryKey = $Key.Trim()
    $t.FallbackKey = $LegacyKey.Trim()
    $t.InstallDir = $InstallDir
    $t.Notes = New-Object System.Collections.Generic.List[string]

    if (-not (Test-Path -LiteralPath $InstallDir)) {
        throw "Install dir not found: $InstallDir  (pass -InstallDir 'D:\HermesAI\OBS')"
    }
    $t.FilesDir = Join-Path $InstallDir 'files'
    if (-not (Test-Path -LiteralPath $t.FilesDir)) { $t.FilesDir = $InstallDir }
    $t.KeyFilePath = Join-Path $InstallDir 'kick_pusher.json'
    $t.LaunchFile = Join-Path $InstallDir 'hermes.launch.txt'
    $cache = Read-JsonFile (Get-CacheFile $InstallDir)

    # ---- running processes (also the best source for the launch command)
    $running = Get-NotifierProcesses -InstallDir $InstallDir -ScriptLeaf ''
    $t.Processes = $running

    # ---- log file
    $t.LogPath = ''
    $t.LogIsFallback = $false
    $cands = New-Object System.Collections.Generic.List[string]
    if ($LogPath) { $cands.Add($LogPath) }
    if ($cache -and $cache.log_path) { $cands.Add([string]$cache.log_path) }
    $cands.Add((Join-Path $t.FilesDir 'kick_notifier.log'))
    $cands.Add((Join-Path $InstallDir 'kick_notifier.log'))
    $cands.Add((Join-Path $InstallDir 'files\hermes_stdout.log'))
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c)) { $t.LogPath = $c; break }
    }
    if (-not $t.LogPath) {
        $logs = @()
        try {
            $logs = Get-ChildItem -LiteralPath $InstallDir -Filter '*.log' -Recurse -File -Depth 4 -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(\.venv|venv|node_modules|\.git|__pycache__)\\' } |
                Sort-Object LastWriteTime -Descending
        } catch { $logs = @() }
        if ($logs.Count -gt 0) {
            $pref = $logs | Where-Object { $_.Name -match '(notifier|kick)' } | Select-Object -First 1
            if ($pref) { $t.LogPath = $pref.FullName } else { $t.LogPath = $logs[0].FullName }
            $t.LogIsFallback = $true
        }
    }

    # ---- config file
    $t.ConfigPath = ''
    $t.ConfigIsFallback = $false
    $cc = New-Object System.Collections.Generic.List[string]
    if ($ConfigPath) { $cc.Add($ConfigPath) }
    if ($cache -and $cache.config_path) { $cc.Add([string]$cache.config_path) }
    $cc.Add((Join-Path $t.FilesDir 'config.json'))
    $cc.Add((Join-Path $InstallDir 'config.json'))
    foreach ($c in $cc) {
        if ($c -and (Test-Path -LiteralPath $c)) { $t.ConfigPath = $c; break }
    }
    if (-not $t.ConfigPath) {
        $cfgs = @()
        try {
            $cfgs = Get-ChildItem -LiteralPath $InstallDir -Filter 'config*.json' -Recurse -File -Depth 3 -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(\.venv|venv|node_modules|\.git|__pycache__)\\' } |
                Sort-Object LastWriteTime -Descending
        } catch { $cfgs = @() }
        if ($cfgs.Count -gt 0) { $t.ConfigPath = $cfgs[0].FullName; $t.ConfigIsFallback = $true }
    }

    # ---- python interpreter
    $t.PythonPath = ''
    $t.PythonSource = ''
    if ($PythonPath) {
        if (Test-Path -LiteralPath $PythonPath) { $t.PythonPath = $PythonPath; $t.PythonSource = 'parameter' }
    }
    if (-not $t.PythonPath -and $running.Count -gt 0) {
        foreach ($p in $running) {
            $split = Split-CommandLine $p.CommandLine
            if ($split.Exe -and (Test-Path -LiteralPath $split.Exe)) { $t.PythonPath = $split.Exe; $t.PythonSource = 'running process'; break }
        }
    }
    if (-not $t.PythonPath -and $cache -and $cache.python_path -and (Test-Path -LiteralPath ([string]$cache.python_path))) {
        $t.PythonPath = [string]$cache.python_path; $t.PythonSource = 'cache'
    }
    if (-not $t.PythonPath) {
        $venvs = @(
            (Join-Path $InstallDir 'venv\Scripts\python.exe'),
            (Join-Path $InstallDir '.venv\Scripts\python.exe'),
            (Join-Path (Split-Path $InstallDir -Parent) 'venv\Scripts\python.exe'),
            (Join-Path (Split-Path $InstallDir -Parent) '.venv\Scripts\python.exe')
        )
        foreach ($v in $venvs) {
            if (Test-Path -LiteralPath $v) { $t.PythonPath = $v; $t.PythonSource = 'venv'; break }
        }
    }
    if (-not $t.PythonPath) {
        foreach ($exe in @('python.exe', 'pythonw.exe', 'py.exe')) {
            $cmd = Get-Command $exe -ErrorAction SilentlyContinue
            if ($cmd) { $t.PythonPath = $cmd.Source; $t.PythonSource = 'PATH'; break }
        }
    }

    # ---- notifier script
    $t.ScriptPath = ''
    $t.ScriptSource = ''
    if ($running.Count -gt 0) {
        foreach ($p in $running) {
            $split = Split-CommandLine $p.CommandLine
            $m = [regex]::Match($split.Args, '([A-Za-z]:\\[^"]+?\.py|\.?[\\/][^"]*?\.py)')
            if ($m.Success) {
                $cand = $m.Groups[1].Value.Trim('"')
                if (Test-Path -LiteralPath $cand) { $t.ScriptPath = $cand; $t.ScriptSource = 'running process'; break }
            }
        }
    }
    if (-not $t.ScriptPath -and $cache -and $cache.script_path -and (Test-Path -LiteralPath ([string]$cache.script_path))) {
        $t.ScriptPath = [string]$cache.script_path; $t.ScriptSource = 'cache'
    }
    if (-not $t.ScriptPath) {
        $pyFiles = @()
        try {
            $pyFiles = Get-ChildItem -LiteralPath $InstallDir -Filter '*.py' -Recurse -File -Depth 3 -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(\.venv|venv|node_modules|\.git|__pycache__|site-packages)\\' -and $_.Length -lt 2MB }
        } catch { $pyFiles = @() }
        $hits = @()
        foreach ($f in $pyFiles) {
            try {
                if (Select-String -LiteralPath $f.FullName -Pattern 'KickWebSocket|ChatMessageEvent|pusher:subscribe|kick_notifier' -Quiet -ErrorAction Stop) {
                    $hits += $f
                }
            } catch { }
        }
        if ($hits.Count -gt 0) {
            $pref = $hits | Where-Object { $_.Name -match '(notifier|kick|main|bot|app)' } | Sort-Object Length -Descending | Select-Object -First 1
            if (-not $pref) { $pref = $hits | Sort-Object Length -Descending | Select-Object -First 1 }
            $t.ScriptPath = $pref.FullName
            $t.ScriptSource = 'scan'
        }
    }

    # ---- launch command (exe + args) for the restart
    $t.LaunchExe = ''
    $t.LaunchArgs = ''
    $t.LaunchSource = ''
    if ($LaunchCommand) {
        $split = Split-CommandLine $LaunchCommand
        $t.LaunchExe = $split.Exe; $t.LaunchArgs = $split.Args; $t.LaunchSource = 'parameter'
    }
    if ($running.Count -gt 0) {
        $split = Split-CommandLine ([string]$running[0].CommandLine)
        if ($split.Exe -and (Test-Path -LiteralPath $split.Exe)) {
            $t.LaunchExe = $split.Exe; $t.LaunchArgs = $split.Args; $t.LaunchSource = 'running process'
        }
    }
    if (-not $t.LaunchExe -and (Test-Path -LiteralPath $t.LaunchFile)) {
        $raw = ''
        try { $raw = ([IO.File]::ReadAllText($t.LaunchFile)).Trim() } catch { $raw = '' }
        if ($raw) {
            $split = Split-CommandLine $raw
            if ($split.Exe) { $t.LaunchExe = $split.Exe; $t.LaunchArgs = $split.Args; $t.LaunchSource = 'hermes.launch.txt' }
        }
    }
    if (-not $t.LaunchExe -and $t.PythonPath -and $t.ScriptPath) {
        $t.LaunchExe = $t.PythonPath
        $t.LaunchArgs = '"' + $t.ScriptPath + '"'
        $t.LaunchSource = 'python + script'
    }
    if ($t.LaunchExe) {
        try { Write-TextFile -Path $t.LaunchFile -Text ($t.LaunchExe + ' ' + $t.LaunchArgs).Trim() } catch { }
    }

    # ---- room id hint from the log (lets us also confirm the subscription)
    $t.RoomId = ''
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        try {
            $tail = @(Get-Content -LiteralPath $t.LogPath -Tail 400 -ErrorAction SilentlyContinue)
            for ($i = $tail.Count - 1; $i -ge 0; $i--) {
                $m = [regex]::Match([string]$tail[$i], 'room_id[=: ]+(\d+)')
                if ($m.Success) { $t.RoomId = $m.Groups[1].Value; break }
            }
        } catch { }
    }

    return $t
}

function Write-TargetSummary {
    param([hashtable]$T)
    if ($T.LogPath) {
        if (T.LogIsFallback) { Warn "log file guessed: $($T.LogPath)" "ไฟล์ log เดาเอา: $($T.LogPath)" }
        else { Ok "log file: $($T.LogPath)" "ไฟล์ log: $($T.LogPath)" }
        if (Test-Path -LiteralPath $T.LogPath) {
            $li = Get-Item -LiteralPath $T.LogPath
            Info ("log size {0} bytes, last written {1}" -f $li.Length, $li.LastWriteTime) ("log ขนาด {0} ไบต์, แก้ล่าสุด {1}" -f $li.Length, $li.LastWriteTime)
        }
    } else {
        Warn 'no log file found under the install dir' 'ไม่พบไฟล์ log ในโฟลเดอร์ติดตั้ง'
    }
    if ($T.ConfigPath) {
        if ($T.ConfigIsFallback) { Warn "config guessed: $($T.ConfigPath)" "config เดาเอา: $($T.ConfigPath)" }
        else { Ok "config: $($T.ConfigPath)" "config: $($T.ConfigPath)" }
    } else {
        Warn 'no config.json found (use -ConfigPath)' 'ไม่พบ config.json (ใช้ -ConfigPath ระบุเอง)'
    }
    if ($T.PythonPath) { Ok "python: $($T.PythonPath)  [$($T.PythonSource)]" "python: $($T.PythonPath)  [$($T.PythonSource)]" }
    else { Warn 'python not found - restart must be done by hand' 'ไม่พบ python - ต้องรีสตาร์ทเอง' }
    if ($T.ScriptPath) { Ok "notifier script: $($T.ScriptPath)  [$($T.ScriptSource)]" "สคริปต์ notifier: $($T.ScriptPath)  [$($T.ScriptSource)]" }
    else { Warn 'notifier script not found (pass -LaunchCommand)' 'ไม่พบสคริปต์ notifier (ใช้ -LaunchCommand ระบุคำสั่งรัน)' }
    if ($T.LaunchExe) { Info ("launch: {0} {1}  [{2}]" -f $T.LaunchExe, $T.LaunchArgs, $T.LaunchSource) ("คำสั่งรัน: {0} {1}  [{2}]" -f $T.LaunchExe, $T.LaunchArgs, $T.LaunchSource) }
    if ($T.Processes -and $T.Processes.Count -gt 0) {
        foreach ($p in $T.Processes) {
            Info ("running pid {0} ({1}): {2}" -f $p.ProcessId, $p.Name, (Get-Trimmed $p.CommandLine 160)) ("โปรเซสที่รันอยู่ pid {0} ({1}): {2}" -f $p.ProcessId, $p.Name, (Get-Trimmed $p.CommandLine 160))
        }
    } else {
        Info 'no running notifier python process detected' 'ไม่พบโปรเซส python ของ notifier ที่รันอยู่'
    }
    if ($T.RoomId) { Info "chatroom id seen in log: $($T.RoomId)" "chatroom id ที่เห็นใน log: $($T.RoomId)" }
}

function Get-Trimmed {
    param([string]$Text, [int]$Max = 200)
    if (-not $Text) { return '' }
    if ($Text.Length -le $Max) { return $Text }
    return $Text.Substring(0, $Max) + '...'
}

# ---------------------------------------------------------------------------
# Live Pusher probe (no python, no pip, no third party code)
# ---------------------------------------------------------------------------

function Test-PusherKey {
    param(
        [string]$ProbeKey,
        [string]$ProbeCluster,
        [int]$TimeoutSec = 12,
        [string]$Channel = ''
    )
    $res = [ordered]@{
        key = $ProbeKey
        cluster = $ProbeCluster
        url = ''
        connected = $false
        http_rejected = $false
        error_code = $null
        error_message = $null
        socket_id = $null
        subscribed = $false
        message_seen = $false
        ms = 0
    }
    $uri = "wss://ws-$($ProbeCluster).pusher.com/app/$($ProbeKey)?protocol=7&client=js&version=8.4.0&flash=false"
    $res.url = $uri
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $ws = $null
    $cts = $null
    try {
        $ws = New-Object System.Net.WebSockets.ClientWebSocket
        try { $ws.Options.KeepAliveInterval = [TimeSpan]::FromSeconds(20) } catch { }
        $cts = New-Object System.Threading.CancellationTokenSource
        $cts.CancelAfter($TimeoutSec * 1000)
        $ws.ConnectAsync([Uri]$uri, $cts.Token).GetAwaiter().GetResult() | Out-Null

        $buf = New-Object byte[] 16384
        $seg = New-Segment $buf
        $chunks = New-Object System.Collections.Generic.List[string]
        $subscribedSent = $false

        while ($true) {
            $r = $ws.ReceiveAsync($seg, $cts.Token).GetAwaiter().GetResult()
            if ($r.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { break }
            $text = [Text.Encoding]::UTF8.GetString($buf, 0, $r.Count)
            $chunks.Add($text)
            if (-not $r.EndOfMessage) { continue }
            $frame = ($chunks -join '')
            $chunks.Clear()
            $obj = $null
            try { $obj = $frame | ConvertFrom-Json } catch { $obj = $null }
            if ($null -eq $obj) { continue }
            $ev = [string]$obj.event
            if ($ev -eq 'pusher:connection_established') {
                $res.connected = $true
                $data = $obj.data
                if ($data -is [string]) { try { $data = $data | ConvertFrom-Json } catch { $data = $null } }
                if ($data -and $data.PSObject.Properties['socket_id']) { $res.socket_id = [string]$data.socket_id }
                if (-not $Channel) { break }
                if (-not $subscribedSent) {
                    $subscribedSent = $true
                    $payload = @{ event = 'pusher:subscribe'; data = @{ channel = $Channel } } | ConvertTo-Json -Compress -Depth 5
                    $sb = [Text.Encoding]::UTF8.GetBytes($payload)
                    $ws.SendAsync((New-Segment $sb), [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).GetAwaiter().GetResult() | Out-Null
                    $cts.CancelAfter([Math]::Max($TimeoutSec, 8) * 1000)
                }
            } elseif ($ev -eq 'pusher:error') {
                $d = $obj.data
                if ($d -is [string]) { try { $d = $d | ConvertFrom-Json } catch { $d = $null } }
                if ($d) {
                    if ($d.PSObject.Properties['code']) { $res.error_code = [int]$d.code }
                    if ($d.PSObject.Properties['message']) { $res.error_message = [string]$d.message }
                }
                break
            } elseif ($ev -eq 'pusher_internal:subscription_succeeded') {
                $res.subscribed = $true
                break
            } elseif ($ev -eq 'App\Events\ChatMessageEvent' -or $ev -like '*ChatMessageEvent*') {
                $res.message_seen = $true
                break
            }
        }
    } catch {
        $msg = [string]$_.Exception.Message
        if ($msg -match 'status code') { $res.http_rejected = $true }
        if (-not $res.connected) {
            if (-not $res.error_message) { $res.error_message = $msg }
        } else {
            $res.error_message = 'key accepted; no further frames within the timeout'
        }
    } finally {
        if ($ws) { try { $ws.Dispose() } catch { } }
        if ($cts) { try { $cts.Dispose() } catch { } }
    }
    $sw.Stop()
    $res.ms = [int]$sw.ElapsedMilliseconds
    return $res
}

function Write-ProbeResult {
    param($R, [string]$Role = 'primary')
    $tag = "[$($R.cluster)] $($R.key)"
    $isLegacy = ($Role -eq 'legacy')
    if ($R.connected) {
        Ok ("$tag -> CONNECTED (socket_id=$($R.socket_id), $($R.ms) ms)") ("$tag -> เชื่อมต่อได้ (socket_id=$($R.socket_id), $($R.ms) ms)")
        if ($R.subscribed) { Ok ("$tag -> subscription_succeeded") ("$tag -> subscription_succeeded") }
        if ($R.message_seen) { Ok ("$tag -> chat message received") ("$tag -> ได้ข้อความแชทแล้ว") }
    } elseif ($isLegacy) {
        Warn ("$tag -> not usable any more (code=$($R.error_code) $($R.error_message)) - expected, this is the revoked key") `
             ("$tag -> ใช้ไม่ได้แล้ว (code=$($R.error_code) $($R.error_message)) - คาดไว้แล้ว นี่คือ key ที่ถูกปิด")
    } elseif ($R.error_code) {
        Fail ("$tag -> pusher:error code=$($R.error_code) $($R.error_message)") ("$tag -> pusher:error code=$($R.error_code) $($R.error_message)")
    } elseif ($R.http_rejected) {
        Fail ("$tag -> handshake refused by Pusher ($($R.error_message))") ("$tag -> Pusher ปฏิเสธ handshake ($($R.error_message))")
    } else {
        Warn ("$tag -> no answer ($($R.error_message))") ("$tag -> ไม่มีคำตอบ ($($R.error_message))")
    }
}

function Invoke-KeyProbe {
    param([hashtable]$T)
    Step 'Key check against Pusher (live)' 'ตรวจ key กับ Pusher (สด)'
    Info "source: $SCRIPT:SourceNote" "ที่มา: $SCRIPT:SourceNote"
    $channel = ''
    if ($T.RoomId) { $channel = "chatrooms.$($T.RoomId).v2" }
    $primary = Test-PusherKey -ProbeKey $T.PrimaryKey -ProbeCluster $T.Cluster -Channel $channel
    Write-ProbeResult $primary 'primary'
    $fallback = Test-PusherKey -ProbeKey $T.FallbackKey -ProbeCluster $T.Cluster -Channel $channel
    Write-ProbeResult $fallback 'legacy'
    $SCRIPT:Evidence.PrimaryProbe = $primary
    $SCRIPT:Evidence.FallbackProbe = $fallback

    if ($primary.connected) { return $T.PrimaryKey }
    if ($fallback.connected) {
        Warn "new key failed but the legacy key still connects - keeping the new key as primary anyway (Kick says the old one is going away) but both are stored." `
             'key ใหม่ไม่ผ่าน แต่ key เก่ายังต่อได้ - ยังเก็บ key ใหม่เป็นตัวหลัก (Kick แจ้งว่า key เก่ากำลังถูกปิด) และเก็บทั้งสองตัว'
        return $T.PrimaryKey
    }
    return ''
}

# ---------------------------------------------------------------------------
# Writing key material: kick_pusher.json + config.json patch
# ---------------------------------------------------------------------------

function Set-KeyMaterial {
    param([hashtable]$T, [string]$WorkingKey)
    Step 'Key material (kick_pusher.json + config.json)' 'ข้อมูล key (kick_pusher.json + config.json)'
    $now = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
    $primaryStatus = 'unverified'
    if ($SCRIPT:Evidence.PrimaryProbe -and $SCRIPT:Evidence.PrimaryProbe.connected) { $primaryStatus = 'verified' }
    $fallbackStatus = 'legacy'
    if ($SCRIPT:Evidence.FallbackProbe -and $SCRIPT:Evidence.FallbackProbe.connected) { $fallbackStatus = 'verified-fallback' }

    $doc = [ordered]@{
        schema         = 'kick.pusher/v1'
        updated_at_utc = $now
        source         = $SCRIPT:SourceNote
        cluster        = $T.Cluster
        keys           = @(
            [ordered]@{ key = $T.PrimaryKey; cluster = $T.Cluster; role = 'primary'; status = $primaryStatus },
            [ordered]@{ key = $T.FallbackKey; cluster = $T.Cluster; role = 'fallback'; status = $fallbackStatus }
        )
        verified_key   = $WorkingKey
    }
    if ($DryRun) {
        Info "dry-run: would write $($T.KeyFilePath)" "dry-run: จะเขียน $($T.KeyFilePath)"
    } else {
        Write-TextFile -Path $T.KeyFilePath -Text ($doc | ConvertTo-Json -Depth 8)
        Ok "wrote $($T.KeyFilePath) (primary=$($T.PrimaryKey), fallback=$($T.FallbackKey))" "เขียน $($T.KeyFilePath) แล้ว (primary=$($T.PrimaryKey), fallback=$($T.FallbackKey))"
    }

    if ($NoConfigPatch) { Info 'config patch skipped (-NoConfigPatch)' 'ข้ามการแก้ config (-NoConfigPatch)'; return }
    if (-not $T.ConfigPath) { Warn 'no config.json to patch' 'ไม่มี config.json ให้แก้'; return }

    $raw = ''
    try { $raw = [IO.File]::ReadAllText($T.ConfigPath) } catch { Fail "cannot read $($T.ConfigPath): $($_.Exception.Message)" "อ่าน $($T.ConfigPath) ไม่ได้: $($_.Exception.Message)"; return }
    $orig = $raw
    $changes = New-Object System.Collections.Generic.List[string]

    if ($raw -match [regex]::Escape($T.LegacyKey)) {
        $raw = $raw.Replace($T.LegacyKey, $T.PrimaryKey)
        $changes.Add("legacy key -> primary key")
    }
    $fieldPattern = '("(?:app_key|pusher_key|pusherKey|PUSHER_KEY|kick_pusher_key|kickPusherKey)"\s*:\s*")([^"]*)(")'
    $raw = [regex]::Replace($raw, $fieldPattern, {
            param($m)
            if ($m.Groups[2].Value -ne $T.PrimaryKey) { $changes.Add("field $($m.Groups[1].Value.Trim('": ')) : $($m.Groups[2].Value) -> $($T.PrimaryKey)") }
            return $m.Groups[1].Value + $T.PrimaryKey + $m.Groups[3].Value
        })
    $clusterPattern = '("(?:cluster|pusher_cluster|pusherCluster)"\s*:\s*")([^"]*)(")'
    $raw = [regex]::Replace($raw, $clusterPattern, {
            param($m)
            if ($m.Groups[2].Value -ne $T.Cluster) { $changes.Add("cluster $($m.Groups[2].Value) -> $($T.Cluster)") }
            return $m.Groups[1].Value + $T.Cluster + $m.Groups[3].Value
        })

    if ($raw -eq $orig) {
        Ok "config.json already uses $($T.PrimaryKey) / cluster $($T.Cluster) - no change needed" "config.json ใช้ $($T.PrimaryKey) / cluster $($T.Cluster) อยู่แล้ว - ไม่ต้องแก้"
        return
    }
    $jsonValidation = $null
    try { $jsonValidation = $raw | ConvertFrom-Json } catch { $jsonValidation = $null }
    if ($null -eq $jsonValidation) {
        Fail 'patched config.json is not valid JSON - refusing to write' 'config.json ที่แก้แล้วไม่ใช่ JSON ที่ถูกต้อง - ไม่เขียนทับ'
        return
    }
    if ($DryRun) {
        Info "dry-run: would patch $($T.ConfigPath) ($($changes.Count) changes)" "dry-run: จะแก้ $($T.ConfigPath) ($($changes.Count) จุด)"
    } else {
        $bak = Backup-File $T.ConfigPath
        if (-not $bak) { Fail 'backup failed - aborting config patch' 'สำรองไฟล์ไม่สำเร็จ - ยกเลิกการแก้ config'; return }
        Write-TextFile -Path $T.ConfigPath -Text $raw
        Ok "patched config.json (backup: $(Split-Path $bak -Leaf))" "แก้ config.json แล้ว (ไฟล์สำรอง: $(Split-Path $bak -Leaf))"
    }
    foreach ($c in $changes) { Info "  - $c" "  - $c" }
    $SCRIPT:Evidence.ConfigChanges = $changes
}

function Test-LegacyKeyLeftover {
    param([hashtable]$T)
    $hits = @()
    foreach ($p in @($T.ConfigPath, $T.KeyFilePath)) {
        if ($p -and (Test-Path -LiteralPath $p)) {
            try {
                $c = Select-String -LiteralPath $p -Pattern ([regex]::Escape($T.LegacyKey)) -SimpleMatch -ErrorAction SilentlyContinue
                if ($c) { $hits += $p }
            } catch { }
        }
    }
    if ($hits.Count -gt 0) {
        Info ("legacy key still referenced in: " + ($hits -join ', ')) ("ยังมี key เก่าอยู่ใน: " + ($hits -join ', '))
    }
}

# ---------------------------------------------------------------------------
# Log reading + analysis
# ---------------------------------------------------------------------------

function Read-LogSince {
    param([string]$Path, [long]$Offset = 0, [int]$MaxBytes = 262144)
    $out = @{ Lines = @(); Offset = $Offset; Note = ''; Length = 0 }
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { $out.Note = 'missing'; return $out }
    $len = 0
    try { $len = (Get-Item -LiteralPath $Path).Length } catch { $out.Note = 'stat failed'; return $out }
    $out.Length = $len
    if ($len -lt $Offset) { $Offset = 0; $out.Note = 'log was truncated/rotated' }
    $start = $Offset
    if (($len - $start) -gt $MaxBytes) { $start = $len - $MaxBytes; $out.Note = 'read last 256 KB only' }
    $text = ''
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $fs.Seek($start, 'Begin') | Out-Null
            $sr = New-Object System.IO.StreamReader($fs, [Text.Encoding]::UTF8, $true)
            $text = $sr.ReadToEnd()
            $sr.Dispose()
        } finally { $fs.Dispose() }
    } catch {
        try { $text = (Get-Content -LiteralPath $Path -Tail 200 -ErrorAction Stop) -join "`n" } catch { $out.Note = 'unreadable: ' + $_.Exception.Message }
    }
    $lines = @()
    if ($text) { $lines = @($text -split "`r?`n" | Where-Object { $_ -ne '' }) }
    $out.Lines = $lines
    $out.Offset = $len
    return $out
}

function Analyze-LogText {
    param([string[]]$Lines)
    $a = [ordered]@{
        lines = 0
        key_rejected = 0
        key_rejected_samples = @()
        connected = 0
        subscribed = 0
        messages = 0
        room_check_failed = 0
        token_issues = 0
        token_samples = @()
        last_ok_line = $null
        last_error_line = $null
        first_timestamp = $null
        last_timestamp = $null
    }
    $rejRe = 'App key .*not in this cluster|Invalid app key|pusher:error.*\b(4001|4002|4003|4009)\b|\bcode[=: ]+(4001|4002|4003|4009)\b'
    $okRe = 'connection_established|Connected to Pusher|WebSocket connected|connection_success'
    $subRe = 'subscription_succeeded|Subscribed to channel'
    $msgRe = 'ChatMessageEvent'
    $roomRe = 'Failed to (resolve|fetch) (the )?(chatroom|channel)|channel .*not found|Cloudflare'
    $tokRe = '(?i)(token|Token)[^|]{0,40}(expired|invalid|401|403)|401 Unauthorized|403 Forbidden|Unauthorized'
    foreach ($ln in $Lines) {
        $l = [string]$ln
        if ($l.Trim().Length -eq 0) { continue }
        $a.lines++
        $ts = [regex]::Match($l, '(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})|(\d{2}:\d{2}:\d{2})')
        if ($ts.Success) {
            if (-not $a.first_timestamp) { $a.first_timestamp = $ts.Value }
            $a.last_timestamp = $ts.Value
        }
        if ($l -match $rejRe) {
            $a.key_rejected++
            if ($a.key_rejected_samples.Count -lt 3) { $a.key_rejected_samples += (Get-Trimmed $l 220) }
            $a.last_error_line = Get-Trimmed $l 220
        }
        if ($l -match $okRe) { $a.connected++; $a.last_ok_line = Get-Trimmed $l 220 }
        if ($l -match $subRe) { $a.subscribed++ ; $a.last_ok_line = Get-Trimmed $l 220 }
        if ($l -match $msgRe) { $a.messages++ ; $a.last_ok_line = Get-Trimmed $l 220 }
        if ($l -match $roomRe) { $a.room_check_failed++ }
        if ($l -match $tokRe) {
            $a.token_issues++
            if ($a.token_samples.Count -lt 3) { $a.token_samples += (Get-Trimmed $l 220) }
        }
    }
    return $a
}

function Show-LogAnalysis {
    param($A, [string]$Label = 'log')
    Info ("$Label lines analyzed: $($A.lines)") ("วิเคราะห์ $Label จำนวน $($A.lines) บรรทัด")
    if ($A.connected -gt 0) { Ok "${Label}: connection established events = $($A.connected)" "${Label}: พบ connection established = $($A.connected)" }
    if ($A.subscribed -gt 0) { Ok "${Label}: subscription_succeeded = $($A.subscribed)" "${Label}: subscription_succeeded = $($A.subscribed)" }
    if ($A.messages -gt 0) { Ok "${Label}: chat messages seen = $($A.messages)" "${Label}: พบข้อความแชท = $($A.messages)" }
    if ($A.key_rejected -gt 0) {
        Fail "${Label}: pusher key rejected $($A.key_rejected) time(s)" "${Label}: key ถูกปฏิเสธ $($A.key_rejected) ครั้ง"
        foreach ($s in $A.key_rejected_samples) { Info "  $s" "  $s" }
    }
    if ($A.token_issues -gt 0) {
        Warn "${Label}: token/401/403 related lines = $($A.token_issues)" "${Label}: บรรทัดเกี่ยวกับ token/401/403 = $($A.token_issues)"
        foreach ($s in $A.token_samples) { Info "  $s" "  $s" }
    }
    if ($A.room_check_failed -gt 0) { Warn "${Label}: chatroom lookup problems = $($A.room_check_failed)" "${Label}: ปัญหาการหา chatroom = $($A.room_check_failed)" }
    if ($A.last_ok_line) { Info "$Label last good line: $($A.last_ok_line)" "$Label บรรทัดดีล่าสุด: $($A.last_ok_line)" }
    if ($A.last_error_line) { Info "$Label last error line: $($A.last_error_line)" "$Label บรรทัด error ล่าสุด: $($A.last_error_line)" }
    if ($A.last_timestamp) { Info "$Label last timestamp: $($A.last_timestamp)" "$Label เวลาใน log ล่าสุด: $($A.last_timestamp)" }
}

function Get-LogClockHint {
    param([hashtable]$T)
    if (-not $T.LogPath -or -not (Test-Path -LiteralPath $T.LogPath)) { return '' }
    $slice = Read-LogSince -Path $T.LogPath -Offset 0 -MaxBytes 262144
    $a = Analyze-LogText -Lines $slice.Lines
    if (-not $a.last_timestamp) { return '' }
    $m = [regex]::Match($a.last_timestamp, '^(\d{2}):(\d{2}):(\d{2})$')
    if (-not $m.Success) { return 'log has full dates' }
    $now = Get-Date
    $asLocal = Get-Date -Hour ([int]$m.Groups[1].Value) -Minute ([int]$m.Groups[2].Value) -Second ([int]$m.Groups[3].Value)
    $asUtc = $asLocal.ToUniversalTime()
    $dLocal = [Math]::Abs(((New-TimeSpan -Start $asLocal -End $now).TotalMinutes))
    $dUtc = [Math]::Abs(((New-TimeSpan -Start $asUtc -End $now).TotalMinutes))
    if ($dLocal -lt 10 -and $dLocal -le $dUtc) { return 'log clock looks like LOCAL time' }
    if ($dUtc -lt 10) { return 'log clock looks like UTC time (log is ~' + [int]($now - [DateTime]::UtcNow).TotalHours + 'h behind local)' }
    return ('log clock drift vs local: {0:N0} min' -f $dLocal)
}

# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

function Stop-Notifier {
    param([hashtable]$T)
    $killed = @()
    $leaf = ''
    if ($T.ScriptPath) { $leaf = Split-Path -Leaf $T.ScriptPath }
    $targets = @(Get-NotifierProcesses -InstallDir $T.InstallDir -ScriptLeaf $leaf)
    if ($targets.Count -eq 0) { $targets = @($T.Processes) }
    if (-not $targets) { return $killed }
    foreach ($p in $targets) {
        if ($DryRun) { Info "dry-run: would stop pid $($p.ProcessId)" "dry-run: จะหยุด pid $($p.ProcessId)"; continue }
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            $killed += $p.ProcessId
            Ok "stopped pid $($p.ProcessId)" "หยุดโปรเซส pid $($p.ProcessId) แล้ว"
        } catch {
            Warn "could not stop pid $($p.ProcessId): $($_.Exception.Message)" "หยุด pid $($p.ProcessId) ไม่ได้: $($_.Exception.Message)"
        }
    }
    if ($killed.Count -gt 0) { Start-Sleep -Seconds 2 }
    return $killed
}

function Start-Notifier {
    param([hashtable]$T)
    if (-not $T.LaunchExe) {
        Warn 'no launch command available - start the notifier yourself' 'ไม่มีคำสั่งรัน - ต้องเปิด notifier เอง'
        return $null
    }
    $exe = $T.LaunchExe
    if ($exe -notmatch '\\' -and $exe -notmatch '^[A-Za-z]:') {
        $c = Get-Command $exe -ErrorAction SilentlyContinue
        if ($c) { $exe = $c.Source }
    }
    if (-not (Test-Path -LiteralPath $exe)) {
        Warn "launcher not found: $exe" "ไม่พบไฟล์รัน: $exe"
        return $null
    }
    $wd = $T.InstallDir
    if ($T.ScriptPath) {
        $sd = Split-Path -Parent $T.ScriptPath
        if ($sd) { $wd = $sd }
    }
    $outFile = Join-Path $T.FilesDir 'hermes_stdout.log'
    $errFile = Join-Path $T.FilesDir 'hermes_stderr.log'
    $argString = ''
    if ($T.LaunchArgs) { $argString = $T.LaunchArgs }
    if ($DryRun) {
        Info "dry-run: would start: `"$exe`" $argString   (cwd=$wd)" "dry-run: จะรัน: `"$exe`" $argString   (cwd=$wd)"
        return $null
    }
    try {
        if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $errFile) { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue }
        $params = @{
            FilePath         = $exe
            WorkingDirectory = $wd
            PassThru         = $true
            WindowStyle      = 'Hidden'
        }
        if ($argString) { $params['ArgumentList'] = $argString }
        if ($exe -notmatch 'pythonw') {
            $params['RedirectStandardOutput'] = $outFile
            $params['RedirectStandardError'] = $errFile
        }
        $p = Start-Process @params
        Ok "started: `"$exe`" $argString   (pid $($p.Id), cwd $wd)" "เริ่มใหม่: `"$exe`" $argString   (pid $($p.Id), cwd $wd)"
        Start-Sleep -Seconds 3
        return $p
    } catch {
        Fail "start failed: $($_.Exception.Message)" "เริ่มโปรเซสไม่สำเร็จ: $($_.Exception.Message)"
        return $null
    }
}

function Wait-ForEvidence {
    param([hashtable]$T, [long]$Offset, [int]$WaitSec)
    Step "Waiting for fresh log evidence (up to $WaitSec s)" "รอหลักฐานใหม่ใน log (สูงสุด $WaitSec วินาที)"
    $deadline = (Get-Date).AddSeconds($WaitSec)
    $acc = @()
    $lastNote = ''
    while ((Get-Date) -lt $deadline) {
        $slice = Read-LogSince -Path $T.LogPath -Offset $Offset
        if ($slice.Note -and $slice.Note -ne $lastNote) { Info "log read note: $($slice.Note)" "หมายเหตุการอ่าน log: $($slice.Note)"; $lastNote = $slice.Note }
        if ($slice.Lines.Count -gt 0) { $acc += $slice.Lines; $Offset = $slice.Offset }
        if ($acc.Count -gt 0) {
            $a = Analyze-LogText -Lines $acc
            if ($a.key_rejected -gt 0) { return @{ Verdict = 'KEY_REJECTED'; Analysis = $a; Lines = $acc } }
            if ($a.subscribed -gt 0 -or $a.messages -gt 0) { return @{ Verdict = 'OK'; Analysis = $a; Lines = $acc } }
            if ($a.connected -gt 0 -and ((Get-Date) -gt $deadline.AddSeconds(-0.35 * $WaitSec))) { return @{ Verdict = 'OK'; Analysis = $a; Lines = $acc } }
        }
        Start-Sleep -Seconds 3
    }
    if ($acc.Count -eq 0) { return @{ Verdict = 'NO_FRESH_EVIDENCE'; Analysis = (Analyze-LogText -Lines @()); Lines = @() } }
    $a = Analyze-LogText -Lines $acc
    if ($a.key_rejected -gt 0) { return @{ Verdict = 'KEY_REJECTED'; Analysis = $a; Lines = $acc } }
    if ($a.connected -gt 0 -or $a.subscribed -gt 0 -or $a.messages -gt 0) { return @{ Verdict = 'OK'; Analysis = $a; Lines = $acc } }
    return @{ Verdict = 'NO_FRESH_EVIDENCE'; Analysis = $a; Lines = $acc }
}

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

function Write-Reports {
    param([hashtable]$T, [string]$Verdict, $NewAnalysis, $OldAnalysis)
    $stamp = Get-Date
    $doc = [ordered]@{
        tool           = 'hermes.ps1'
        version        = $SCRIPT:Version
        ran_at_local   = $stamp.ToString('s')
        ran_at_utc     = $stamp.ToUniversalTime().ToString('s') + 'Z'
        mode           = $Mode
        dry_run        = [bool]$DryRun
        install_dir    = $T.InstallDir
        log_path       = $T.LogPath
        config_path    = $T.ConfigPath
        python_path    = $T.PythonPath
        script_path    = $T.ScriptPath
        launch         = ($T.LaunchExe + ' ' + $T.LaunchArgs).Trim()
        cluster        = $T.Cluster
        keys           = @{ primary = $T.PrimaryKey; fallback = $T.FallbackKey }
        probes         = @{ primary = $SCRIPT:Evidence.PrimaryProbe; fallback = $SCRIPT:Evidence.FallbackProbe }
        verdict        = $Verdict
        new_log        = $NewAnalysis
        old_log        = $OldAnalysis
        warnings       = $SCRIPT:WarnCount
        failures       = $SCRIPT:FailCount
    }
    $jsonPath = Join-Path $T.FilesDir 'hermes_report.json'
    try {
        Write-TextFile -Path $jsonPath -Text ($doc | ConvertTo-Json -Depth 10)
        Ok "report: $jsonPath" "รายงาน: $jsonPath"
    } catch { Warn "cannot write $jsonPath : $($_.Exception.Message)" "เขียน $jsonPath ไม่ได้: $($_.Exception.Message)" }

    $th = New-Object System.Collections.Generic.List[string]
    $th.Add('รายงานผล - HermesAI / Kick notifier (hermes.ps1 v' + $SCRIPT:Version + ')')
    $th.Add('เวลา: ' + $stamp.ToString('yyyy-MM-dd HH:mm:ss') + ' (local)')
    $th.Add('โหมด: ' + $Mode + $(if ($DryRun) { ' [dry-run]' } else { '' }))
    $th.Add('สรุปผล: ' + $Verdict)
    $th.Add('')
    $th.Add('ที่มา: ' + $SCRIPT:SourceNote)
    $th.Add('')
    foreach ($l in $SCRIPT:Th) { $th.Add($l) }
    $th.Add('')
    $th.Add('คำสั่งตรวจซ้ำ:')
    if ($T.LogPath) {
        $th.Add('  Get-Content ''' + $T.LogPath + ''' -Tail 20 -Wait')
    }
    $th.Add('  powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" verify')
    $thPath = Join-Path $T.FilesDir 'hermes_report_TH.txt'
    try {
        Write-TextFile -Path $thPath -Text ($th -join "`r`n") -Bom
        Ok "Thai report: $thPath" "รายงานภาษาไทย: $thPath"
    } catch { Warn "cannot write $thPath : $($_.Exception.Message)" "เขียน $thPath ไม่ได้: $($_.Exception.Message)" }
    return @{ Json = $jsonPath; Thai = $thPath }
}

function Show-NextSteps {
    param([hashtable]$T, [string]$Verdict)
    Step 'Next steps' 'ขั้นตอนถัดไป'
    switch ($Verdict) {
        'OK' {
            Info 'Nothing to do. The socket is up with the current key.' 'ไม่ต้องทำอะไรแล้ว socket ทำงานด้วย key ปัจจุบัน'
        }
        'KEY_REJECTED' {
            Info "If this happened right after patching, run: hermes.ps1 fix" "ถ้าเพิ่งเกิดหลังแก้ ให้รัน: hermes.ps1 fix"
            Info "If it repeats with the new key, Kick may have rotated again - check x.com/notjoker" "ถ้ายังซ้ำด้วย key ใหม่ Kick อาจเปลี่ยน key อีก - เช็ค x.com/notjoker"
        }
        'NO_FRESH_EVIDENCE' {
            Info 'The app may log at WARNING level only, or writes elsewhere.' 'แอปอาจเขียน log ระดับ WARNING เท่านั้น หรือเขียนที่อื่น'
            Info 'Check the captured stdout/stderr next to the log file.' 'ดูไฟล์ stdout/stderr ที่เก็บไว้ข้าง ๆ ไฟล์ log'
            Info 'Or run the app in a visible console once and watch it.' 'หรือรันแอปในหน้าต่าง console หนึ่งครั้งแล้วดูข้อความ'
        }
        default { }
    }
    if ($T.LogPath) {
        Info ("follow the live log: Get-Content '" + $T.LogPath + "' -Tail 20 -Wait") ("ดู log สด: Get-Content '" + $T.LogPath + "' -Tail 20 -Wait")
    }
    if ($T.FilesDir) {
        Info ("reports: " + (Join-Path $T.FilesDir 'hermes_report_TH.txt')) ("รายงาน: " + (Join-Path $T.FilesDir 'hermes_report_TH.txt'))
    }
}

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

function Resolve-OrDie {
    try {
        $t = Resolve-Targets
        return $t
    } catch {
        Fail $_.Exception.Message $_.Exception.Message
        return $null
    }
}

function Invoke-Doctor {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    Step 'Environment' 'สภาพแวดล้อม'
    Write-TargetSummary $t
    Save-Cache $t
    Invoke-KeyProbe $t | Out-Null
    Step 'Config + log content' 'เนื้อหา config + log'
    Test-LegacyKeyLeftover $t
    if ($t.ConfigPath) {
        $cfg = Read-JsonFile $t.ConfigPath
        if ($cfg) {
            $props = @()
            foreach ($p in $cfg.PSObject.Properties) { $props += $p.Name }
            Info ("config keys: " + ($props -join ', ')) ("คีย์ใน config: " + ($props -join ', '))
        } else {
            Warn 'config.json did not parse - check it by hand' 'config.json อ่านไม่เข้าหรือไม่ถูกต้อง - ตรวจด้วยมือ'
        }
    }
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        $all = Read-LogSince -Path $t.LogPath -Offset 0 -MaxBytes 524288
        $a = Analyze-LogText -Lines $all.Lines
        Show-LogAnalysis $a 'log'
        $hint = Get-LogClockHint $t
        if ($hint) { Info "log clock: $hint" "นาฬิกาใน log: $hint" }
        $SCRIPT:Evidence.OldLog = $a
    } else {
        Warn 'no log file yet' 'ยังไม่มีไฟล์ log'
    }
    Show-NextSteps $t 'NO_FRESH_EVIDENCE'
    Write-Reports -T $t -Verdict 'DIAGNOSED' -NewAnalysis $null -OldAnalysis $SCRIPT:Evidence.OldLog | Out-Null
    if ($SCRIPT:FailCount -gt 0) { return 1 }
    return 0
}

function Invoke-Verify {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    Step 'Environment' 'สภาพแวดล้อม'
    Write-TargetSummary $t
    Save-Cache $t
    $working = Invoke-KeyProbe $t
    Step 'Current log content' 'เนื้อหา log ปัจจุบัน'
    $old = $null
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        $all = Read-LogSince -Path $t.LogPath -Offset 0 -MaxBytes 524288
        $old = Analyze-LogText -Lines $all.Lines
        Show-LogAnalysis $old 'log'
    } else {
        Warn 'no log file yet' 'ยังไม่มีไฟล์ log'
    }
    $verdict = 'NO_FRESH_EVIDENCE'
    if ($old -and $old.key_rejected -gt 0) { $verdict = 'KEY_REJECTED' }
    elseif ($old -and ($old.subscribed -gt 0 -or $old.messages -gt 0)) { $verdict = 'OK' }
    if ($working -and $verdict -eq 'KEY_REJECTED') {
        Warn 'the log still shows key rejections but the probe says the key connects now - the running process is stale, run: hermes.ps1 fix' `
             'log ยังมี key ถูกปฏิเสธ แต่ผลตรวจบอกว่า key ต่อได้แล้ว - โปรเซสที่รันอยู่เป็นตัวเก่า ให้รัน: hermes.ps1 fix'
    }
    Show-NextSteps $t $verdict
    Write-Reports -T $t -Verdict $verdict -NewAnalysis $null -OldAnalysis $old | Out-Null
    if ($verdict -eq 'KEY_REJECTED') { return 2 }
    if ($verdict -eq 'NO_FRESH_EVIDENCE') { return 1 }
    return 0
}

function Invoke-Fix {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    Step '1/6 Environment' '1/6 สภาพแวดล้อม'
    Write-TargetSummary $t
    Save-Cache $t

    Step '2/6 Old log content (for the record)' '2/6 เนื้อหา log เดิม (เก็บไว้เป็นหลักฐาน)'
    $old = $null
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        $all = Read-LogSince -Path $t.LogPath -Offset 0 -MaxBytes 524288
        $old = Analyze-LogText -Lines $all.Lines
        Show-LogAnalysis $old 'old log'
    } else {
        Warn 'no log file yet' 'ยังไม่มีไฟล์ log'
    }

    Step '3/6 Live key probe' '3/6 ตรวจ key กับ Pusher (สด)'
    $working = Invoke-KeyProbe $t
    if (-not $working) {
        Fail 'neither key could be verified from this machine - check network/proxy/TLS, the app cannot connect either' `
             'ตรวจ key ไม่ผ่านทั้งคู่จากเครื่องนี้ - เช็คเน็ต/พร็อกซี/TLS เพราะแอปก็จะต่อไม่ได้เช่นกัน'
        Info 'you can still patch files without the probe: hermes.ps1 fix -NoRestart' 'ยังแก้ไฟล์ได้แม้ตรวจไม่ผ่าน: hermes.ps1 fix -NoRestart'
    } else {
        Ok "key that works right now: $working" "key ที่ใช้ได้ตอนนี้: $working"
    }

    Step '4/6 Write key material' '4/6 เขียนข้อมูล key'
    Set-KeyMaterial -T $t -WorkingKey $working

    Step '5/6 Restart the notifier' '5/6 รีสตาร์ท notifier'
    $offset = 0
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        try { $offset = (Get-Item -LiteralPath $t.LogPath).Length } catch { $offset = 0 }
        Info "log offset before restart: $offset bytes (only newer bytes count)" "ตำแหน่ง log ก่อนรีสตาร์ท: $offset ไบต์ (นับเฉพาะข้อมูลใหม่)"
    }
    $verdict = 'NO_FRESH_EVIDENCE'
    $newA = $null
    if ($NoRestart) {
        Info 'restart skipped (-NoRestart)' 'ข้ามการรีสตาร์ท (-NoRestart)'
    } elseif (-not $t.LaunchExe) {
        Warn 'cannot restart automatically: no launch command' 'รีสตาร์ทอัตโนมัติไม่ได้: ไม่มีคำสั่งรัน'
        Info "run it yourself, e.g.: `"$($t.PythonPath)`" `"$($t.ScriptPath)`"" "รันเอง เช่น: `"$($t.PythonPath)`" `"$($t.ScriptPath)`""
    } else {
        Stop-Notifier $t | Out-Null
        $p = Start-Notifier $t
        if ($p -or $DryRun) {
            $res = Wait-ForEvidence -T $t -Offset $offset -WaitSec $WaitSeconds
            $verdict = $res.Verdict
            $newA = $res.Analysis
            Step '6/6 Fresh evidence' '6/6 หลักฐานใหม่'
            if ($res.Lines.Count -gt 0) {
                Info "new lines: $($res.Lines.Count)" "บรรทัดใหม่: $($res.Lines.Count)"
                $show = $res.Lines
                if ($show.Count -gt 12) { $show = $show[($show.Count - 12)..($show.Count - 1)] }
                foreach ($l in $show) { Info "  $(Get-Trimmed $l 200)" "  $(Get-Trimmed $l 200)" }
            }
            Show-LogAnalysis $newA 'new log'
        }
    }
    if ($verdict -eq 'NO_FRESH_EVIDENCE') {
        # try the captured stdout/stderr as a second source
        foreach ($f in @('hermes_stdout.log', 'hermes_stderr.log')) {
            $fp = Join-Path $t.FilesDir $f
            if (Test-Path -LiteralPath $fp) {
                $r = Read-LogSince -Path $fp -Offset 0 -MaxBytes 65536
                if ($r.Lines.Count -gt 0) {
                    $aa = Analyze-LogText -Lines $r.Lines
                    Info "captured $f : $($r.Lines.Count) lines" "ไฟล์ $f : $($r.Lines.Count) บรรทัด"
                    Show-LogAnalysis $aa $f
                    if ($aa.key_rejected -gt 0) { $verdict = 'KEY_REJECTED'; $newA = $aa }
                    elseif ($aa.connected -gt 0 -or $aa.subscribed -gt 0 -or $aa.messages -gt 0) { $verdict = 'OK'; $newA = $aa }
                }
            }
        }
    }
    Show-NextSteps $t $verdict
    if ($verdict -eq 'OK') { Ok 'VERDICT: OK' 'สรุป: ใช้งานได้' }
    elseif ($verdict -eq 'KEY_REJECTED') { Fail 'VERDICT: KEY_REJECTED' 'สรุป: key ถูกปฏิเสธ' }
    else { Warn 'VERDICT: NO_FRESH_EVIDENCE' 'สรุป: ยังไม่มีหลักฐานใหม่' }
    Write-Reports -T $t -Verdict $verdict -NewAnalysis $newA -OldAnalysis $old | Out-Null
    if ($verdict -eq 'OK') { return 0 }
    if ($verdict -eq 'KEY_REJECTED') { return 2 }
    return 1
}

function Invoke-Logs {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    if (-not $t.LogPath) { Fail 'no log file found' 'ไม่พบไฟล์ log'; return 2 }
    Info "resolved log path: $($t.LogPath)" "path ของ log ที่ใช้จริง: $($t.LogPath)"
    if ($t.LogIsFallback) { Warn 'this path was guessed - pass -LogPath to pin it' 'path นี้เดามา - ถ้าไม่ใช่ให้ระบุ -LogPath' }
    Write-Line ''
    Write-Line ("Get-Content '" + $t.LogPath + "' -Tail $TailLines") 'DarkGray'
    Write-Line ("Get-Content '" + $t.LogPath + "' -Tail $TailLines -Wait   # live follow") 'DarkGray'
    Write-Line ''
    $slice = Read-LogSince -Path $t.LogPath -Offset 0 -MaxBytes 262144
    $lines = $slice.Lines
    if ($lines.Count -gt $TailLines) { $lines = $lines[($lines.Count - $TailLines)..($lines.Count - 1)] }
    if ($lines.Count -eq 0) { Warn 'log is empty' 'log ว่างเปล่า' }
    foreach ($l in $lines) { Write-Line $l 'Gray' }
    Write-Line ''
    $a = Analyze-LogText -Lines $slice.Lines
    Show-LogAnalysis $a 'log'
    if ($a.key_rejected -gt 0) { return 2 }
    if ($a.subscribed -gt 0 -or $a.messages -gt 0) { return 0 }
    return 1
}

function Invoke-Report {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    $old = $null
    if ($t.LogPath -and (Test-Path -LiteralPath $t.LogPath)) {
        $all = Read-LogSince -Path $t.LogPath -Offset 0 -MaxBytes 524288
        $old = Analyze-LogText -Lines $all.Lines
        Show-LogAnalysis $old 'log'
    }
    $verdict = 'NO_FRESH_EVIDENCE'
    if ($old) {
        if ($old.key_rejected -gt 0) { $verdict = 'KEY_REJECTED' }
        elseif ($old.subscribed -gt 0 -or $old.messages -gt 0) { $verdict = 'OK' }
    }
    $keys = Read-JsonFile $t.KeyFilePath
    if ($keys) {
        Info ("kick_pusher.json updated: $($keys.updated_at_utc), verified_key=$($keys.verified_key)") ("kick_pusher.json อัปเดต: $($keys.updated_at_utc), verified_key=$($keys.verified_key)")
    }
    $r = Write-Reports -T $t -Verdict $verdict -NewAnalysis $null -OldAnalysis $old
    if ($Open -and $r -and (Test-Path -LiteralPath $r.Thai)) { try { Start-Process notepad.exe $r.Thai } catch { } }
    if ($verdict -eq 'KEY_REJECTED') { return 2 }
    if ($verdict -eq 'OK') { return 0 }
    return 1
}

function Invoke-Watch {
    $t = Resolve-OrDie
    if (-not $t) { return 2 }
    if (-not $t.LogPath) { Fail 'no log file found' 'ไม่พบไฟล์ log'; return 2 }
    $watchLog = Join-Path $t.FilesDir 'hermes_watch.log'
    $offset = 0
    try { $offset = (Get-Item -LiteralPath $t.LogPath).Length } catch { $offset = 0 }
    Info "watching $($t.LogPath) every $Interval s (Ctrl+C to stop)" "เฝ้า $($t.LogPath) ทุก $Interval วินาที (กด Ctrl+C เพื่อหยุด)"
    Write-TextFile -Path $watchLog -Text ("watch started $(Get-Date -Format s) - $($t.LogPath)`r`n") -Bom
    $fixes = 0
    while ($true) {
        Start-Sleep -Seconds $Interval
        $slice = Read-LogSince -Path $t.LogPath -Offset $offset
        if ($slice.Lines.Count -eq 0) { continue }
        $offset = $slice.Offset
        $a = Analyze-LogText -Lines $slice.Lines
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        if ($a.key_rejected -gt 0) {
            $msg = "$stamp KEY_REJECTED ($($a.key_rejected)x) - rotating"
            Write-Line $msg 'Yellow'
            try { [IO.File]::AppendAllText($watchLog, $msg + "`r`n", [Text.Encoding]::UTF8) } catch { }
            if ($fixes -ge 3) {
                Fail 'stop: 3 auto-fixes already attempted - a human must look at this' 'หยุด: ซ่อมอัตโนมัติ 3 ครั้งแล้ว - ต้องมีคนดู'
                return 2
            }
            $fixes++
            # alternate the key on every retry so both directions get a chance
            if ($fixes % 2 -eq 0) {
                $tmp = $t.PrimaryKey; $t.PrimaryKey = $t.FallbackKey; $t.FallbackKey = $tmp
                Info "swapping key order: primary is now $($t.PrimaryKey)" "สลับลำดับ key: ตัวหลักตอนนี้คือ $($t.PrimaryKey)"
            }
            Stop-Notifier $t | Out-Null
            Set-KeyMaterial -T $t -WorkingKey $t.PrimaryKey
            $offset = 0
            try { $offset = (Get-Item -LiteralPath $t.LogPath).Length } catch { $offset = 0 }
            Start-Notifier $t | Out-Null
        } elseif ($a.subscribed -gt 0 -or $a.messages -gt 0 -or $a.connected -gt 0) {
            $msg = "$stamp healthy (connected=$($a.connected) subscribed=$($a.subscribed) messages=$($a.messages))"
            Write-Line $msg 'DarkGreen'
            try { [IO.File]::AppendAllText($watchLog, $msg + "`r`n", [Text.Encoding]::UTF8) } catch { }
        }
    }
    return 0
}

function Invoke-SelfTest {
    Write-Line 'hermes.ps1 self-test' 'Cyan'
    $pass = 0
    $fail = 0

    $sample = @(
        '[2026-10-14 03:06:02] [KickWebSocket] Connecting to Pusher WebSocket for room_id=123456',
        '[2026-10-14 03:06:03] [KickWebSocket] pusher:error code=4001 message=App key 32cbd69e4b950bf97679 not in this cluster',
        '[2026-10-14 03:06:04] [KickWebSocket] WebSocket closed: code=4001 (Unknown/Unregistered), reason=App key not in this cluster',
        '[2026-10-14 03:06:20] [KickWebSocket] pusher:connection_established',
        '[2026-10-14 03:06:21] [KickWebSocket] pusher_internal:subscription_succeeded channel=chatrooms.123456.v2',
        '[2026-10-14 03:06:31] [KickWebSocket] ChatMessageEvent from viewer1: hello'
    )
    $a = Analyze-LogText -Lines $sample
    if ($a.lines -eq 6 -and $a.key_rejected -eq 2 -and $a.connected -eq 1 -and $a.subscribed -eq 1 -and $a.messages -eq 1) {
        Write-Line '  [PASS] log analysis' 'Green'; $pass++
    } else {
        Write-Line "  [FAIL] log analysis (lines=$($a.lines) rej=$($a.key_rejected) conn=$($a.connected) sub=$($a.subscribed) msg=$($a.messages))" 'Red'; $fail++
    }

    $s1 = Split-CommandLine '"D:\HermesAI\.venv\Scripts\python.exe" "D:\HermesAI\OBS\notifier.py" --config config.json'
    $s2 = Split-CommandLine 'python.exe notifier.py'
    $s3 = Split-CommandLine 'D:\HermesAI\.venv\Scripts\pythonw.exe D:\HermesAI\OBS\notifier.py'
    if ($s1.Exe -eq 'D:\HermesAI\.venv\Scripts\python.exe' -and $s1.Args -eq '"D:\HermesAI\OBS\notifier.py" --config config.json' -and
        $s2.Exe -eq 'python.exe' -and $s2.Args -eq 'notifier.py' -and
        $s3.Exe -eq 'D:\HermesAI\.venv\Scripts\pythonw.exe' -and $s3.Args -eq 'D:\HermesAI\OBS\notifier.py') {
        Write-Line '  [PASS] command line splitter' 'Green'; $pass++
    } else {
        Write-Line "  [FAIL] command line splitter (`"$($s1.Exe)`" | `"$($s1.Args)`" | `"$($s2.Exe)`")" 'Red'; $fail++
    }

    $tmp = Join-Path $env:TEMP ('hermes_selftest_' + [Guid]::NewGuid().ToString('N') + '.json')
    $doc = [ordered]@{ a = 1; keys = @('x', 'y'); nested = [ordered]@{ ok = $true } }
    try {
        Write-TextFile -Path $tmp -Text ($doc | ConvertTo-Json -Depth 5)
        $back = Read-JsonFile $tmp
        if ($back.a -eq 1 -and $back.keys[1] -eq 'y' -and $back.nested.ok) { Write-Line '  [PASS] json write/read' 'Green'; $pass++ }
        else { Write-Line '  [FAIL] json write/read' 'Red'; $fail++ }
    } catch {
        Write-Line "  [FAIL] json write/read: $($_.Exception.Message)" 'Red'; $fail++
    } finally {
        try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch { }
    }

    $cfgSample = '{ "app_key": "32cbd69e4b950bf97679", "cluster": "eu1", "channel": "x" }'
    $patched = $cfgSample.Replace('32cbd69e4b950bf97679', '34bf7a0ff419a2a775b9')
    $patched = [regex]::Replace($patched, '("(?:app_key|pusher_key)"\s*:\s*")([^"]*)(")', { param($m) $m.Groups[1].Value + '34bf7a0ff419a2a775b9' + $m.Groups[3].Value })
    $patched = [regex]::Replace($patched, '("(?:cluster|pusher_cluster)"\s*:\s*")([^"]*)(")', { param($m) $m.Groups[1].Value + 'us2' + $m.Groups[3].Value })
    $okCfg = $false
    try {
        $o = $patched | ConvertFrom-Json
        if ($o.app_key -eq '34bf7a0ff419a2a775b9' -and $o.cluster -eq 'us2') { $okCfg = $true }
    } catch { $okCfg = $false }
    if ($okCfg) { Write-Line '  [PASS] config patch regex' 'Green'; $pass++ }
    else { Write-Line "  [FAIL] config patch regex -> $patched" 'Red'; $fail++ }

    if ($Online) {
        Write-Line '  probing both keys (live)...' 'DarkGray'
        $p = Test-PusherKey -ProbeKey $Key -ProbeCluster $Cluster -TimeoutSec 12
        Write-ProbeResult $p
        $p2 = Test-PusherKey -ProbeKey $LegacyKey -ProbeCluster $Cluster -TimeoutSec 12
        Write-ProbeResult $p2
    }
    Write-Line ''
    Write-Line ("self-test: $pass passed, $fail failed") 'Cyan'
    if ($fail -gt 0) { return 2 }
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if ($SelfTest) {
    $code = Invoke-SelfTest
    exit $code
}

Write-Line ("hermes.ps1 v$SCRIPT:Version  mode=$Mode  dry-run=$([bool]$DryRun)") 'White'

switch ($Mode) {
    'doctor' { $code = Invoke-Doctor }
    'verify' { $code = Invoke-Verify }
    'logs' { $code = Invoke-Logs }
    'report' { $code = Invoke-Report }
    'watch' { $code = Invoke-Watch }
    default { $code = Invoke-Fix }
}
exit $code
