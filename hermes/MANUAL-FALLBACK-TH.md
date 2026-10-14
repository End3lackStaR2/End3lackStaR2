# วิธีสำรอง (ไม่ต้องโหลดไฟล์ใด ๆ) — MANUAL FALLBACK

ใช้เมื่อ: ดาวน์โหลดจาก GitHub ไม่ได้ หรือข้อความที่คัดลอกมาโดน markdown ทำให้ URL พัง
ทุกท่อนข้างล่างใช้ PowerShell ที่มีอยู่ใน Windows เท่านั้น **ไม่มี URL ให้ markdown ทำพัง** (ตัว URL ถูกต่อจากชิ้นส่วน)

> ทุกท่อนคัดลอกแยกกันได้ ผลลัพธ์มี `VERDICT:` ให้คัดลอกกลับเสมอ

---

## ท่อน 0 (ไม่บังคับ) — โหลดเครื่องมือเต็ม

**ทำไมท่อนนี้หน้าตาแปลก (ใช้ `Join-Path` ทุกที่)**: ไคลเอนต์แชทบางตัวแปลงข้อความอย่าง
`$env:TEMP\ebs.zip` ให้กลายเป็นลิงก์ `[ebs.zip](http://ebs.zip)` — พอวางใน PowerShell
จะได้ชื่อไฟล์ที่มี `:` และ `/` ซึ่ง Windows สร้างไม่ได้ (`iwr` จะล้มทันที)
ท่อนนี้จึงสร้างพาธทุกอันด้วย `Join-Path` และต่อ URL จากชิ้นส่วน ไม่มีพาธแนว `\ชื่อ.ext` ให้ระบบแปลง

### ทางที่ 0 — ดาวน์โหลดผ่านเบราว์เซอร์ (ไม่มี URL ใน PowerShell เลย = พังไม่ได้)

1. เปิดหน้า PR: `github.com/End3lackStaR2/End3lackStaR2/pull/1`
2. กดปุ่มเขียว **Code → Download ZIP**
3. แตกไฟล์ แล้วคัดลอกโฟลเดอร์ `hermes` ไปไว้ที่ `%USERPROFILE%\hermes-kick`

### ทางที่ A — codeload zip (ตรวจแล้วทุกไบต์ ตรงกับต้นฉบับ)

```powershell
$repo = 'End3lackStaR2/End3lackStaR2'
$sha  = '61a23e019514ec925aa28996c0e366a534faae93'
$tmp  = $env:TEMP
$zip  = Join-Path $tmp 'ebs.zip'
$dst  = Join-Path $tmp 'ebs'
$d    = Join-Path $env:USERPROFILE 'hermes-kick'

New-Item -ItemType Directory -Force -Path $d | Out-Null
iwr -UseBasicParsing ('https://' + 'codeload.' + 'github.com/' + $repo + '/zip/' + $sha) -OutFile $zip
Write-Host ('zip: ' + (Get-Item -LiteralPath $zip).Length + ' bytes')
Expand-Archive -LiteralPath $zip -DestinationPath $dst -Force

Get-ChildItem -LiteralPath $dst -Directory | ForEach-Object {
    $src = Join-Path $_.FullName 'hermes'
    if (Test-Path -LiteralPath $src) { Copy-Item -Path (Join-Path $src '*') -Destination $d -Recurse -Force }
}

$h = Join-Path $d 'hermes.ps1'
$raw = Get-Content -LiteralPath $h -Raw
if ($raw.Length -lt 50000 -or $raw -notmatch 'hermes\.ps1 v' -or $raw -notmatch 'VERDICT') {
    Write-Host 'ไฟล์ไม่ครบ - ใช้ทางที่ 0 หรือ ทาง B' -ForegroundColor Yellow
} else {
    Get-ChildItem -LiteralPath $d | Select-Object Name, Length
    Write-Host ('พร้อมใช้: ' + $h + '  (' + (Get-Item -LiteralPath $h).Length + ' bytes)') -ForegroundColor Green
}
```

### ทางที่ B — git clone (ถ้ามี git; ทางนี้วางแล้วปลอดภัยที่สุด)

```powershell
$d = Join-Path $env:USERPROFILE 'hermes-kick'
git clone --depth 1 'https://github.com/End3lackStaR2/End3lackStaR2.git' $d
$h = Join-Path (Join-Path $d 'hermes') 'hermes.ps1'
Get-ChildItem -LiteralPath $h | Select-Object Name, Length
```

