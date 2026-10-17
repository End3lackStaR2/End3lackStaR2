# FIX NOW — ปิดเคส Kick notifier (ไม่ต้องโหลดอะไรเลย)

ใช้เมื่อ: **config แก้เป็น key ใหม่แล้ว, token สด, แต่ WebSocket ยังโดนปฏิเสธ** เพราะ "โปรเซสเก่ายังรันอยู่"

---

## ทางที่สั้นที่สุด: ดับเบิลคลิก `run-fix` (ไม่ต้องพิมพ์อะไรเลย)

มีไฟล์ **`run-fix.cmd`** อยู่ในโฟลเดอร์ `hermes` — ดับเบิลคลิกแล้วมันจะ

1. หา clone ของ repo ในเครื่อง (`%USERPROFILE%\ebs`) ถ้าไม่มีก็โหลดให้ (depth 1, เฉพาะ branch นี้)
2. อัปเดตให้เป็นเวอร์ชันล่าสุด
3. รัน `fix-now.ps1` ด้วย `-ExecutionPolicy Bypass` (ไม่โดนบล็อกสคริปต์)
4. แสดง **VERDICT** แล้วหยุดรอให้อ่าน

```bat
run-fix.cmd            :: ทำจริง (ถามก่อนฆ่าโปรเซส)
run-fix.cmd -DryRun    :: ดูก่อนว่าาจะทำอะไร ไม่แก้อะไร
run-fix.cmd -WaitSeconds 60
```

> ถ้าโหลด repo มาเป็น zip แล้ว Windows ติ "Mark of the Web" ให้ปลดก่อน:
> `Get-ChildItem -LiteralPath (Join-Path $env:USERPROFILE 'ebs') -Recurse | Unblock-File`

ผลลัพธ์ที่ต้องเห็นคือ **`VERDICT: OK`** (exit code 0) — นั่นคือจุดปิดเคส

> บล็อกนี้ copy-paste ได้ทั้งก้อน ไม่มีพาธแบบ `\ชื่อ.ext` (กันแชทแปลงเป็นลิงก์) ไม่มี `taskkill /IM python.exe /F`
> และ **ไม่ลบ log** — มันจำขนาด log ก่อนรีสตาร์ท แล้วอ่านเฉพาะไบต์ที่เพิ่มมาหลังจากนั้น

---

## ท่อนเดียวจบ (วางทั้งบล็อก) — *ไม่แนะนำแล้ว*

> ⚠️ บล็อกที่วางจากแชทเคยพังมาแล้ว 3 ครั้ง (บรรทัดหาย, `$app`/`$log` ไม่ถูกกำหนด, `??` ของ PS7)
> ใช้ `run-fix.cmd` ด้านบนแทน ถ้าจำเป็นต้องวางจริง ให้ไล่เช็ค 4 บั๊กด้านล่างก่อน

