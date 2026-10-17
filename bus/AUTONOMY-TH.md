# ให้ Friday ทำงานเองโดยไม่ต้องรอคุณตัดสินใจ — ทำได้แค่ไหน และทำอย่างไร

## ตอบตรง ๆ ก่อน (สำคัญที่สุด)

| สิ่งที่อยากได้ | ทำได้จริงไหม | เหตุผล |
| --- | --- | --- |
| Friday **เรียก Arena เอง** ทุกครั้งที่ต้องการ | ❌ **ไม่ได้** | Arena ไม่มี API/MCP ให้เครื่องเรียก และ Arena "ตื่น" เมื่อมนุษย์พิมพ์เท่านั้น |
| Friday **ตัดสินใจเอง** ว่า下一步 ทำอะไร / สำเร็จหรือยัง | ✅ **ได้** | ย้ายการตัดสินใจไปเป็น `policy.json` ที่คุณอนุมัติครั้งเดียว |
| Friday **ประเมินผลงานจริง** (ไม่ใช่แค่ exit code) | ✅ **ได้** | `accept` criteria: exit code, regex ต้องมี/ต้องไม่มี, เวลาสูงสุด |
| Friday **ลองใหม่/ยกระดับปัญหาเอง** | ✅ **ได้** | `retries`, `on_fail: retry / goto / escalate / abort` |
| Friday **สั่งปรับปรุงตัวเอง** | ⚠️ **ได้แบบเสนอ** | เขียนข้อเสนอลง `bus/suggestions/` — **ไม่แก้ policy เอง** (กันการแก้เกณฑ์ให้ผ่านง่าย ๆ) |
| Friday **รายงานความสำเร็จ/ล้มเหลวให้ Arena อ่าน** | ✅ **ได้** | `bus/reports/` + `bus/requests/` (คำถาม/escalation) |

**สรุป:** สิ่งที่ autonomy ทำได้คือ *"Friday ตัดสินใจแทนคุณตามกติกาที่คุณเขียนไว้"* — ส่วน Arena ยังเป็น **ผู้ตรวจทาน/ที่ปรึกษา** ที่อ่านรายงานเมื่อคุณเรียก ไม่ใช่ผู้สั่งการแบบเรียลไทม์

---

## วงจรการทำงาน

```
policy.json  ← คุณเขียน/อนุมัติครั้งเดียว (กติกาการตัดสินใจ)
     │
     ▼
autopilot.py run --workflow kick_repair
     │
     ├─ รัน action (เฉพาะที่อยู่ใน actions.json = ด่านที่ 2)
     ├─ ประเมินผลกับ accept criteria → ผ่าน/ไม่ผ่าน + เหตุผล
     ├─ ผ่าน   → ไป step ถัดไป (on_pass)
     ├─ ไม่ผ่าน → retry / goto / escalate / abort (on_fail)
     └─ จบ → เขียน bus/reports/<run_id>.json + push
              ├─ achieved        → ล้าง state, จบ
              ├─ needs_review    → เขียน bus/requests/Q-… ถาม Arena
              ├─ awaiting_approval → รอ `approve` ครั้งเดียว แล้ว `--resume`
              └─ aborted         → แตะเพดาน/ kill switch
```

## คำสั่งที่ใช้จริง

```powershell
$d = Join-Path $env:USERPROFILE 'hermes-bus'
$bus = Join-Path $d 'bus'
$pilot = Get-ChildItem -LiteralPath $bus -Filter 'autopilot*.py' | Select-Object -First 1
$fb    = Get-ChildItem -LiteralPath $bus -Filter 'friday_*.py' | Select-Object -First 1

# ครั้งแรก: คัดลอก policy ตัวอย่างแล้วแก้ path ให้ตรงเครื่อง
Copy-Item -LiteralPath (Join-Path $bus 'policy.json.example') -Destination (Join-Path $bus 'policy.json')

# ดูว่าจะทำอะไร (ไม่รันจริง ไม่ push)
python $pilot.FullName --dir $d run --workflow kick_repair --dry-run

# อนุมัติ action ที่ถูก gate ไว้ (ครั้งเดียวต่อ run) แล้วทำต่อ
python $pilot.FullName --dir $d approve --action hermes_fix
python $pilot.FullName --dir $d run --workflow kick_repair --resume

# ทำเองวนไปเรื่อย ๆ จนสำเร็จ (หรือจนแตะเพดาน)
python $pilot.FullName --dir $d loop --workflow kick_repair --interval 300

# ดูภาพรวมผลงาน (self-evaluation)
python $pilot.FullName --dir $d report
```

## สิ่งที่ปกป้องคุณ (บังคับใช้จริง ไม่ใช่คำแนะนำ)