### ทางที่ C — GitHub API (ถ้า codeload ถูกบล็อก)

```powershell
$repo = 'End3lackStaR2/End3lackStaR2'
$sha  = '61a23e019514ec925aa28996c0e366a534faae93'
$d    = Join-Path $env:USERPROFILE 'hermes-kick'
New-Item -ItemType Directory -Force -Path $d | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $d 'tools') | Out-Null

foreach ($f in @('hermes.ps1', 'report-evidence.ps1')) {
    $api  = 'https://' + 'api.' + 'github.com/repos/' + $repo + '/contents/hermes/' + $f + '?ref=' + $sha
    $json = (iwr -UseBasicParsing $api).Content | ConvertFrom-Json
    $out  = Join-Path $d $f
    [IO.File]::WriteAllBytes($out, [Convert]::FromBase64String($json.content))
    Write-Host ('wrote ' + $out + ' (' + (Get-Item -LiteralPath $out).Length + ' bytes)')
}
```

### ตรวจ + รัน (ใช้ได้กับทุกทางข้างบน)

```powershell
$d = Join-Path $env:USERPROFILE 'hermes-kick'
$h = Join-Path $d 'hermes.ps1'
if (-not (Test-Path -LiteralPath $h)) { $h = Join-Path (Join-Path $d 'hermes') 'hermes.ps1' }
Write-Host ('ใช้: ' + $h + ' (' + (Get-Item -LiteralPath $h).Length + ' bytes)')

powershell -NoProfile -ExecutionPolicy Bypass -File $h doctor
powershell -NoProfile -ExecutionPolicy Bypass -File $h fix -WaitSeconds 90 -Open
```

> ตรวจสอบจากฝั่งผู้พัฒนา (sandbox ที่เข้าได้เฉพาะ github.com / codeload / api.github.com):
> ทาง A และทาง C ให้ไฟล์ไบต์ตรงกันเป๊ะกับต้นฉบับ (69,094 ไบต์, BOM ถูกต้อง, มี `RESULT_JSON`)
> ส่วน `github.com/.../raw/...` (ที่บทสนทนาก่อนหน้าใช้) เป็น 302 ไป `raw.githubusercontent.com`
> ซึ่งเครือข่ายของผมเข้าไม่ถึง จึงยืนยันไม่ได้ — เลี่ยงไปใช้ทาง A/B/C หรือทางที่ 0

---

## ท่อน 1 — ทดสอบ key ใหม่กับ Pusher (ไม่แก้อะไรทั้งสิ้น)

```powershell
Add-Type -AssemblyName System.Net.WebSockets.Client
$key = '34bf7a0ff419a2a775b9'
$url = 'wss://' + 'ws-us2' + '.pusher.com/app/' + $key + '?protocol=7&client=js&version=8.4.0&flash=false'
$ws  = New-Object System.Net.WebSockets.ClientWebSocket
$cts = New-Object System.Threading.CancellationTokenSource
$cts.CancelAfter(12000)
try {
    $ws.ConnectAsync([Uri]$url, $cts.Token).GetAwaiter().GetResult() | Out-Null
    $buf = New-Object byte[] 8192
    $seg = New-Object 'System.ArraySegment[byte]' -ArgumentList @(,$buf)
    $r   = $ws.ReceiveAsync($seg, $cts.Token).GetAwaiter().GetResult()
    $msg = [Text.Encoding]::UTF8.GetString($buf, 0, $r.Count)
    if     ($msg -match 'connection_established') { Write-Host 'KEY-OK: connected to Pusher' -ForegroundColor Green }
    elseif ($msg -match 'pusher:error')            { Write-Host "KEY-REJECTED: $msg" -ForegroundColor Red }
    else                                           { Write-Host "UNEXPECTED: $msg" -ForegroundColor Yellow }
} catch {
    Write-Host "NO-ANSWER: $($_.Exception.Message)" -ForegroundColor Yellow
} finally {
    $ws.Dispose(); $cts.Dispose()
}
```

ผลที่ต้องได้: `KEY-OK: connected to Pusher`
ถ้าได้ `NO-ANSWER` → ปัญหาเน็ต/TLS/ไฟร์วอลล์ (ไม่ใช่ปัญหา key)
ถ้าได้ `KEY-REJECTED` → Kick อาจหมุน key อีกครั้ง ดูท่อน 5

