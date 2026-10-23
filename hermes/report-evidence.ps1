#Requires -Version 5.1
<#
    report-evidence.ps1 - collect everything needed to close the Kick/Pusher case into ONE file.

    Why this exists
    ---------------
    The repair tool (hermes.ps1) runs on YOUR Windows machine; whoever reads the
    result (a person or an AI agent in a chat) cannot see that machine. Instead of
    "wait 10-15 seconds and look at the log", this packs the facts into a single
    file that can be attached to a chat or committed to the repo:

      * the verdict block (hermes_report.json) if the repair tool has run
      * config.json + kick_pusher.json with SHA256 (secrets auto-redacted)
      * the log tail, with size/mtime and the offset rule explained
      * captured stdout/stderr of the restarted process, if any
      * the python processes with their command lines (only notifier-looking ones)
      * the launch command that was used

    Note on log evidence: a log file is appended to, so old lines always remain.
    This report always prints the file size and mtime, and (when hermes.ps1 has
    run) the offset that marks where "fresh" evidence starts - so nobody has to
    guess whether a line is old or new.

    Modes
    -----
      .\report-evidence.ps1                       # write the file next to your Desktop
      .\report-evidence.ps1 -Open                 # ... and open it in Notepad
      .\report-evidence.ps1 -Zip                  # also produce a .zip (easy to attach)
      .\report-evidence.ps1 -Repo user/repo -Branch hermes-evidence
                                                  # commit it to the repo (uses your git
                                                  # credential manager - no token in chat)
      .\report-evidence.ps1 -DiscordWebhookFile "$env:USERPROFILE\hook.txt"
                                                  # post the report to a Discord channel
                                                  # (URL comes from a file: it is a credential,
                                                  #  never paste it into a chat)
      .\report-evidence.ps1 -SelfTest             # prove redaction works, write nothing

    Exit codes: 0 = report written, 1 = written with warnings, 2 = failure.
#>

[CmdletBinding()]
param(
    [string]$AppDir = 'D:\HermesAI\OBS',
    [string]$OutFile = '',
    [int]$TailLines = 40,

    [string]$Repo = '',
    [string]$Branch = 'hermes-evidence',
    [switch]$NoPush,

    # Discord: post the report to a channel webhook (NO Arena connector needed).
    # The webhook URL is a credential: pass a FILE path, never the URL in a chat.
    [string]$Discord = '',
    [string]$DiscordWebhookFile = '',

    [switch]$Open,
    [switch]$Zip,
    [switch]$SelfTest,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$script:Warnings = 0

function Write-Head {
    param([string]$Text)
    $script:lines.Add('')
    $script:lines.Add('### ' + $Text)
    if (-not $Quiet) { Write-Host ('### ' + $Text) -ForegroundColor Cyan }
}

function Write-Body {
    param([string]$Text = '', [string]$Color = 'Gray')
    $script:lines.Add($Text)
    if (-not $Quiet) { Write-Host $Text -ForegroundColor $Color }
}

function Write-Warn {
    param([string]$Text)
    $script:Warnings++
    $script:lines.Add('[WARN] ' + $Text)
    if (-not $Quiet) { Write-Host ('[WARN] ' + $Text) -ForegroundColor Yellow }
}

function Get-Redacted {
    param([string]$Text)
    if (-not $Text) { return '' }
    $t = $Text
    $t = [regex]::Replace($t, '(?i)("?(?:access_token|refresh_token|id_token|client_secret|api_key|apikey|password|secret|authorization|webhook)"?\s*[:=]\s*")([^"]{4,})(")', '$1<redacted>$3')
    $t = [regex]::Replace($t, '(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}', 'Bearer <redacted>')
    $t = [regex]::Replace($t, '(?i)\bgh[pousr]_[A-Za-z0-9]{20,}', '<redacted-github-token>')
    $t = [regex]::Replace($t, '\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}', '<redacted-jwt>')
    $t = [regex]::Replace($t, '(?i)\b(xox[baprs]-[A-Za-z0-9\-]{10,})', '<redacted-slack-token>')
    return $t
}

function Get-Lines {
    param([string]$Path, [int]$Count)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
    try { return @(Get-Content -LiteralPath $Path -Tail $Count -ErrorAction Stop) } catch { return @() }
}

function Get-FileFacts {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{ path = $Path; exists = $false }
    }
    try {
        $item = Get-Item -LiteralPath $Path
        $hash = ''
        try { $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower() } catch { $hash = 'unavailable' }
        return [ordered]@{
            path        = $Path
            exists      = $true
            bytes       = $item.Length
            last_write  = $item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
            sha256      = $hash
        }
    } catch {
        return [ordered]@{ path = $Path; exists = $true; read_error = $_.Exception.Message }
    }
}