```powershell
# ===================== 0) ตั้งค่า =====================
$app  = 'D:\HermesAI\OBS'
$fld  = Join-Path $app 'files'
$log  = Join-Path $fld ('kick_' + 'notifier' + '.log')
$pyEx = '.' + 'py'

Write-Host ("app: " + $app)      -ForegroundColor Cyan
Write-Host ("log: " + $log)      -ForegroundColor Cyan

# ===================== 1) key เก่าอยู่ใน "โค้ด" หรือไม่ =====================
# ถ้าเจอ key เก่าในไฟล์โค้ด แก้ config.json จะไม่มีผลเลย - ต้องแก้ที่ไฟล์นั้น
$hits = @()
Get-ChildItem -LiteralPath $app -Recurse -File -Depth 2 -ErrorAction SilentlyContinue |
  Where-Object { $_.Extension -in @($pyEx, '.json', '.ini', '.yaml', '.yml') -and $_.Length -lt 2MB } |
  ForEach-Object {
      $m = Select-String -LiteralPath $_.FullName -Pattern '32cbd69e4b950bf97679' -ErrorAction SilentlyContinue
      if ($m) { $hits += $_.FullName }
  }
if ($hits.Count -gt 0) {
    Write-Host '!!! พบ key เก่าในไฟล์เหล่านี้ (แก้ config อย่างเดียวไม่พอ):' -ForegroundColor Red
    $hits | ForEach-Object { Write-Host ('    ' + $_) -ForegroundColor Red }
} else {
    Write-Host 'ไม่พบ key เก่าในโค้ด/config (ดี) - ปัญหาคือโปรเซสเก่ายังรันอยู่' -ForegroundColor Green
}

# ===================== 2) ดูโปรเซส python ที่เกี่ยวกับ notifier =====================
$procs = @(Get-CimInstance Win32_Process -Filter "Name like 'py%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'HermesAI' -or $_.CommandLine -match 'notifier' -or $_.CommandLine -match 'kick') })

if ($procs.Count -eq 0) {
    Write-Host 'ไม่พบโปรเซส python ของ notifier (อาจตายไปแล้ว)' -ForegroundColor Yellow
} else {
    Write-Host ("พบ " + $procs.Count + " โปรเซส:") -ForegroundColor Yellow
    $procs | ForEach-Object { Write-Host ("  pid " + $_.ProcessId + "  " + $_.CommandLine) }
}

# ===================== 3) หา entry script + python + working dir =====================
$cands = @(Get-ChildItem -LiteralPath $app -Recurse -File -Depth 2 -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -eq $pyEx -and $_.FullName -notmatch '\\(\.venv|venv|site-packages|__pycache__)\\' })
$entry = $cands | Where-Object { $_.Name -match 'notifier|kick' } | Sort-Object Length -Descending | Select-Object -First 1
if (-not $entry) { $entry = $cands | Sort-Object Length -Descending | Select-Object -First 1 }

if (-not $entry) {
    Write-Host 'หาไฟล์ script ไม่เจอ - หยุดตรงนี้ แล้วแจ้ง path ของ script มาให้ผม' -ForegroundColor Red
    return
}
Write-Host ("entry: " + $entry.FullName) -ForegroundColor Cyan

$py = $null
foreach ($rel in @('venv', '.venv')) {
    foreach ($root in @($app, (Split-Path -Parent $app))) {
        $cand = Join-Path (Join-Path (Join-Path $root $rel) 'Scripts') 'python.exe'
        if (-not $py -and (Test-Path -LiteralPath $cand)) { $py = $cand }
    }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) {
    Write-Host 'หา python ไม่เจอ - แจ้ง path ของ python.exe มาให้ผม' -ForegroundColor Red
    return
}
Write-Host ("python: " + $py) -ForegroundColor Cyan

# working dir: ถ้าโฟลเดอร์แม่ของ script มี backend/ ให้รันจากโฟลเดอร์แม่ (import แบบ package)
$cwd = $app
if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $entry.DirectoryName) 'backend')) {
    $cwd = Split-Path -Parent $entry.DirectoryName
} elseif (Test-Path -LiteralPath (Join-Path $entry.DirectoryName 'backend')) {
    $cwd = $entry.DirectoryName
}
Write-Host ("cwd: " + $cwd) -ForegroundColor Cyan

# ===================== 4) หยุดเฉพาะโปรเซสที่ใช่ (ถามก่อน) =====================
if ($procs.Count -gt 0) {
    $ans = Read-Host ('หยุด ' + $procs.Count + ' โปรเซสข้างบนหรือไม่? (y/N)')
    if ($ans -eq 'y') {
        $procs | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host ("  stopped pid " + $_.ProcessId) -ForegroundColor Yellow
        }
        Start-Sleep -Seconds 2
    } else {
        Write-Host 'ข้ามการหยุดโปรเซส (จะรันตัวใหม่ทับ - อาจชนกัน)' -ForegroundColor Yellow
    }
}

# ===================== 5) จำ offset ของ log แล้วเริ่มใหม่ =====================
$off = 0
if (Test-Path -LiteralPath $log) { $off = (Get-Item -LiteralPath $log).Length }
Write-Host ("log offset ก่อนรีสตาร์ท: " + $off + " bytes (จะนับเฉพาะที่เพิ่มหลังจากนี้)") -ForegroundColor Cyan

$outF = Join-Path $fld ('hermes_' + 'run_out' + '.log')
$errF = Join-Path $fld ('hermes_' + 'run_err' + '.log')
$p = Start-Process -FilePath $py -ArgumentList ('"' + $entry.FullName + '"') -WorkingDirectory $cwd `
      -RedirectStandardOutput $outF -RedirectStandardError $errF -PassThru -WindowStyle Hidden
Write-Host ("started pid " + $p.Id) -ForegroundColor Green

# ===================== 6) รอแล้วอ่าน "เฉพาะไบต์ใหม่" =====================
Start-Sleep -Seconds 20

