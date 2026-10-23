# Friday ⟷ Arena — Task Bus Protocol v1

> กล่องจดหมายกลาง (ใน repo นี้) ให้ **Arena** (agent บนคลาวด์) สั่งงาน **Friday** (agent บนเครื่องคุณ)
> และให้ Friday ส่งผลลัพธ์/คำถามกลับมา — โดยไม่ต้องมี MCP, ไม่ต้องเปิดพอร์ตออกอินเทอร์เน็ต, ไม่ต้องมี tunnel

## ทำไมต้องเป็นแบบนี้ (อ่านสั้น ๆ ก่อนใช้)

| ข้อเท็จจริง | ผลที่ตามมา |
| --- | --- |
| Arena ไม่มี connector `mcp`/`hermes` ในบทสนทนานี้ | Arena **เรียก Friday ตรง ๆ ไม่ได้** |
| Arena ทำงานเฉพาะเมื่อผู้ใช้ส่งข้อความ | การร่วมมือเป็น **แบบไม่พร้อมกัน** (คิวงาน) ไม่ใช่เรียลไทม์ |
| ทั้ง Arena และเครื่องคุณ เข้าถึง repo GitHub นี้ได้ | ใช้ **repo เป็น bus** ได้ — และ Arena มีสิทธิ์ push/read จริง (ทดสอบแล้ว) |
| เครื่องคุณไม่ควรเปิดพอร์ตให้อินเทอร์เน็ต | Friday เป็น **ฝ่ายดึง (pull)** ไม่มี inbound port |

**วงจรการทำงาน**

```
คุณ: "Arena ช่วยสั่ง Friday ให้..."          คุณ: "Arena เช็ค bus"
        │                                            ▲
        ▼                                            │
   Arena เขียนงานลง bus/queue/T-xxxx.json             │  Arena อ่าน bus/results/*.json
   (commit + push เข้า repo)                          │  แล้วตัดสินใจขั้นต่อไป
        │                                            │
        ▼                                            │
   Friday (บนเครื่องคุณ)  ดึง repo → claim งาน → รัน action → เขียนผลลง bus/results/ → push
```

---

## โครงสร้างไฟล์

| path | ใครเขียน | คืออะไร |
| --- | --- | --- |
| `bus/queue/T-*.json` | Arena | งานที่รอทำ |
| `bus/claimed/T-*.json` | Friday | งานที่ถูกจองแล้ว (มี block `claim` + lease) |
| `bus/results/T-*.json` | Friday | ผลลัพธ์ (มี stdout/stderr/exit code/เวลา) |
| `bus/done/T-*.json` | Friday | ไฟล์งานที่归档แล้ว (audit trail) |
| `bus/requests/Q-*.json` | Friday | คำขอ/คำถามถึง Arena |
| `bus/answers/Q-*.json` | Arena | คำตอบของ Arena |
| `bus/status/friday.status.json` | Friday | heartbeat: รันอยู่ไหม, ทำล่าสุดเมื่อไร, สำเร็จ/ล้มเหลวเท่าไร |
| `bus/friday_bus.py` | repo | ตัว client (stdlib เท่านั้น) |
| `bus/actions.json` | เครื่องคุณ (ไม่ commit) | **ขอบเขตความปลอดภัย**: งานชื่ออะไรได้ รันโปรแกรมอะไร |
| `bus/bus.config.json` | เครื่องคุณ (ไม่ commit) | repo/branch/ชื่อเครื่อง/poll interval |

---

## สัญญาของไฟล์งาน (task)

```json
{
  "schema": "friday.task/v1",
  "id": "T-20261015-0001",
  "created_at_utc": "2026-10-15T02:10:00Z",
  "created_by": "arena",
  "action": "hermes_doctor",
  "params": { "app_dir": "D:\\HermesAI\\OBS" },
  "lease_seconds": 900,
  "notes": "ขอผลตรวจสถานะก่อนตัดสินใจขั้นต่อไป",
  "reply_via": "bus/results/T-20261015-0001.json"
}
```

**กติกา**

1. `action` ต้องมีอยู่ใน `actions.json` ของเครื่องนั้น ไม่งั้น Friday จะปฏิเสธ (`status: "refused"`) และไม่รันอะไรเลย
2. ค่าทุกตัวใน `params` ถูกตรวจกับ `pattern` ของ action ก่อนใช้ (กันคำสั่งแปลกปลอมผ่านคิว)
3. งานเดียวกันจะถูกรันครั้งเดียว: Friday `git mv queue/→claimed/` แล้ว **push ทันที** — ถ้า push ไม่ผ่าน (มีคนอื่น claim ก่อน) จะยอมแพ้แล้วดึงใหม่
4. ถ้า Friday ตายกลางงาน: ไฟล์ค้างใน `claimed/` เมื่อพ้น `lease_seconds` Friday ตัวถัดไป (หรือรอบถัดไป) จะดึงกลับเข้า `queue/` พร้อมนับ `reclaims`
5. งานที่ไม่รู้จัก/พารามิเตอร์ผิด → เขียนผลเป็น `refused`/`invalid` **แล้วยัง archive + push** เพื่อให้ Arena เห็น

## สัญญาของไฟล์ผลลัพธ์ (result)