function Get-NotifierProcesses {
    $out = @()
    try {
        $out = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            ($_.Name -match '^py' -or $_.Name -match 'python') -and
            ($_.CommandLine -match 'HermesAI|notifier|kick')
        })
    } catch { $out = @() }
    return $out
}

function Get-ExistingPaths {
    param([string]$Dir, [string[]]$Names, [string[]]$ExtraDirs = @())
    $found = @()
    foreach ($extra in $ExtraDirs) {
        foreach ($n in $Names) {
            $p = Join-Path $extra $n
            if (Test-Path -LiteralPath $p) { $found += $p }
        }
    }
    foreach ($n in $Names) {
        $p = Join-Path $Dir $n
        if (Test-Path -LiteralPath $p) { $found += $p }
    }
    return $found
}

function Get-DiscordWebhook {
    param([string]$Url, [string]$File, [switch]$Quiet)
    if ($File) {
        if (-not (Test-Path -LiteralPath $File)) {
            Write-Warn ('webhook file not found: ' + $File); return ''
        }
        try { $Url = ([IO.File]::ReadAllText($File)).Trim() }
        catch { Write-Warn ('cannot read webhook file: ' + $_.Exception.Message); return '' }
    }
    if (-not $Url) { return '' }
    # only accept a real Discord webhook, so a typo can never post your log somewhere else
    if ($Url -notmatch '^https://([a-z]+\.)?discord(app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+$') {
        if (-not $Quiet) {
            Write-Warn 'not a Discord webhook URL (expected https://discord.com/api/webhooks/<id>/<token>) - nothing sent'
        }
        return ''
    }
    return $Url
}