$new = ''
if (Test-Path -LiteralPath $log) {
    $fs = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
    try {
        $fs.Seek($off, 'Begin') | Out-Null
        $sr = New-Object IO.StreamReader($fs)
        $new = $sr.ReadToEnd()
        $sr.Dispose()
    } finally { $fs.Dispose() }
}
$lines = @($new -split "`r?`n" | Where-Object { $_ -ne '' })

Write-Host ('log ไบต์ใหม่: ' + $new.Length + '   บรรทัดใหม่: ' + $lines.Count) -ForegroundColor Cyan
if ($lines.Count -gt 0) { $lines | Select-Object -Last 20 }

if ($lines.Count -eq 0) {
    Write-Host '--- stdout ของโปรเซสใหม่ ---' -ForegroundColor Yellow
    if (Test-Path -LiteralPath $outF) { Get-Content -LiteralPath $outF -Tail 20 }
    Write-Host '--- stderr ของโปรเซสใหม่ ---' -ForegroundColor Yellow
    if (Test-Path -LiteralPath $errF) { Get-Content -LiteralPath $errF -Tail 20 }
}

# ===================== 7) สรุปผล =====================
$v = 'NO_FRESH_EVIDENCE'
if ($new -match 'pusher:error|4001') { $v = 'KEY_REJECTED' }
elseif ($new -match 'connection_established|subscription_succeeded|ChatMessageEvent') { $v = 'OK' }

$col = if ($v -eq 'OK') { 'Green' } elseif ($v -eq 'KEY_REJECTED') { 'Red' } else { 'Yellow' }
Write-Host ''
Write-Host ('VERDICT: ' + $v) -ForegroundColor $col
Write-Host ('RESULT_JSON ' + (@{ verdict = $v; new_bytes = $new.Length; new_lines = $lines.Count;
    entry = $entry.FullName; cwd = $cwd; log = $log } | ConvertTo-Json -Compress))
