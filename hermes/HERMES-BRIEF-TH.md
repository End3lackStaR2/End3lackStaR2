# BRIEF สำหรับ agent (Hermes) — งาน: ทำให้ Kick notifier ใช้งานได้หลัง Kick หมุน Pusher key

เอกสารนี้คือ "ใบสั่งงาน + เกณฑ์ตรวจรับ" ที่ agent (หรือคน) ต้องทำตาม **ห้ามสรุปผลจากการเดา
หรืออ่าน log บรรทัดเก่า** งานจะถือว่าเสร็จก็ต่อเมื่อมีหลักฐานตามเกณฑ์ด้านล่าง

---

## 0) ข้อเท็จจริงที่ยืนยันแล้ว (ห้ามเถียงด้วยความจำรุ่นเก่า)

| เรื่อง | ค่า |
| --- | --- |
| key เก่า (ถูกปิด) | `32cbd69e4b950bf97679` |
| key ใหม่ (ใช้แทน) | `34bf7a0ff419a2a775b9` |
| cluster | `us2` (URL: `wss://ws-us2.pusher.com/app/<key>?protocol=7&client=js&version=8.4.0&flash=false`) |
| ที่มา | ประกาศทีม Kick 8 ต.ค. 2026 — https://x.com/notjoker (“will be revoking the existing Pusher app key … new app key is 34bf7a0ff419a2a775b9”) |
| แพตช์อ้างอิงที่ทดสอบแล้ว | https://github.com/Andro2k/MiniKick/pull/45 (10 ต.ค. 2026) |
| อาการของแอปถ้าใช้ key เก่า | `pusher:error code=4001 ... App key ... not in this cluster` แล้ว socket ปิด → reconnect loop → chat ไม่ขึ้น (token ยังสดก็ไม่ช่วย) |

**ห้ามเดา key อื่น** และห้ามใส่ key ลงในที่ที่ commit เข้า git

---

## 1) เกณฑ์ตรวจรับ (Acceptance criteria) — ต้องมีหลักฐานทุกข้อ

| # | เกณฑ์ | วิธีเก็บหลักฐาน |
| --- | --- | --- |
| A1 | `D:\HermesAI\OBS\kick_pusher.json` มี 2 key โดย `keys[0].key = 34bf7a0ff419a2a775b9`, `keys[1].key = 32cbd69e4b950bf97679`, `cluster = "us2"` | แนบเนื้อไฟล์ |
| A2 | `config.json` **ไม่มี** key เก่าอีก (ค้นหาได้ 0 ครั้ง) และ `cluster = "us2"` | `Select-String -Path <config> -Pattern 32cbd69e4b950bf97679` → ต้องว่าง |
| A3 | ไม่มีโปรเซส python ตัวเก่าค้างอยู่ และตัวใหม่มีเวลาสตาร์ทใหม่จริง | `Get-CimInstance Win32_Process -Filter "Name like 'py%'" | Select ProcessId, CreationDate, CommandLine` |
| A4 | log **บรรทัดใหม่หลังเวลารีสตาร์ท** ต้องมีอย่างน้อยหนึ่งใน: `pusher:connection_established` / `subscription_succeeded` / `ChatMessageEvent` **และต้องไม่มี** `4001` | ดูข้อ 3 (นับจาก offset เท่านั้น) |
| A5 | socket ต่อได้จริงอย่างน้อย 1 ใน 2 key | `python tools\kick_pusher_probe.py --json` → `"connected": true` |
| A6 (ถ้าแก้โค้ด) | `backend/providers/chat/kick_ws_provider.py` หมุน key ได้เองเมื่อโดน 4001/4003/4009 | `python -m unittest test_patch_kick_app` → `OK` (10 เทสต์) |

ถ้าข้อใดยังไม่ผ่าน → **ห้ามรายงานว่าเสร็จ** ให้รายงานว่า "ยังไม่ผ่านข้อ X เพราะ …"

---

## 2) คำสั่งที่ต้องรัน (เรียงตามลำดับ) + ผลที่ต้องได้