function Send-DiscordEvidence {
    param([string]$Webhook, [string]$FilePath, [string]$Summary = '')

    $fileName = Split-Path -Leaf $FilePath
    $content = 'hermes evidence report'
    if ($Summary) { $content = $Summary }
    if ($content.Length -gt 1800) { $content = $content.Substring(0, 1800) }
    $payload = '{"content":"hermes evidence report"}'
    try { $payload = (@{ content = $content } | ConvertTo-Json -Compress) } catch { }

    # multipart/form-data assembled by hand: Invoke-RestMethod -Form needs PowerShell 6+
    $boundary = '----hermes' + [Guid]::NewGuid().ToString('N')
    $enc = New-Object System.Text.UTF8Encoding($false)
    try { $fileBytes = [IO.File]::ReadAllBytes($FilePath) }
    catch { Write-Warn ('cannot read ' + $FilePath); return $false }

    $pre = $enc.GetBytes(
        '--' + $boundary + "`r`n" +
        'Content-Disposition: form-data; name="payload_json"' + "`r`n`r`n" +
        $payload + "`r`n" +
        '--' + $boundary + "`r`n" +
        'Content-Disposition: form-data; name="files[0]"; filename="' + $fileName + '"' + "`r`n" +
        'Content-Type: text/plain' + "`r`n`r`n")
    $post = $enc.GetBytes("`r`n--" + $boundary + "--`r`n")
    $body = New-Object byte[] ($pre.Length + $fileBytes.Length + $post.Length)
    [Array]::Copy($pre, 0, $body, 0, $pre.Length)
    [Array]::Copy($fileBytes, 0, $body, $pre.Length, $fileBytes.Length)
    [Array]::Copy($post, 0, $body, $pre.Length + $fileBytes.Length, $post.Length)

    try {
        Invoke-RestMethod -Uri $Webhook -Method Post -Body $body -TimeoutSec 60 `
            -ContentType ('multipart/form-data; boundary=' + $boundary) -ErrorAction Stop | Out-Null
        Write-Body ('posted the report file to Discord (' + $fileName + ')') 'Green'
        return $true
    } catch {
        Write-Warn ('Discord file upload failed: ' + $_.Exception.Message)
    }

    # fallback: text only, chunked, so the key lines still arrive
    try {
        $text = [IO.File]::ReadAllText($FilePath)
        $chunks = New-Object System.Collections.Generic.List[string]
        $current = ''
        foreach ($line in ($text -split "`r?`n")) {
            if (($current.Length + $line.Length + 1) -gt 1800) { $chunks.Add($current); $current = '' }
            $current += $line + "`n"
            if ($chunks.Count -ge 4) { break }
        }
        if ($current) { $chunks.Add($current) }
        $count = [Math]::Min($chunks.Count, 4)
        for ($i = 0; $i -lt $count; $i++) {
            $msg = @{ content = ('```' + "`n" + '[' + ($i + 1) + '/' + $count + '] ' + $chunks[$i] + '```') } |
                ConvertTo-Json -Compress
            Invoke-RestMethod -Uri $Webhook -Method Post -Body $enc.GetBytes($msg) -TimeoutSec 30 `
                -ContentType 'application/json' -ErrorAction Stop | Out-Null
        }
        Write-Body ('posted ' + $count + ' text chunk(s) to Discord instead of the file') 'Yellow'
        return $true
    } catch {
        Write-Warn ('Discord text post failed too: ' + $_.Exception.Message)
        return $false
    }
}

function Invoke-SelfTest {
    Write-Body 'redaction self test'
    $samples = @{
        'json token'    = '{"access_token":"abcdef123456","other":"keep"}'
        'bearer'        = 'Authorization: Bearer eyJhbGciOi.payload.signature'
        'github pat'    = 'token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        'jwt'           = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP'
        'slack'         = 'xoxb-1234567890-abcdefghijkl'
    }
    $fail = 0
    foreach ($k in $samples.Keys) {
        $out = Get-Redacted $samples[$k]
        $leaked = $out -eq $samples[$k]
        if ($leaked) { Write-Body ("  [FAIL] {0} not redacted -> {1}" -f $k, $out); $fail++ }
        else { Write-Body ("  [OK]   {0} -> {1}" -f $k, $out) }
    }
    $keep = Get-Redacted 'app_key=34bf7a0ff419a2a775b9 channel=abc'
    if ($keep -notmatch '34bf7a0ff419a2a775b9') { Write-Body '  [FAIL] public pusher key must stay readable'; $fail++ }
    else { Write-Body '  [OK]   public pusher key is kept' }

    $good = Get-DiscordWebhook -Url 'https://discord.com/api/webhooks/123456789012345678/AbCdEf-ghi_JKL' -File '' -Quiet
    $bad1 = Get-DiscordWebhook -Url 'https://evil.example.com/api/webhooks/1/x' -File '' -Quiet
    $bad2 = Get-DiscordWebhook -Url 'not a url' -File '' -Quiet
    if ($good -and -not $bad1 -and -not $bad2) {
        Write-Body '  [OK]   discord webhook guard (discord.com only)'
    } else {
        Write-Body ('  [FAIL] discord webhook guard: good=' + [bool]$good + ' bad1=' + [bool]$bad1 + ' bad2=' + [bool]$bad2)
        $fail++
    }
    Write-Body ''
    Write-Body ("self test: {0} failed" -f $fail)
    if ($fail -gt 0) { return 2 }
    return 0
}

function Push-ToRepo {
    param([string]$Repo, [string]$Branch, [string]$FilePath)
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { Write-Warn 'git not found in PATH - cannot push (attach the file to the chat instead)'; return $false }
    $tmp = Join-Path $env:TEMP ('hermes-push-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    $url = 'https://github.com/' + $Repo + '.git'
    try {
        Write-Body ('cloning ' + $url)
        & git clone --depth 1 $url $tmp 2>&1 | ForEach-Object { Write-Body ('  git: ' + $_) }
        if (-not (Test-Path -LiteralPath (Join-Path $tmp '.git'))) { Write-Warn 'clone failed'; return $false }
        Push-Location $tmp
        try {
            & git checkout -B $Branch 2>&1 | ForEach-Object { Write-Body ('  git: ' + $_) }
            $destDir = Join-Path $tmp 'evidence'
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
            $dest = Join-Path $destDir (Split-Path -Leaf $FilePath)
            Copy-Item -LiteralPath $FilePath -Destination $dest -Force
            & git add -- $dest 2>&1 | ForEach-Object { Write-Body ('  git: ' + $_) }
            $msg = 'evidence: ' + $env:COMPUTERNAME + ' ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
            & git -c user.name='hermes-bridge' -c user.email='hermes-bridge@localhost' commit -m $msg 2>&1 |
                ForEach-Object { Write-Body ('  git: ' + $_) }
            & git push -u origin $Branch 2>&1 | ForEach-Object { Write-Body ('  git: ' + $_) }
            if ($LASTEXITCODE -ne 0) {
                Write-Warn ('push failed (exit ' + $LASTEXITCODE + '). Run "git push" once by hand in that repo to set up the credential prompt, then retry.')
                return $false
            }
            Write-Body ('pushed to ' + $Repo + ' branch ' + $Branch) 'Green'
            return $true
        } finally { Pop-Location }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------------------------- main

if ($SelfTest) {
    $script:lines = New-Object System.Collections.Generic.List[string]
    $code = Invoke-SelfTest
    exit $code
}

$script:lines = New-Object System.Collections.Generic.List[string]
$now = Get-Date
$stamp = $now.ToString('yyyyMMdd-HHmmss')

if (-not $Quiet) { Write-Host ('hermes evidence report - ' + $now.ToString('yyyy-MM-dd HH:mm:ss')) -ForegroundColor White }

Write-Head 'WHO / WHEN'
Write-Body ('host      : ' + $env:COMPUTERNAME)
Write-Body ('user      : ' + $env:USERNAME)
Write-Body ('when local: ' + $now.ToString('yyyy-MM-dd HH:mm:ss') + ' (' + [TimeZoneInfo]::Local.Id + ')')
Write-Body ('when utc  : ' + $now.ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss') + 'Z  <- compare log timestamps against BOTH lines')
Write-Body ('powershell: ' + $PSVersionTable.PSVersion.ToString())
Write-Body ('app dir   : ' + $AppDir)
Write-Body ''
Write-Body 'NOTE: lines/tokens that look secret are replaced with <redacted>. Kick Pusher app keys'
Write-Body '      are public (they ship inside every browser) so they are kept readable on purpose.'

$filesDir = Join-Path $AppDir 'files'
if (-not (Test-Path -LiteralPath $filesDir)) { $filesDir = $AppDir }

Write-Head 'VERDICT (from the repair tool)'
$reportJson = Join-Path $filesDir 'hermes_report.json'
if (Test-Path -LiteralPath $reportJson) {
    try {
        $rep = Get-Content -LiteralPath $reportJson -Raw | ConvertFrom-Json
        Write-Body ('verdict     : ' + $rep.verdict)
        Write-Body ('mode        : ' + $rep.mode)
        Write-Body ('ran_at_utc  : ' + $rep.ran_at_utc)
        if ($rep.probes) {
            Write-Body ('probe primary: connected=' + $rep.probes.primary.connected + ' code=' + $rep.probes.primary.error_code)
            Write-Body ('probe fallback: connected=' + $rep.probes.fallback.connected + ' code=' + $rep.probes.fallback.error_code)
        }
        if ($rep.new_log) {
            Write-Body ('new log evidence: lines=' + $rep.new_log.lines + ' connected=' + $rep.new_log.connected +
                        ' subscribed=' + $rep.new_log.subscribed + ' messages=' + $rep.new_log.messages +
                        ' key_rejected=' + $rep.new_log.key_rejected)
        }
        Write-Body ('warnings/failures: ' + $rep.warnings + '/' + $rep.failures)
    } catch {
        Write-Warn ('hermes_report.json exists but does not parse: ' + $_.Exception.Message)
    }
    Write-Body ''
    Write-Body '--- raw hermes_report.json ---'
    foreach ($l in (Get-Lines $reportJson 200)) { Write-Body (Get-Redacted $l) }
} else {
    Write-Warn 'hermes_report.json not found - run hermes.ps1 fix (or verify) first'
}

Write-Head 'FILES'
$configPaths = Get-ExistingPaths -Dir $filesDir -Names @('config.json') -ExtraDirs @($AppDir)
$keyPaths = Get-ExistingPaths -Dir $AppDir -Names @('kick_pusher.json') -ExtraDirs @($filesDir)
$logPaths = Get-ExistingPaths -Dir $filesDir -Names @('kick_notifier.log', 'hermes_stdout.log', 'hermes_stderr.log', 'manual_stdout.log', 'manual_stderr.log')

foreach ($p in @($configPaths + $keyPaths + $logPaths)) {
    $f = Get-FileFacts $p
    Write-Body ("{0}`n    bytes={1} last_write={2}`n    sha256={3}" -f $p, $f.bytes, $f.last_write, $f.sha256)
}
if ($logPaths.Count -eq 0) { Write-Warn 'no log file found under the app dir' }

Write-Head 'CONFIG (secrets redacted)'
if ($configPaths.Count -gt 0) {
    foreach ($p in $configPaths) {
        Write-Body ('--- ' + $p + ' ---')
        $raw = ''
        try { $raw = [IO.File]::ReadAllText($p) } catch { Write-Warn ('cannot read: ' + $_.Exception.Message); continue }
        foreach ($l in ($raw -split "`r?`n")) { Write-Body (Get-Redacted $l) }
        if ($raw -match '32cbd69e4b950bf97679') { Write-Warn 'this file still contains the REVOKED key 32cbd69e4b950bf97679' }
        if ($raw -match '34bf7a0ff419a2a775b9') { Write-Body 'contains the new key 34bf7a0ff419a2a775b9 (expected)' 'Green' }
        if ($raw -notmatch '34bf7a0ff419a2a775b9' -and $raw -notmatch '32cbd69e4b950bf97679') {
            Write-Warn 'no known Kick Pusher key found in this config - the app may read its key from elsewhere'
        }
    }
} else {
    Write-Warn 'config.json not found'
}

Write-Head 'KEY FILE (kick_pusher.json)'
if ($keyPaths.Count -gt 0) {
    foreach ($p in $keyPaths) {
        Write-Body ('--- ' + $p + ' ---')
        foreach ($l in (Get-Lines $p 60)) { Write-Body (Get-Redacted $l) }
    }
} else {
    Write-Body 'not present (only hermes.ps1 / patch_kick_app.py create it)'
}

Write-Head 'LOG TAIL (remember: the file is appended to, old lines stay)'
if ($logPaths.Count -gt 0) {
    foreach ($p in $logPaths) {
        $f = Get-FileFacts $p
        Write-Body ('--- ' + $p + '  (bytes=' + $f.bytes + ', last_write=' + $f.last_write + ') ---')
        $tail = Get-Lines $p $TailLines
        if ($tail.Count -eq 0) { Write-Body '(empty)' }
        foreach ($l in $tail) { Write-Body (Get-Redacted $l) }
        $joined = ($tail -join "`n")
        if ($joined -match 'pusher:error|4001') { Write-Warn 'the tail still shows pusher:error/4001 - if ALL these lines are older than last_write-restart, see the offset rule below' }
        if ($joined -match 'connection_established|subscription_succeeded|ChatMessageEvent') { Write-Body 'tail contains a success marker (connection_established / subscription_succeeded / ChatMessageEvent)' 'Green' }
    }
    Write-Body ''
    Write-Body 'offset rule: fresh evidence = bytes written AFTER the log size captured just before the restart.'
    Write-Body 'hermes.ps1 prints that offset ("log offset before restart: N bytes") and only judges those bytes.'
} else {
    Write-Body '(no log file)'
}

