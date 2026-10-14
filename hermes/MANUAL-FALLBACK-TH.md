# วิธีสำรอง (ไม่ต้องโหลดไฟล์ใด ๆ) — MANUAL FALLBACK

ใช้เมื่อ: ดาวน์โหลดจาก GitHub ไม่ได้ หรือข้อความที่คัดลอกมาโดน markdown ทำให้ URL พัง
ทุกท่อนข้างล่างใช้ PowerShell ที่มีอยู่ใน Windows เท่านั้น **ไม่มี URL ให้ markdown ทำพัง** (ตัว URL ถูกต่อจากชิ้นส่วน)

> ทุกท่อนคัดลอกแยกกันได้ ผลลัพธ์มี `VERDICT:` ให้คัดลอกกลับเสมอ

---

## ท่อน 0 (ไม่บังคับ) — โหลดเครื่องมือเต็ม (แบบที่ markdown ทำ URL พังไม่ได้)

URL ถูก **ต่อจากชิ้นส่วน** จึงไม่มี `[` `]` `(` `)` ติดมาได้ และมีตัวตรวจว่าไฟล์ที่ได้ "ครบและถูกไฟล์"

ทาง A — **zip จาก codeload** (ตรวจแล้วว่าได้ไฟล์ตรงกับต้นฉบับทุกไบต์ = 69,094 ไบต์):

```powershell
$repo = 'End3lackStaR2/End3lackStaR2'
$sha  = '51cacf730f862ea434dcdea2db58ea7b9461af6e'
$d    = Join-Path $env:USERPROFILE 'hermes-kick'
New-Item -ItemType Directory -Force -Path $d | Out-Null

$zip = Join-Path $env:TEMP 'ebs.zip'
iwr -UseBasicParsing ('https://' + 'codeload.' + 'github.com/' + $repo + '/zip/' + $sha) -OutFile $zip
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $env:TEMP 'ebs') -Force
Copy-Item (Join-Path $env:TEMP ('ebs/End3lackStaR2-' + $sha + '/hermes/*')) $d -Recurse -Force
Copy-Item (Join-Path $env:TEMP ('ebs/End3lackStaR2-' + $sha + '/hermes/tools')) $d -Recurse -Force

$raw = Get-Content (Join-Path $d 'hermes.ps1') -Raw
if ($raw.Length -lt 50000 -or $raw -notmatch 'hermes\.ps1 v' -or $raw -notmatch 'VERDICT') {
    Write-Host 'ไฟล์ไม่ครบ/ผิดไฟล์ - ลองทาง B' -ForegroundColor Yellow
} else {
    Write-Host ('พร้อมใช้: ' + (Join-Path $d 'hermes.ps1') + '  (' + (Get-Item (Join-Path $d 'hermes.ps1')).Length + ' bytes)') -ForegroundColor Green
}
```

ทาง B — ถ้า codeload ถูกบล็อก ใช้ **GitHub API** (คืนไฟล์เป็น base64, ตรวจแล้วว่าตรงกันทุกไบต์):

```powershell
$repo = 'End3lackStaR2/End3lackStaR2'
$sha  = '51cacf730f862ea434dcdea2db58ea7b9461af6e'
$d    = Join-Path $env:USERPROFILE 'hermes-kick'
New-Item -ItemType Directory -Force -Path $d | Out-Null
foreach ($f in @('hermes.ps1', 'tools/patch_kick_app.py', 'tools/kick_pusher_probe.py', 'tools/test_patch_kick_app.py')) {
    $api  = 'https://' + 'api.' + 'github.com/repos/' + $repo + '/contents/hermes/' + $f + '?ref=' + $sha
    $json = (iwr -UseBasicParsing $api).Content | ConvertFrom-Json
    $dest = Join-Path $d ($f -replace '/', '\')
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    [IO.File]::WriteAllBytes($dest, [Convert]::FromBase64String($json.content))
    Write-Host ("wrote {0} ({1} bytes)" -f $dest, (Get-Item $dest).Length)
}
```

ทาง C — วิธีปกติ (ใช้ได้บนเน็ตบ้านทั่วไป แต่ proxied/องค์กรอาจบล็อก):

