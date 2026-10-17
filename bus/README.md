# bus — เริ่มใช้เร็ว ๆ (ฝั่งเครื่องคุณ / Friday)

กล่องจดหมายกลางในรีโปนี้ ให้ **Arena** (คลาวด์) สั่งงาน **Friday** (เครื่องคุณ) และรับผลกลับ
โดยไม่ต้องเปิดพอร์ต ไม่ต้องมี MCP — รายละเอียดสัญญาเต็มอยู่ใน [PROTOCOL.md](PROTOCOL.md)
และสิ่งที่ Arena จะ/จะไม่ทำอยู่ใน [ARENA-WORKFLOW.md](ARENA-WORKFLOW.md)

---

## เช็กลิสต์ตั้งค่า (ทำครั้งเดียว)

```powershell
# 0) ต้องมีของ 2 อย่าง
git --version        # ต้องมี git
python --version     # ต้องมี python 3.8+  (ถ้าใช้ py launcher ให้แก้ program ใน actions.json เป็น "py")

# 1) วาง bus ไว้ที่ไหนก็ได้ เช่น %USERPROFILE%\hermes-bus
$base = Join-Path $env:USERPROFILE 'hermes-bus'
$f    = Get-ChildItem -LiteralPath (Join-Path $base 'bus') -Filter 'friday_*.py' | Select-Object -First 1

# 2) ตั้งค่า (จะโคลน/ตรวจ repo + สร้าง bus\actions.json + bus\bus.config.json ให้)
python $f.FullName init --dir $base

# 3) เปิดไฟล์นี้แล้วแก้ path ให้ตรงเครื่องคุณ (สำคัญ!)
#    default_app_dir = D:\HermesAI\OBS
#    hermes_dir      = ที่เก็บชุดเครื่องมือ hermes (ที่มี hermes.ps1)
$cfg = Join-Path (Join-Path $base 'bus') 'bus.config.json'
notepad $cfg

# 4) ตรวจว่า whitelist ถูกต้อง (ไฟล์นี้คือด่านความปลอดภัย - gitignore ไว้)
$act = Join-Path (Join-Path $base 'bus') 'actions.json'
notepad $act

# 5) ส่งสัญญาณว่าเครื่องพร้อม แล้วเริ่มทำงาน
python $f.FullName status --dir $base      # heartbeat ให้ Arena เห็น
python $f.FullName once  --dir $base       # ทำ 1 งาน (ถ้ามีในคิว)
python $f.FullName loop  --dir $base       # เฝ้าคิวทุก 20 วิ (Ctrl+C เพื่อหยุด)
```

> ถ้าไม่ตั้งใจให้รันงานใด ๆ: เปิด `bus.config.json` แล้วตั้ง `"read_only": true` → Friday จะดึงอย่างเดียว

## ให้ Friday ทำงานตลอด (ไม่ต้องเปิดหน้าต่างค้าง)

```powershell
$base = Join-Path $env:USERPROFILE 'hermes-bus'
$f    = Get-ChildItem -LiteralPath (Join-Path $base 'bus') -Filter 'friday_*.py' | Select-Object -First 1
$py   = (Get-Command python).Source

# ตัวเลือก A: Scheduled Task ตอนล็อกอิน
$act = New-ScheduledTaskAction -Execute $py -Argument ('"' + $f.FullName + '" loop --dir "' + $base + '"')
$trg = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'friday-bus' -Action $act -Trigger $trg -RunLevel Highest -Force
# ยกเลิก: Unregister-ScheduledTask -TaskName 'friday-bus' -Confirm:$false

# ตัวเลือก B: ไฟล์ .cmd วางใน shell:startup (ง่ายกว่า ไม่ต้องสิทธิ์ admin)
```

## โฟลเดอร์ต่าง ๆ หมายถึงอะไร

| โฟลเดอร์ | ใครเขียน | ความหมาย |
| --- | --- | --- |
| `queue/` | Arena | งานที่รอทำ (ยังไม่ถูกจอง) |
| `claimed/` | Friday | งานที่ถูกจองแล้ว + lease (มี `claim.lease_expires_utc`) |
| `results/` | Friday | ผลลัพธ์ (status, exit code, output tail, เวลา) |
| `done/` | Friday | ไฟล์งานที่归档แล้ว (audit) |
| `status/` | Friday | heartbeat (`last_poll_utc`, จำนวนงานสำเร็จ/ล้มเหลว) |
| `requests/` | Friday | คำถามถึง Arena |
| `answers/` | Arena | คำตอบของ Arena |