Write-Head 'PROCESSES (notifier-looking only)'
$procs = Get-NotifierProcesses
if ($procs.Count -eq 0) {
    Write-Warn 'no notifier-looking python process is running'
} else {
    foreach ($p in $procs) {
        Write-Body ("pid {0}  {1}`n    cmd: {2}" -f $p.ProcessId, $p.Name, (Get-Redacted ([string]$p.CommandLine)))
    }
}
$launchFile = Join-Path $AppDir 'hermes.launch.txt'
if (Test-Path -LiteralPath $launchFile) {
    Write-Body ''
    Write-Body '--- launch command recorded by hermes.ps1 (' + $launchFile + ') ---'
    foreach ($l in (Get-Lines $launchFile 5)) { Write-Body (Get-Redacted $l) }
}

Write-Head 'WHAT TO DO WITH THIS FILE'
Write-Body '1) attach it to the chat (or push it with -Repo), and'
Write-Body '2) include the exit code of hermes.ps1 fix, and'
Write-Body '3) the one line it printed: RESULT_JSON {...}'

if (-not $OutFile) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop) { $desktop = $env:USERPROFILE }
    $OutFile = Join-Path $desktop ('hermes-evidence-' + $stamp + '.txt')
}

try {
    $parent = Split-Path -Parent $OutFile
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $enc = New-Object System.Text.UTF8Encoding($true)
    [IO.File]::WriteAllText($OutFile, ($script:lines -join "`r`n"), $enc)
    Write-Host ('report written: ' + $OutFile + '  (' + (Get-Item -LiteralPath $OutFile).Length + ' bytes)') -ForegroundColor Green
} catch {
    Write-Host ('cannot write ' + $OutFile + ' : ' + $_.Exception.Message) -ForegroundColor Red
    exit 2
}