---

## ท่อน 2 — แก้ config.json (สำรองอัตโนมัติก่อนเขียน)

```powershell
$app  = 'D:\HermesAI\OBS'                                  # โฟลเดอร์แอป (ไม่มีจุด = ไม่โดนแปลงเป็นลิงก์)
$cand = @((Join-Path (Join-Path $app 'files') 'config.json'), (Join-Path $app 'config.json'))
$cfg  = $cand | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $cfg) { throw 'ไม่พบ config.json - ระบุพาธเองในบรรทัด $cfg' }
Write-Host ('config: ' + $cfg)

Copy-Item -LiteralPath $cfg -Destination ($cfg + '.bak-' + (Get-Date -f yyyyMMdd-HHmmss)) -Force
$old = [IO.File]::ReadAllText($cfg)
$new = $old.Replace('32cbd69e4b950bf97679', '34bf7a0ff419a2a775b9')
if ($new -eq $old) { Write-Host 'ไม่มี key เก่าในไฟล์นี้ (อาจแก้ไปแล้ว)' -ForegroundColor Yellow }
[IO.File]::WriteAllText($cfg, $new, (New-Object Text.UTF8Encoding($false)))
Select-String -LiteralPath $cfg -Pattern '34bf7a0ff419a2a775b9' | ForEach-Object { $_.Line.Trim() }
```

---

## ท่อน 3 — หยุด **เฉพาะ** โปรเซสของ notifier (ไม่ฆ่า python ทั้งเครื่อง)

```powershell
Get-CimInstance Win32_Process -Filter "Name like 'py%'" |
  Where-Object { $_.CommandLine -match 'HermesAI|notifier|kick' } |
  ForEach-Object {
      Write-Host ("stopping pid {0}`n  cmd: {1}" -f $_.ProcessId, $_.CommandLine) -ForegroundColor Yellow
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
```

> คัดลอกบรรทัด `cmd:` ไว้ — ใช้เป็นคำสั่งรันในท่อน 4 (ต้องรันด้วยคำสั่งเดิม)

---

## ท่อน 4 — รันใหม่ + ดู **เฉพาะ log ที่ใหม่กว่าตอนนี้** (หัวใจของการแก้ปัญหานี้)

```powershell
$app = 'D:\HermesAI\OBS'
$fld = Join-Path $app 'files'
$log = Join-Path $fld 'kick_notifier.log'
$off = if (Test-Path -LiteralPath $log) { (Get-Item -LiteralPath $log).Length } else { 0 }
Write-Host ('log offset = ' + $off + ' bytes (จะนับเฉพาะที่เพิ่มหลังจากนี้)') -ForegroundColor Cyan

# >>> แทนท่อนนี้ด้วยคำสั่งรันจริงของแอป (ใช้คำสั่งเดิมที่เคยรัน) <<<
# $py  = Join-Path $env:USERPROFILE '.venv'           # แก้ให้ตรงกับ python ของคุณ
# $exe = Join-Path (Join-Path $py 'Scripts') 'python.exe'
# $scr = Join-Path $app 'main.py'
# Start-Process -FilePath $exe -ArgumentList ('"' + $scr + '"') -WorkingDirectory $app `
#   -RedirectStandardOutput (Join-Path $fld 'manual_stdout.log') `
#   -RedirectStandardError  (Join-Path $fld 'manual_stderr.log')

Start-Sleep -Seconds 20

$new = ''
if (Test-Path -LiteralPath $log) {
    $fs = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
    try { $fs.Seek($off, 'Begin') | Out-Null; $sr = New-Object IO.StreamReader($fs); $new = $sr.ReadToEnd(); $sr.Dispose() }
    finally { $fs.Dispose() }
}
$lines = @($new -split "`r?`n" | Where-Object { $_ -ne '' })
Write-Host "บรรทัดใหม่: $($lines.Count)" -ForegroundColor Cyan
$lines | Select-Object -Last 20

if     ($new -match 'pusher:error|4001')                                    { $v = 'KEY_REJECTED' }
elseif ($new -match 'connection_established|subscription_succeeded|ChatMessageEvent') { $v = 'OK' }
else                                                                        { $v = 'NO_FRESH_EVIDENCE' }
$col = if ($v -eq 'OK') { 'Green' } elseif ($v -eq 'KEY_REJECTED') { 'Red' } else { 'Yellow' }
Write-Host ''
Write-Host "VERDICT: $v" -ForegroundColor $col
Write-Host ("RESULT_JSON " + (@{ verdict = $v; log_path = $log; new_lines = $lines.Count;
    rejected = ([regex]::Matches($new, 'pusher:error|4001')).Count } | ConvertTo-Json -Compress))