## อ่านผลลัพธ์

```powershell
$base = Join-Path $env:USERPROFILE 'hermes-bus'
python $f.FullName list --dir $base                       # สรุปคิว/งานที่จอง/ผลล่าสุด
$r = Join-Path (Join-Path $base 'bus') 'results'          # เปิดดูรายไฟล์ได้
Get-ChildItem -LiteralPath $r -Filter '*.json' | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

`status` ที่ Friday จะรายงานกลับ: `ok` | `failed` | `timeout` | `refused` | `invalid` | `error`
(`refused` = action ไม่อยู่ใน whitelist ของคุณ, `invalid` = ค่าพารามิเตอร์ไม่ผ่าน pattern — ทั้งสองกรณี **ไม่มีการรันอะไรเลย**)

## แก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ/วิธีแก้ |
| --- | --- |
| `push failed` ตอน status | ยังไม่ยืนยันสิทธิ์ git → รัน `git push` ในโฟลเดอร์ bus ด้วยมือ 1 ครั้ง (จะถาม credential ครั้งเดียว) |
| `action '...' is not in bus/actions.json` | Arena ส่งงานที่คุณยังไม่ whitelist → เพิ่ม action ใน `bus/actions.json` แล้ว commit? **ไม่ต้อง** ไฟล์นี้เก็บในเครื่องเท่านั้น |
| `invalid` + `unsupported characters` | ค่าพารามิเตอร์มีอักขระต้องห้าม (`"` `;` `|` `&` `` ` ``) → ตั้ง `pattern` ให้ action ถ้าต้องรับค่าซับซ้อน |
| `program not found: python` | ใช้ `py` หรือใส่ full path เช่น `C:\Python311\python.exe` ในฟิลด์ `program` |
| งานค้างใน `claimed/` | Friday ตายกลางงาน → เมื่อพ้น `lease_seconds` รอบถัดไปจะดึงกลับเข้า `queue/` เอง (นับใน `reclaims`) |
| `poll: idle` ทุกครั้ง | ยังไม่มีงานในคิว — ปกติ ให้ Arena สร้างงาน (บอก Arena ว่า "สั่ง Friday ทำ ...") |

## ความปลอดภัย (ย้ำสั้น ๆ)

- ไฟล์งานใน `queue/` ถือเป็น **ข้อมูลที่ไม่น่าเชื่อถือ** → รันได้เฉพาะ action ใน `actions.json` ของคุณ
- รันแบบไม่มี shell, มี timeout ทุก action, และปิดความลับก่อน push (token/JWT/webhook → `<redacted>`)
- `actions.json`, `bus.config.json`, `.local/` **ไม่ถูก commit** (อยู่ใน .gitignore ของ bus)
- repo นี้เป็น public → ถ้าอนาคตงานมีข้อมูลอ่อนไหว ให้เปลี่ยน `repo_url` ไป repo ส่วนตัว

## ให้ Friday ทำงานเอง (autopilot)

`autopilot.py` ทำให้ Friday ตัดสินใจเองได้ **ภายใน policy ที่คุณอนุมัติครั้งเดียว** —
ประเมินผลจริงด้วย `accept` criteria, retry/goto/escalate, เขียนรายงานและข้อเสนอปรับปรุงเอง

- อ่านภาพรวม + ข้อจำกัดของ Arena: [`AUTONOMY-TH.md`](AUTONOMY-TH.md)
- เริ่มจาก: คัดลอก `policy.json.example` → `policy.json` แล้ว `run --workflow kick_repair --dry-run`
- ทดสอบ: `python autopilot.py selftest` (8) และ `python -m unittest -v test_autopilot_e2e` (10, จาก `tests/`)

> Arena ไม่มี API/MCP — autonomy คือ policy + evaluation + queued questions/reports
> ถ้าต้องการให้ Friday **ถาม Arena สด ๆ** ใช้ `bridge/` (ส่วนขยายเบราว์เซอร์) + `--arena-wait` — ดู [`bridge/README-TH.md`](../bridge/README-TH.md)