if ($Zip) {
    $zipPath = [IO.Path]::ChangeExtension($OutFile, '.zip')
    try {
        Compress-Archive -LiteralPath $OutFile -DestinationPath $zipPath -Force
        Write-Host ('zip written   : ' + $zipPath) -ForegroundColor Green
    } catch { Write-Warn ('zip failed: ' + $_.Exception.Message) }
}

if ($Discord -or $DiscordWebhookFile) {
    $hook = Get-DiscordWebhook -Url $Discord -File $DiscordWebhookFile
    if ($hook) {
        $verdictBit = ''
        if (Test-Path -LiteralPath $reportJson) {
            try {
                $verdictBit = 'verdict=' + (Get-Content -LiteralPath $reportJson -Raw | ConvertFrom-Json).verdict
            } catch { }
        }
        $summary = 'hermes evidence from ' + $env:COMPUTERNAME + ' ' + $now.ToString('yyyy-MM-dd HH:mm:ss') +
                   ' ' + $verdictBit + "`n" + 'file: ' + (Split-Path -Leaf $OutFile)
        Send-DiscordEvidence -Webhook $hook -FilePath $OutFile -Summary $summary | Out-Null
    }
}

if ($Repo -and -not $NoPush) {
    Push-ToRepo -Repo $Repo -Branch $Branch -FilePath $OutFile | Out-Null
}

if ($Open) {
    try { Start-Process notepad.exe $OutFile } catch { }
}

if ($script:Warnings -gt 0) {
    Write-Host ('done with ' + $script:Warnings + ' warning(s)') -ForegroundColor Yellow
    exit 1
}
Write-Host 'done' -ForegroundColor Green
exit 0
