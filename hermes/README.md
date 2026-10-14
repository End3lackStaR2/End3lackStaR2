# hermes — ชุดซ่อม/ตรวจ Kick Pusher key (สำหรับ HermesAI · D:\HermesAI\OBS)

> **สรุปสาเหตุ**: Kick หมุน (rotate) Pusher app key — key เก่า `32cbd69e4b950bf97679` ถูกปิด
> key ใหม่คือ `34bf7a0ff419a2a775b9` บน cluster `us2`
> (ประกาศโดยทีม Kick วันที่ 8 ต.ค. 2026: https://x.com/notjoker)
>
> อาการของแอป: `pusher:error code=4001 App key ... not in this cluster` แล้ว socket ปิด
> worker ต่อใหม่ไม่หยุด → chat ไม่ขึ้น ทั้งที่ token ยังสด

ชุดนี้ทำ 3 อย่างที่ "แก้ config เฉย ๆ" ทำไม่ได้:

1. **หาที่อยู่จริง** ของ `config.json`, log, python, สคริปต์ notifier (เลิกเดา path — log ที่ `D:/HermesAI/OBS/files/kick_notifier.log` จะถูกยืนยันหรือชี้ตัวใหม่)
2. **ทดสอบ key ทั้ง 2 ตัวแบบสด** ด้วยการต่อ WebSocket ไปที่ Pusher จริง → เลือก key ที่ใช้ได้จาก "หลักฐาน" ไม่ใช่จากการเดา
3. **นับหลักฐานใหม่เท่านั้น**: จำขนาด log ก่อนรีสตาร์ท แล้วอ่านเฉพาะไบต์ที่เพิ่มมาหลังรีสตาร์ท → ไม่มีทาง "อ่าน log บรรทัดเก่าแล้วสรุปผิด" อีก

---

## เริ่มใช้ (คัดลอกวางได้เลย)

```powershell
# 1) โหลดเครื่องมือ (ครั้งเดียว)
$u = 'https://raw.githubusercontent.com/End3lackStaR2/End3lackStaR2/arena/a508b692-end3lackstar2/hermes'
$d = "$env:USERPROFILE\hermes-kick"
New-Item -ItemType Directory -Force -Path $d | Out-Null
iwr -UseBasicParsing "$u/hermes.ps1" -OutFile "$d\hermes.ps1"
iwr -UseBasicParsing "$u/tools/patch_kick_app.py" -OutFile "$d\patch_kick_app.py"

# 2) ตรวจก่อน (ไม่แก้อะไรเลย) — ใช้เวลาประมาณ 30 วินาที
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" doctor

# 3) ซ่อม + รีสตาร์ท + รอหลักฐานใหม่ + เขียนรายงานภาษาไทย
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\hermes.ps1" fix -Open
```

> **บรรทัดส่งกลับ**: ทุกโหมดจะพิมพ์บรรทัดเดียวจบหน้าจอว่า
> `RESULT_JSON {"verdict":"...","working_key":"...","log_path":"...",...}`
> คัดลอกบรรทัดนั้นส่งกลับมาได้เลย — ไม่ต้องสรุปความ (ถ้าอยากได้หลักฐานเพิ่ม ใช้ `-Open` เปิดรายงานไทย)

หลังรันเสร็จจะได้:

| ไฟล์ | คืออะไร |
| --- | --- |
| `<โฟลเดอร์แอป>\files\hermes_report_TH.txt` | สรุปภาษาไทย (เปิดให้อัตโนมัติเมื่อใส่ `-Open`) |
| `<โฟลเดอร์แอป>\files\hermes_report.json` | หลักฐานแบบเครื่องอ่าน (path, key, ผลทดสอบ, verdict) |
| `<โฟลเดอร์แอป>\files\hermes_stdout.log`, `hermes_stderr.log` | ข้อความที่โปรเซสใหม่พิมพ์ออกมา (กรณีแอปไม่เขียน log เอง) |
| `<โฟลเดอร์แอป>\hermes.launch.txt` | คำสั่งรันจริงของโปรเซสเดิม (ใช้รีสตาร์ทครั้งต่อ ๆ ไป) |
| `<โฟลเดอร์แอป>\kick_pusher.json` | รายการ key (primary + fallback) ที่ให้โค้ดอ่านได้ |

> **ไม่แน่ใจ?** รัน `hermes.cmd fix -DryRun` หรือ `hermes.ps1 fix -DryRun` ก่อน — จะโชว์ทุกอย่างที่จะเปลี่ยนโดยไม่เขียนไฟล์

---

### ถ้าคัดลอกจากแชทแล้ว URL ติดอักขระ markdown ( `[` `]` `(` `)` )

แชทบางตัวจะห่อ URL เป็นลิงก์ ถ้าวางทั้งบล็อกลง PowerShell ตัว `$u` จะมี `[]()` ติดมาด้วย
ให้ใช้ท่อนนี้แทน (มันตัดอักขระส่วนเกินออก และตรวจไฟล์ที่โหลดได้ก่อนใช้งานจริง):

```powershell
$u  = 'https://github.com/End3lackStaR2/End3lackStaR2/raw/9d3506fbb146c8f61e1f5bfaa3289de4573d30f6/hermes'.Trim()
$u  = $u -replace '^\[|\]$', '' -replace '\]\(.*\)$', ''      # กัน markdown link ติดมา
$d  = "$env:USERPROFILE\hermes-kick"; New-Item -ItemType Directory -Force -Path $d | Out-Null
iwr -UseBasicParsing "$u/hermes.ps1" -OutFile "$d\hermes.ps1"
$raw = Get-Content "$d\hermes.ps1" -Raw
if ($raw.Length -lt 50000 -or $raw -notmatch 'hermes\.ps1 v' -or $raw -notmatch 'VERDICT') {
    Write-Host 'ดาวน์โหลดไม่ครบ/ผิดไฟล์ - ลองใหม่จาก zip' -ForegroundColor Yellow
    $z = "$env:TEMP\End3lackStaR2.zip"
    iwr -UseBasicParsing 'https://codeload.github.com/End3lackStaR2/End3lackStaR2/zip/9d3506fbb146c8f61e1f5bfaa3289de4573d30f6' -OutFile $z
    Expand-Archive $z -DestinationPath "$env:TEMP\ebs" -Force
    Copy-Item "$env:TEMP\ebs\End3lackStaR2-*\hermes\hermes.ps1" "$d\hermes.ps1" -Force
}
Write-Host ("พร้อมใช้: " + (Get-Item "$d\hermes.ps1").Length + " bytes")
```

## สะพานส่งหลักฐานกลับมา (ใช้เมื่อ agent/ใครก็ตามไม่เห็นเครื่องคุณ)

`hermes.ps1` รันบนเครื่องคุณเท่านั้น — ส่งผลลัพธ์กลับได้ 3 ทาง ไม่ต้องใช้ MCP:

| ทาง | วิธี | ข้อดี |
| --- | --- | --- |
| **แนบไฟล์ในแชท** | รัน `report-evidence.ps1` แล้วแนบไฟล์ `.txt`/`.zip` ที่ได้ | ง่ายสุด ไม่ต้องมี credential ไม่ต้องมี git |
| **push ขึ้น repo** | `report-evidence.ps1 -Repo <owner>/<repo>` (commit ไป branch `hermes-evidence`) | agent อ่านเองได้ทันทีผ่าน GitHub (ทดสอบแล้วว่าเส้นทางนี้ใช้ได้) |
| **วางข้อความ** | คัดลอกบรรทัด `RESULT_JSON {...}` ที่ `hermes.ps1` พิมพ์ | เร็วสุด แต่ได้ข้อมูลน้อยกว่า 2 ทางบน |

```powershell
# เก็บหลักฐานเป็นไฟล์เดียว (+ zip ให้แนบง่าย)
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\report-evidence.ps1" -AppDir 'D:\HermesAI\OBS' -Zip -Open

# หรือส่งขึ้น GitHub ตรง ๆ (ใช้ git credential ที่มีอยู่แล้ว ไม่ต้องวาง token ในแชท)
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\report-evidence.ps1" -AppDir 'D:\HermesAI\OBS' -Repo 'End3lackStaR2/End3lackStaR2'
```

ตัวรายงานจะปิดข้อมูลที่อาจเป็นความลับให้อัตโนมัติ (`access_token`, `Bearer …`, JWT, `ghp_…`, `xoxb-…`)
แต่ **เก็บ Pusher app key ไว้ให้อ่านได้** เพราะเป็น key สาธารณะที่มากับเว็บ Kick อยู่แล้ว (ใช้เป็นหลักฐาน)
และมี `-SelfTest` ให้พิสูจน์เรื่องการปิดข้อมูลโดยไม่เขียนไฟล์อะไรเลย

> **หมายเหตุเรื่อง MCP**: การเชื่อม connector ทำได้จากฝั่ง UI ของ Arena เท่านั้น และ slug `hermes`
> ไม่มีให้เลือก (ตรวจแล้วได้สถานะ `unsupported`) — ถ้าคุณเพิ่ม MCP server ของตัวเองใน UI สำเร็จ
> บอกชื่อ slug มา แล้วโหลด tools ของมันได้ในเทิร์นถัดไป

## ทางเลือกที่ไม่ต้องโหลดอะไรเลย

ถ้าดาวน์โหลดจาก GitHub ไม่ได้ หรือข้อความที่คัดลอกโดน markdown ทำ URL พัง ให้ใช้
**[MANUAL-FALLBACK-TH.md](MANUAL-FALLBACK-TH.md)** — 5 ท่อน copy-paste ที่ใช้แค่ PowerShell
ในเครื่อง (ทดสอบ key สด → แก้ config → หยุดเฉพาะโปรเซสที่ใช่ → ดู log เฉพาะที่ใหม่จริง → เก็บหลักฐาน)
และมี "ท่อน 0" สำหรับโหลดเครื่องมือเต็มแบบที่ markdown ทำ URL พังไม่ได้ (ต่อ URL จากชิ้นส่วน)

## โหมดต่าง ๆ

| คำสั่ง | ทำอะไร |
| --- | --- |
| `hermes.ps1 fix` (ค่าเริ่มต้น) | ตรวจ key → เขียน `kick_pusher.json` → แก้ `config.json` (สำรอง `.bak-…`) → หยุดโปรเซสเดิม → รีสตาร์ทด้วยคำสั่งเดิม → รอหลักฐานใหม่สูงสุด 45 วิ → สรุป `OK` / `KEY_REJECTED` / `NO_FRESH_EVIDENCE` |
| `hermes.ps1 verify` | ทดสอบ key สด + วิเคราะห์ log ปัจจุบัน (ไม่แก้/ไม่รีสตาร์ท) |
| `hermes.ps1 doctor` | ตรวจอย่างเดียว: path, python, config, log, โปรเซส, หลักฐาน, นาฬิกาใน log (UTC/ท้องถิ่น) |
| `hermes.ps1 logs -TailLines 40` | แสดง path log ที่ใช้จริง + 20 บรรทัดท้าย + บรรทัด "Get-Content … -Wait" ที่ถูกต้อง (เลิกใช้ path ที่ผิด) |
| `hermes.ps1 report -Open` | เขียนรายงานใหม่จาก log ปัจจุบัน |
| `hermes.ps1 watch` | เฝ้า log ทุก 20 วิ ถ้าเจอ key ถูกปฏิเสธอีก → สลับ key + รีสตาร์ทให้อัตโนมัติ (สูงสุด 3 ครั้ง) |
| `hermes.ps1 -SelfTest` | ทดสอบตัวสคริปต์เอง (ตัดคำสั่ง/วิเคราะห์ log/เขียน json/regex config) |
| `hermes.ps1 -SelfTest -Online` | เหมือนด้านบน + ยิงทดสอบ key ทั้ง 2 ตัวกับ Pusher จริง |

พารามิเตอร์ที่ใช้บ่อย:

```powershell
-InstallDir 'D:\HermesAI\OBS'          # โฟลเดอร์แอป (ค่าเริ่มต้น)
-LogPath   'D:\HermesAI\OBS\files\kick_notifier.log'
-ConfigPath 'D:\HermesAI\OBS\config.json'
-PythonPath 'D:\HermesAI\.venv\Scripts\pythonw.exe'
-LaunchCommand '"D:\HermesAI\.venv\Scripts\pythonw.exe" "D:\HermesAI\OBS\notifier.py"'   # ถ้าตรวจไม่พบคำสั่งรันเอง
-WaitSeconds 90     # รอนานขึ้นถ้าแอปต่อช้า
-DryRun -NoRestart -NoConfigPatch  -Thai -Open
```

Exit code: `0` = OK, `1` = มีคำเตือน/ต้องดูเอง, `2` = ไม่ผ่าน

---

## ทำให้ "ทนการ rotate ครั้งต่อไป" (แนะนำ แต่ไม่บังคับ)

`hermes.ps1` แก้แค่ข้อมูล (config + json) ซึ่งพอสำหรับตอนนี้ ถ้าอยากให้ตัวแอปเอง
สลับ key อัตโนมัติเมื่อคราวหน้า (โดยไม่ต้องแก้ config ทุกครั้ง) ให้รันตัวแพตช์:

```powershell
# ดูก่อนว่าจะทำอะไร
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\patch_kick_app.py" --root D:\HermesAI\OBS --plan

# ลงจริง (สำรอง .bak-… ทุกไฟล์, ตรวจ json/ast ก่อนเขียนทับ)
python "$d\patch_kick_app.py" --root D:\HermesAI\OBS
```

แพตช์นี้ทำ 2 อย่าง:

1. สร้าง `backend/config/pusher_config.py` ที่อ่านรายการ key จาก `kick_pusher.json`
   (แก้ key ครั้งต่อไป = แก้ json แล้วรีสตาร์ท ไม่ต้องแตะโค้ด)
2. แพตช์ `backend/providers/chat/kick_ws_provider.py` ให้
   - รับได้ทั้ง key เดี่ยว และ list ของ key (**ไม่ต้องแก้ call site ใด ๆ**)
   - ลงทะเบียน handler `"pusher:error"` และเพิ่ม `_rotate_key_if_rejected()`
   - เมื่อโดน `4001 / 4003 / 4009` (หรือ close code เดียวกัน) → สลับไป key ถัดไปอัตโนมัติ
   - `stop_socket()` รีเซ็ตกลับไปใช้ key หลัก

ตรวจว่าแพตช์ทำงานจริง (มี unit test ที่ "import โค้ดที่แพตช์แล้ว" และยิง 4001 ดูว่า key หมุนจริง):

```powershell
cd "$d\.." ; python -m unittest discover -s hermes\tools -p "test_*.py"
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\tools\kick_pusher_probe.py" --selftest
```

---

## เครื่องมือเสริม (ใช้แยกก็ได้)

| ไฟล์ | ใช้ทำอะไร | ตัวอย่าง |
| --- | --- | --- |
| `tools/kick_pusher_probe.py` | ทดสอบ key กับ Pusher แบบไม่ต้องติดตั้งอะไร (stdlib ล้วน, มี WebSocket client ของตัวเอง) | `python kick_pusher_probe.py --channel <chatroom_id> --json` |
| `tools/patch_kick_app.py` | แพตช์ข้อมูล+โค้ด (`--plan`, `--dry-run`, `--revert`) | `python patch_kick_app.py --root D:\HermesAI\OBS --plan` |
| `tools/test_patch_kick_app.py` | ชุดทดสอบของตัวแพตช์ (10 เทสต์) | `python -m unittest test_patch_kick_app` |
| `selfcheck.cmd` | รัน self-test ทั้งหมดในคลิกเดียว | ดับเบิลคลิก |
| `report-evidence.ps1` | รวมหลักฐานทั้งหมดเป็นไฟล์เดียว (ปิดความลับอัตโนมัติ) + `-Zip` + `-Repo` ส่งขึ้น GitHub + `-SelfTest` | `powershell -File report-evidence.ps1 -AppDir 'D:\HermesAI\OBS' -Zip -Open` |

---

## แก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ/วิธีแก้ |
| --- | --- |
| `[WARN] log file guessed` | ยังไม่เจอ log ที่คาด → ระบุเองด้วย `-LogPath` (ตรวจ path ที่ถูกด้วย `hermes.ps1 logs`) |
| `[WARN] no running notifier python process detected` | แอปไม่ได้รันอยู่ หรือรันเป็น `.exe` แพ็กเกจ → ระบุคำสั่งรันด้วย `-LaunchCommand` |
| `NO_FRESH_EVIDENCE` | แอปอาจไม่เขียน log เอง → ดู `files\hermes_stdout.log` / `hermes_stderr.log` ที่สคริปต์เก็บให้ หรือรันแอปในหน้าต่าง console ดูตรง ๆ |
| ตัวเลขเวลาใน log ไม่ตรงกับนาฬิกาเครื่อง | log บางตัวเขียนเป็น UTC — สคริปต์จะบอก "log clock looks like UTC time" ในโหมด `doctor` และไม่ใช้เวลาใน log ตัดสิน (นับจากไบต์ที่เพิ่มหลังรีสตาร์ทเท่านั้น) |
| `KEY_REJECTED` อีกครั้งด้วย key ใหม่ | Kick อาจหมุน key อีก → เช็ค https://x.com/notjoker แล้วอัปเดต `kick_pusher.json` (หรือรอ `watch`) |
| สิทธิ์/execution policy | ใช้ `powershell -NoProfile -ExecutionPolicy Bypass -File …` ตามตัวอย่าง (ไม่ต้องเปลี่ยนนโยบายทั้งเครื่อง) |
| ไม่มี python ในเครื่อง | `hermes.ps1` ใช้ .NET เองทั้งหมด จึงทำงานได้โดยไม่ต้องมี python (แพตช์โค้ดต้องมี python) |

## ความปลอดภัย

- ไม่ส่งข้อมูลออกไปที่อื่นเลยนอกจากต่อ Pusher (`ws-us2.pusher.com`) เพื่อทดสอบ key และสร้าง log/report ในเครื่อง
- ทุกไฟล์ที่แก้จะถูกคัดลอกเป็น `<ชื่อไฟล์>.bak-<วันที่-เวลา>` ก่อนเขียนเสมอ
- ไม่แตะ token/ไฟล์ credential ใด ๆ และไม่เขียน key ลง log ของตัวเอง
- `-DryRun` ทำให้ดูทุกอย่างก่อนลงมือ

## ที่มา/หลักฐาน

- ประกาศ Kick (ทีมงาน): https://x.com/notjoker (8 ต.ค. 2026) — key ใหม่ `34bf7a0ff419a2a775b9`, cluster `us2`
- ตัวอย่างการแก้แบบเดียวกันที่ทดสอบแล้วในโปรเจกต์อื่น (10 ต.ค. 2026): https://github.com/Andro2k/MiniKick/pull/45
  (ใช้เป็น "แพตช์อ้างอิง" ของโหมด `--patch-code`: ยอมรับทั้ง 2 key, ลงทะเบียน `pusher:error`, หมุน key เมื่อโดน 4001/4003/4009, รีเซ็ตเมื่อ stop)