```powershell
$base = 'github.com/End3lackStaR2/End3lackStaR2/raw/' + '51cacf730f862ea434dcdea2db58ea7b9461af6e' + '/hermes'
iwr -UseBasicParsing ('https://' + $base + '/hermes.ps1') -OutFile (Join-Path $env:USERPROFILE 'hermes-kick\hermes.ps1')
```

จากนั้นใช้คำสั่งนี้ (ไม่มีการพิมพ์ URL ซ้ำอีก):

```powershell
$h = Join-Path $env:USERPROFILE 'hermes-kick\hermes.ps1'
powershell -NoProfile -ExecutionPolicy Bypass -File $h doctor
powershell -NoProfile -ExecutionPolicy Bypass -File $h fix -WaitSeconds 90 -Open
```

> ตรวจสอบจากฝั่งผู้พัฒนา (sandbox ที่เข้าได้เฉพาะ github.com / codeload / api.github.com):
> ทาง A และทาง B ให้ไฟล์ไบต์ตรงกันเป๊ะกับต้นฉบับ (69,094 ไบต์, BOM ถูกต้อง, มี `RESULT_JSON` และตัวสแกนไฟล์)
> ส่วนทาง C (ผ่าน `github.com/.../raw/...`) เป็น 302 ไป `raw.githubusercontent.com` ซึ่งเครือข่ายของผมเข้าไม่ถึง
> จึง **ยืนยันจากฝั่งผมไม่ได้** แต่บนเครื่องบ้านทั่วไปใช้ได้ — ถ้าไม่ได้ให้ใช้ทาง A หรือ B

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
$cfg = 'D:\HermesAI\OBS\files\config.json'
if (-not (Test-Path -LiteralPath $cfg)) { $cfg = 'D:\HermesAI\OBS\config.json' }
Write-Host "config: $cfg"
Copy-Item -LiteralPath $cfg -Destination "$cfg.bak-$(Get-Date -f yyyyMMdd-HHmmss)" -Force
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
$log = 'D:\HermesAI\OBS\files\kick_notifier.log'
$off = if (Test-Path -LiteralPath $log) { (Get-Item -LiteralPath $log).Length } else { 0 }
Write-Host "log offset = $off bytes (จะนับเฉพาะที่เพิ่มหลังจากนี้)" -ForegroundColor Cyan

# >>> แทนบรรทัดนี้ด้วยคำสั่งรันของจริง (คำสั่งเดิมของแอป) <<<
# Start-Process -FilePath 'D:\HermesAI\.venv\Scripts\python.exe' `
#   -ArgumentList '"D:\HermesAI\OBS\main.py"' -WorkingDirectory 'D:\HermesAI\OBS' `
#   -RedirectStandardOutput 'D:\HermesAI\OBS\files\manual_stdout.log' `
#   -RedirectStandardError  'D:\HermesAI\OBS\files\manual_stderr.log'

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
> $h = Join-Path $env:USERPROFILE 'hermes-kick\report-evidence.ps1'
> powershell -NoProfile -ExecutionPolicy Bypass -File $h -AppDir 'D:\HermesAI\OBS' -Zip -Open
> ```
> หรือส่งขึ้น GitHub ตรง ๆ (ใช้ credential ของ git ที่มีอยู่ ไม่ต้องวาง token ในแชท):
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File $h -AppDir 'D:\HermesAI\OBS' -Repo 'End3lackStaR2/End3lackStaR2'
> ```

```powershell
$app = 'D:\HermesAI\OBS'
$out = "$env:USERPROFILE\Desktop\hermes-evidence.txt"
$body = @()
$body += '### RESULT (จากท่อน 4)'
$body += 'VERDICT: ' + (Read-Host 'วางค่า VERDICT จากท่อน 4 (หรือกด Enter เพื่อข้าม)')
foreach ($f in @("$app\files\kick_notifier.log", "$app\files\manual_stdout.log", "$app\files\manual_stderr.log")) {
    if (Test-Path -LiteralPath $f) {
        $body += "### $f (ท้าย 20 บรรทัด)"
        $body += Get-Content -LiteralPath $f -Tail 20
    }
}
$body += '### key probe (ท่อน 1)'
$body += 'KEY-OK / KEY-REJECTED / NO-ANSWER: ' + (Read-Host 'ผลจากท่อน 1')
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