```powershell
$d = "$env:USERPROFILE\hermes-kick"     # โฟลเดอร์เครื่องมือ (มี hermes.ps1 อยู่)

# (1) ตรวจสภาพแวดล้อมก่อน — ต้องเห็น path ของ log/config/python ที่ "มีอยู่จริง"
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" doctor

# (2) ซ่อม + รีสตาร์ท + รอหลักฐานใหม่ (ไม่ต้องรอ 10-15 วิแบบเดา ๆ แล้ว)
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" fix -WaitSeconds 90 -Open
#   ผลที่ต้องได้: VERDICT: OK  (ถ้าได้ KEY_REJECTED ให้ดูข้อ 4)

# (3) ทำให้ทนการ rotate ครั้งต่อไป (สำรอง + ตรวจ syntax ก่อนเขียน)
python "$d\patch_kick_app.py" --root D:\HermesAI\OBS --plan
python "$d\patch_kick_app.py" --root D:\HermesAI\OBS
python -m unittest test_patch_kick_app        # ต้องได้ OK
```

**รายงานที่ต้องส่งกลับ** (คัดลอกผลลัพธ์จริง ไม่ต้องสรุปความ): exit code, บรรทัด `VERDICT: …`,
ผลทดสอบ key ทั้ง 2 ตัว, 3-5 บรรทัดสุดท้ายของ log ที่ **ใหม่กว่า** เวลารีสตาร์ท, และ path ของ
`hermes_report_TH.txt`

---

## 3) กติกาการอ่าน log (จุดที่รอบก่อน ๆ พลาด)

1. log เป็นไฟล์ **append** — บรรทัดเก่า (เช่น `15:16:26`) ยังอยู่เสมอ การเห็นบรรทัดเก่า **ไม่ใช่** หลักฐานว่าแอปยังพัง
2. ก่อนรีสตาร์ทให้จำค่า `(Get-Item <log>).Length` แล้วหลังรีสตาร์ทอ่านเฉพาะส่วนที่เกินค่านั้น (หรือใช้ `hermes.ps1 fix` ที่ทำให้แล้ว)
3. เวลาใน log อาจเป็น **UTC** — อย่าเทียบเวลานาฬิกาเครื่องตรง ๆ ให้เทียบจากลำดับ/offset
4. ตัวอักษร "กี่นาทีที่แล้ว" หรือ "รออีก 10-15 วิ" **ไม่ใช่หลักฐาน** ต้องมีบรรทัดจริงหรือ `VERDICT` จริง

คำสั่งตรวจ log ที่ถูกต้อง (path จะถูกบอกโดย `hermes.ps1 logs`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" logs -TailLines 30
# เทียบเท่า:  Get-Content '<path ที่สคริปต์บอก>' -Tail 30 -Wait
```

---

## 4) แผนสำรองเมื่อไม่ผ่าน

| ผล | ต้องทำอะไรต่อ |
| --- | --- |
| `KEY_REJECTED` ซ้ำด้วย key ใหม่ | Kick อาจหมุนอีก → เปิด https://x.com/notjoker ยืนยัน key ปัจจุบัน แล้วแก้ `kick_pusher.json` / `config.json` (ใช้ `hermes.ps1 fix` อีกครั้ง) และอย่าลบ fallback |
| `NO_FRESH_EVIDENCE` | แอปอาจไม่เขียน log (หรือเขียนที่อื่น) → ดู `files\hermes_stdout.log` และ `files\hermes_stderr.log`, หรือรันแอปในหน้าต่าง console, หรือรัน `hermes.ps1 watch` ทิ้งไว้ |
| ต่อ Pusher ไม่ได้เลยแม้แต่ key เดียว | ตรวจ firewall/TLS/proxy ของเครื่อง — แอปก็จะพังด้วยเหตุเดียวกัน (ไม่ใช่ปัญหา key) |
| แอปยัง reconnect loop | ตรวจว่าโปรเซสเก่าตายจริง (A3) และแอปอ่าน key จากที่ที่เราแก้ (ตรวจว่ามีโค้ดอ่าน `kick_pusher.json`/config ที่ถูกใช้จริง) — อย่าแก้ซ้ำโดยไม่ดูว่าอ่านไฟล์ไหน |

---

## 5) สิ่งที่ห้ามทำ

- ห้าม commit ไฟล์ที่มี key/token จริง หรือไฟล์ `.bak-*`, `kick_pusher.json`, log รายงาน เข้า git
- ห้ามลบ key เก่าออกจากรายการ fallback (โค้ดต้องเหลือทางถอย 1 ทาง)
- ห้ามแก้ไฟล์โดยไม่สำรอง `.bak-` และห้ามเขียนทับ `config.json` ถ้า parse ไม่ผ่าน
- ห้ามรายงานว่าเสร็จถ้า log ใหม่ไม่มีบรรทัดยืนยัน (ข้อ A4)