```

**เกณฑ์**: `VERDICT: OK` = log ใหม่มีบรรทัดยืนยันว่าต่อ Pusher ได้จริง (ไม่ใช่ log เก่า)

---

## ท่อน 5 — เก็บหลักฐานส่งกลับ (ไฟล์เดียว)

> ถ้าโหลดเครื่องมือเต็มได้แล้ว (ท่อน 0) ใช้ตัวช่วยที่ฉลาดกว่า — มันปิดข้อมูลลับให้เองและรวมทุกอย่างในไฟล์เดียว:
> ```powershell
> $h = Join-Path (Join-Path $env:USERPROFILE 'hermes-kick') 'report-evidence.ps1'
> powershell -NoProfile -ExecutionPolicy Bypass -File $h -AppDir 'D:\HermesAI\OBS' -Zip -Open
> ```
> หรือส่งขึ้น GitHub ตรง ๆ (ใช้ credential ของ git ที่มีอยู่ ไม่ต้องวาง token ในแชท):
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File $h -AppDir 'D:\HermesAI\OBS' -Repo 'End3lackStaR2/End3lackStaR2'
> ```
>
> หรือยิงเข้า Discord (ไม่ต้องมี connector ของ Arena) — เก็บ webhook URL ในไฟล์ก่อน:
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File $h -AppDir 'D:\HermesAI\OBS' -DiscordWebhookFile (Join-Path $env:USERPROFILE 'hook.txt')
> ```

```powershell
$app  = 'D:\HermesAI\OBS'
$fld  = Join-Path $app 'files'
$desk = [Environment]::GetFolderPath('Desktop')
$out  = Join-Path $desk ('hermes-evidence-' + (Get-Date -f yyyyMMdd-HHmmss) + '.txt')

$body = @()
$body += '### RESULT (จากท่อน 4)'
$body += 'VERDICT: ' + (Read-Host 'วางค่า VERDICT จากท่อน 4 แล้วกด Enter')
$body += '### key probe (ท่อน 1)'
$body += 'ผล: ' + (Read-Host 'KEY-OK / KEY-REJECTED / NO-ANSWER')

foreach ($name in @('kick_notifier.log', 'manual_stdout.log', 'manual_stderr.log')) {
    $f = Join-Path $fld $name
    if (Test-Path -LiteralPath $f) {
        $body += ('### ' + $f + '  (bytes=' + (Get-Item -LiteralPath $f).Length + ', last_write=' + (Get-Item -LiteralPath $f).LastWriteTime + ')')
        $body += (Get-Content -LiteralPath $f -Tail 20)
    }
}
$body | Set-Content -LiteralPath $out -Encoding UTF8
notepad $out
```

---

## ถ้า Kick หมุน key อีกครั้ง

1. เช็คประกาศ: เปิดเบราว์เซอร์ไปที่ `x.com/notjoker` (หรือค้น "Kick pusher app key")
2. เอา key ใหม่มาแทนที่ในทุกคำสั่งข้างบน (ท่อน 1, 2) และเพิ่มเป็นตัวหลัก
3. **อย่าลบ key เก่า** — เก็บไว้เป็น fallback 1 ตัวเสมอ

## เทียบกับเครื่องมือเต็ม (hermes.ps1)

| เรื่อง | ท่อน manual นี้ | hermes.ps1 |
| --- | --- | --- |
| ทดสอบ key สด | ทำได้ (ท่อน 1) | ทำได้ + ทดสอบทั้ง 2 key + subscribe เข้า chatroom จริง |
| หา path เอง | ต้องรู้เอง | ค้น `config.json`, log, python, คำสั่งรันให้ |
| นับ log ใหม่ | ทำได้ (ท่อน 4) | ทำได้ + แยก old/new ให้ด้วย |
| รีสตาร์ท | ต้องคัดลอกคำสั่งเอง (ท่อน 3→4) | หยุด + รีสตาร์ทด้วยคำสั่งเดิมให้อัตโนมัติ |
| รายงาน | ท่อน 5 | เขียน `hermes_report_TH.txt` + `hermes_report.json` + `RESULT_JSON` |