```

---

## ⚠️ 4 บั๊กที่ทำให้บล็อกที่คัดลอกมา "รันไม่ผ่าน" (ตรวจด้วย linter แล้ว)

ถ้าคุณเจอบล็อกเวอร์ชันที่ถูกตัด/จัดรูปใหม่ ให้เช็ค 4 จุดนี้ก่อน — ทั้ง 4 ทำให้ PowerShell ล้มทันที:

| # | บรรทัดที่พัง | ทำไมพัง | ที่ถูก |
| --- | --- | --- | --- |
| 1 | `$py = $py ?? (Get-Command python).Source` | `??` เป็นของ **PowerShell 7+** เท่านั้น บน PS 5.1 (ค่าตั้งต้นของ Windows) เป็น **parse error ทั้งสคริปต์** | `if (-not $py) { $cmd = Get-Command python -ErrorAction SilentlyContinue; if ($cmd) { $py = $cmd.Source } }` |
| 2 | `$cwd = $app` | `$app` **ไม่เคยถูกกำหนด** (บรรทัดตั้งค่าหายไป) → `-WorkingDirectory $null` | กำหนด `$app = 'D:\HermesAI\OBS'` ที่ต้นบล็อก |
| 3 | `[IO.File]::Open($log, ...)` | `$log` **ไม่เคยถูกกำหนด** (ใช้ literal เฉพาะใน `if` แต่ส่ง `$log`) → `ArgumentException` | `$log = Join-Path $fld ('kick_' + 'notifier' + '.log')` แล้วใช้ `$log` ตลอด |
| 4 | `-ForegroundColor (if($v -eq 'OK'){'Green'}...)` | PowerShell **ไม่มี if เป็น expression** ในวงเล็บ → parse error | `$col = if ($v -eq 'OK') { 'Green' } elseif ($v -eq 'KEY_REJECTED') { 'Red' } else { 'Yellow' }` แล้ว `Write-Host ... -ForegroundColor $col` |

> ทั้ง 3 ตัว ($app, $log, สี) และ `??` ถูกแก้ในไฟล์ **[fix-now.ps1](fix-now.ps1)** แล้ว — ใช้ไฟล์นั้นแทนการวางบล็อกจากแชท

## วิธีที่แนะนำ: รันเป็นไฟล์ (ไม่มีอะไรถูกตัดหาย)

```powershell
# ถ้ามี repo นี้อยู่ในเครื่องแล้ว
$f = Get-ChildItem -LiteralPath (Join-Path $env:USERPROFILE 'hermes-kick\hermes') -Filter 'fix-now.ps1' -ErrorAction SilentlyContinue
if (-not $f) { $f = Get-ChildItem -Path $env:USERPROFILE -Filter 'fix-now.ps1' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 }
if (-not $f) { Write-Host 'ไม่พบไฟล์ - ใช้วิธีคัดลอกบล็อกด้านบน หรือโหลด zip' -ForegroundColor Yellow } else {
    powershell -NoProfile -ExecutionPolicy Bypass -File $f.FullName -DryRun          # ดูก่อน ไม่แก้อะไร
    powershell -NoProfile -ExecutionPolicy Bypass -File $f.FullName                  # ทำจริง (ถามก่อนฆ่าโปรเซส)
    powershell -NoProfile -ExecutionPolicy Bypass -File $f.FullName -Force -Quiet    # ไม่ถาม (สำหรับ Task Scheduler)
}
```

ไฟล์นี้รองรับ: `-AppDir`, `-LogPath`, `-EntryScript`, `-PythonPath`, `-WaitSeconds`, `-TailLines`, `-NoKill`, `-DryRun`, `-Force`, `-Quiet`
และเขียนรายงานไว้ที่ `files\hermes_fix_now_report.txt`

---

## อ่านผลยังไง

| VERDICT | หมายความว่า | ทำต่อ |
| --- | --- | --- |
| **`OK`** | log **ใหม่** มี `pusher:connection_established` หรือ `subscription_succeeded` หรือ `ChatMessageEvent` → ต่อ Pusher ได้ด้วย key ใหม่ | จบ — เปิด chat ใน OBS ดูได้เลย |
| **`KEY_REJECTED`** | log ใหม่ยังมี `pusher:error` / `4001` → **อ่านข้อความในบรรทัดนั้น** ว่ามันปฏิเสธ key อะไร | ถ้าเป็น key **เก่า** = ยังมีที่อ่าน key เก่าอยู่ (ดูท่อน 1: ไฟล์ที่ขึ้นสีแดง) / ถ้าเป็น key **ใหม่** = Kick หมุน key อีก ต้องอัปเดต |
| **`NO_FRESH_EVIDENCE`** | 20 วิแล้ว log ไม่มีบรรทัดใหม่เลย | ดู `--- stdout/stderr ---` ที่บล็อกพิมพ์ให้ (บรรทัดบน) — มักมี ImportError / path ผิด |

## ตรวจซ้ำแบบสด ๆ (หลัง VERDICT)

```powershell
$log = Join-Path (Join-Path 'D:\HermesAI\OBS' 'files') ('kick_' + 'notifier' + '.log')
Get-Content -LiteralPath $log -Tail 20 -Wait
```

## กติกาสำคัญ 3 ข้อ (จุดที่พลาดซ้ำหลายรอบ)

1. **log เป็นไฟล์ append** — บรรทัดเก่า (`15:16:26`, `4001`) ยังอยู่ตลอด การเห็นมัน **ไม่ใช่** หลักฐานว่าแอปยังพัง
   หลักฐานที่ใช้ได้คือ "ไบต์ที่เพิ่มหลัง offset" เท่านั้น (บล็อกนี้ทำให้นับจาก offset ให้แล้ว)
2. **อย่าใช้ `taskkill /IM python.exe /F`** — มันฆ่า python ทุกตัวในเครื่อง (รวม tool อื่นที่คุณเปิดอยู่)
   ให้ฆ่าเฉพาะ pid ที่ CommandLine มี `HermesAI`/`notifier`/`kick`
3. **ดู `VERDICT` ไม่ใช่ "รอ 10-15 วินาที"** — ถ้าไม่มีบรรทัดใหม่ ให้ไปดู stdout/stderr ของโปรเซสใหม่ทันที (บล็อกนี้พิมพ์ให้)

## ถ้าอยากให้ตรวจซ้ำแบบไม่ต้องคิดเอง

```powershell
# ต้องมี hermes.ps1 (จากคิว bus หรือโหลด zip) - มันทำทุกอย่างในบล็อกนี้ + รายงานภาษาไทย
$h = Join-Path (Join-Path $env:USERPROFILE 'hermes-kick') 'hermes.ps1'
powershell -NoProfile -ExecutionPolicy Bypass -File $h fix -WaitSeconds 90 -Open
```