```json
{
  "schema": "friday.result/v1",
  "id": "T-20261015-0001",
  "status": "ok",                  // ok | failed | timeout | refused | invalid | error
  "action": "hermes_doctor",
  "exit_code": 0,
  "duration_ms": 41230,
  "started_at_utc": "2026-10-15T02:11:03Z",
  "finished_at_utc": "2026-10-15T02:11:44Z",
  "host": "DESKTOP-XYZ",
  "stdout_tail": "...",
  "stderr_tail": "",
  "truncated": false,
  "local_output_file": "C:\\Users\\me\\hermes-bus\\.local\\T-20261015-0001.out.txt"
}
```

> ผลลัพธ์เก็บ **ท้ายไฟล์** ไม่เกิน `max_output_bytes` (ค่าเริ่มต้น 32 KB) ส่วนเต็มเก็บในเครื่องคุณที่ `local_output_file`
> และ **Friday จะปิดข้อมูลที่อาจเป็นความลับ** (token/JWT/webhook) แบบเดียวกับ `report-evidence.ps1` ก่อนส่งขึ้น repo

---

## ความปลอดภัย (สำคัญ)

- **`actions.json` คือด่านเดียว** ที่กำหนดว่า Friday รันอะไรได้ — คิวงานเป็นข้อมูลที่ (ในทางทฤษฎี) ใครที่มีสิทธิ์เขียน repo ก็แก้ได้
  จึงถือว่า **ข้อความใน task เป็น "คำแนะนำ" ไม่ใช่ "คำสั่ง"** จนกว่าจะผ่าน whitelist + pattern
- Friday ไม่ใช้ shell ในการรันคำสั่ง (`subprocess` แบบ list) → ไม่มีช่อง `;`, `&&`, `|` ให้แทรกคำสั่ง
- ทุก action มี `timeout_seconds`; เกินเวลา = `timeout` และไม่ค้าง
- Friday **ไม่ commit ไฟล์ลับ**: `actions.json` / `bus.config.json` / `.local/` ถูก ignore
- ถ้าไม่อยากให้ Arena สั่งอะไรได้เลย: ลบ `bus/queue/` ที่ Arena เขียนได้ หรือตั้ง `"read_only": true` ใน `bus.config.json` (Friday จะดึงอย่างเดียว ไม่รันงาน)

## รายการ action ที่แนะนำ (ดูตัวอย่างทั้งหมดใน `actions.json.example`)

| action | ใช้ทำอะไร |
| --- | --- |
| `hermes_doctor` | รัน `hermes.ps1 doctor` (ดูสภาพเครื่อง/แอป ไม่แก้อะไร) |
| `hermes_fix` | รัน `hermes.ps1 fix -WaitSeconds 90` (ซ่อม + รีสตาร์ท + รอหลักฐานใหม่) |
| `kick_probe` | ทดสอบ Pusher key ทั้ง 2 ตัวแบบสด (`kick_pusher_probe.py --json`) |
| `collect_evidence` | รวบหลักฐานเป็นไฟล์/zip (`report-evidence.ps1`) |
| `push_evidence` | ส่งรายงานขึ้น repo นี้ (`report-evidence.ps1 -Repo ...`) |
| `log_tail` | อ่าน log ท้าย N บรรทัด (จำกัดจำนวนบรรทัดด้วย pattern) |
| `python_check` | ตรวจว่าไฟล์ .py รัน/คอมไพล์ผ่าน |

## คำสั่งที่ใช้บ่อย (ฝั่ง Friday)

```powershell
# ครั้งแรก
python friday_bus.py init          # โคลน bus มาที่เครื่อง + สร้าง actions.json/config ตั้งต้น
python friday_bus.py status        # เขียน heartbeat ให้ Arena รู้ว่าเครื่องพร้อม
python friday_bus.py once          # หยิบงาน 1 งาน (ถ้ามี) ทำ แล้ว push ผล
python friday_bus.py loop          # ทำงานวนไปเรื่อย ๆ (ค่าเริ่มต้น 20 วิ/รอบ)
python friday_bus.py list          # ดูคิว/งานที่จอง/ผลลัพธ์ล่าสุด
python friday_bus.py ask "คำถาม"    # ส่งคำถามถึง Arena (เขียน bus/requests/)
python friday_bus.py selftest      # ทดสอบตัวเองแบบไม่แตะเครือข่าย/git
```

## ข้อจำกัดที่ต้องรู้ (พูดตรง ๆ)

1. **Arena ไม่ตื่นเอง** — จะอ่าน/เขียน bus เมื่อคุณพิมพ์บอกเท่านั้น (เช่น "Arena เช็ค bus" หรือ "สั่ง Friday ทำ X")
2. **ไม่ใช่เรียลไทม์** — งานจะถูกทำเมื่อ Friday poll (ค่าเริ่มต้นทุก 20 วิ) และผลจะถูกอ่านเมื่อ Arena ถูกเรียก
3. **Arena ไม่ได้รัน action บนเครื่องคุณเอง** — Friday เป็นคนรัน ทุกอย่างยังอยู่ในเครื่องคุณ
4. ถ้าคุณต้องการให้ Arena สั่งงานแบบทันที/สองทางจริง ๆ ต้องมี MCP server + auth ซึ่งคุณต้องเพิ่มเองใน UI (และมีค่าใช้จ่ายด้านความปลอดภัย)