| กลไก | ค่าเริ่มต้น | ทำอะไร |
| --- | --- | --- |
| `actions.json` | — | action ที่ไม่มีในนี้ **รันไม่ได้** (ด่านที่ 2) |
| `require_approval` | `["hermes_fix"]` | action เสี่ยงต้องอนุมัติ **ครั้งเดียว** ต่อ run |
| `max_total_actions` | 12 | กัน loop บ้า ๆ (เช่น `goto:` ที่วนไม่จบ) |
| `max_consecutive_failures` | 3 | หยุดเมื่อล้มติดกันเกินกำหนด |
| `retries` (ต่อ step) | ตามที่เขียน | ลองซ้ำได้ แต่มีเพดาน |
| kill switch | `bus/.local/STOP` | สร้างไฟล์นี้ = รอบถัดไปหยุดทันที |
| `--dry-run` | — | เดินครบทุก step แต่ไม่รัน/ไม่ push |
| audit | ทุก run | `bus/reports/<run_id>.json` (ประวัติถาวรบน repo) |
| redaction | อัตโนมัติ | token/JWT/webhook ถูกปิดก่อนขึ้น repo |

## "สั่งปรับปรุง" ทำงานอย่างไร (และทำไมไม่ให้มันแก้ตัวเอง)

เมื่อ failure **ซ้ำ** เกิน `suggestion_threshold` (ค่าเริ่มต้น 2 ครั้ง) autopilot จะเขียน
`bus/suggestions/S-<hash>.json` ที่มี:

- `signature` ของปัญหาที่ซ้ำ
- `recent_evidence` (3 ครั้งล่าสุด)
- `proposed_options` เช่น "เกณฑ์รับผลเข้มเกินจริงหรือเปล่า", "ควรเพิ่ม retry", "ถ้าเจอ `App key ... not in this cluster` แปลว่า key เก่ายังถูกอ่านจากโค้ด ไม่ใช่ config"

**autopilot ไม่แก้ `policy.json` เอง** เพราะถ้ามันแก้เกณฑ์ให้ผ่านง่าย ๆ ได้ ก็เท่ากับโกงตัวเอง
การอนุมัติ perubahan ยังเป็นของคุณ (หรือของ Arena เมื่อคุณสั่ง) — และนี่คือจุดที่ "คุณไม่ต้องเฝ้า" แต่ยังคุมอำนาจสุดท้าย

## Arena อยู่ตรงไหนในวงจรนี้

| เวลา | Arena ทำอะไร |
| --- | --- |
| ก่อนเริ่ม | ช่วยเขียน/รีวิว policy + accept criteria |
| ระหว่างรัน | **ไม่ทำอะไร** (เรียกไม่ได้) — Friday ใช้ policy ตัดสินเอง |
| หลังรัน | เมื่อคุณพิมพ์ "Arena เช็ค bus" → ผมอ่าน `reports/` + `requests/` + `suggestions/` แล้วสรุป/เสนอปรับ policy |
| เมื่อล้มซ้ำ | ผมวิเคราะห์ signature ที่ซ้ำ แล้วเสนอ patch policy ที่เจาะจง (คุณกดใช้หรือไม่ก็ได้) |

> ถ้าอนาคตคุณต้องการให้ Arena สั่งการแบบเรียลไทม์จริง ๆ ต้องมี MCP server + auth ที่คุณติดตั้งเอง
> ซึ่งแลกมาด้วยความเสี่ยง (remote execution) — ทาง policy + bus นี้คือทางที่ปลอดภัยและทำงานได้ตอนนี้

## ตัวอย่างผลลัพธ์จริงจาก autopilot

```json
{
  "schema": "friday.report/v1",
  "run_id": "R-20261017-14931",
  "workflow": "kick_repair",
  "status": "needs_review",
  "actions_used": 3,
  "steps": [
    {"step": "doctor", "passed": true,  "exit_code": 0, "duration_ms": 41230},
    {"step": "fix",    "passed": false, "exit_code": 0, "reasons": ["output does not match /VERDICT:\\s*OK/"]},
    {"step": "probe",  "passed": false, "reasons": ["output contains forbidden /\"error_code\":\\s*4001/"]}
  ],
  "escalation_request": "Q-20261017-14931"
}
```

อ่านแล้วรู้ทันทีว่า: doctor ผ่าน แต่ fix ไม่ได้ผลจริง (แม้ exit 0) และ probe ยังโดน 4001 → ไม่ใช่ปัญหา config แต่เป็น key เก่าที่ค้างในโค้ด/โปรเซส ซึ่งคือ `proposed_options` ข้อ 3
