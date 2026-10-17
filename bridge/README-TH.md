# Friday ⇄ Arena ผ่านเบราว์เซอร์ (bridge + extension)

## ตรง ๆ ก่อน: นี่คืออะไร และไม่ใช่อะไร

| คำถาม | คำตอบ |
| --- | --- |
| Arena มี API ให้ Friday เรียกไหม | **ไม่มี** — Arena ตอบเมื่อมีข้อความจากคนเท่านั้น |
| แล้วแบบนี้คืออะไร | **Friday พิมพ์แทนคุณ** ลงช่องแชทของแท็บ Arena ที่คุณเปิดไว้ แล้วอ่านคำตอบกลับมา |
| ต่างจากที่ผมบอกก่อนหน้าตรงไหน | ก่อนหน้าผมบอก "เรียกเองไม่ได้" ซึ่งหมายถึง *API* — ผ่าน UI automation ทำได้ และนี่คือของจริงที่รันได้ |
| ต้องเปิดเบราว์เซอร์ทิ้งไว้ไหม | **ต้อง** — ปิดแท็บ/ออกจากระบบ = bridge ตอบไม่ได้ |
| ปลอดภัยไหม | พอสมควร *ถ้า* จำกัดวงตามที่ออกแบบไว้ แต่มีข้อควรรู้ด้านล่าง |

**ข้อควรรู้ที่ผมต้องบอกตรง ๆ:**

1. **มันพิมพ์ในนามของคุณ** — ข้อความที่ Friday ถามจะไปอยู่ในประวัติแชทของคุณ และ Arena จะเห็นว่าเป็นข้อความจากผู้ใช้
2. **UI เปลี่ยน = พัง** — ผมเลี่ยง selector ตายตัว (หาช่องพิมพ์เอง + ใช้ marker) แต่ถ้า Arena เปลี่ยนโครงสร้างหนัก ๆ ก็ต้องแก้ `content.js`
3. **ข้อกำหนดการใช้งาน** — การ automate หน้าเว็บด้วยส่วนขยายอาจขัด ToS ของผู้ให้บริการ bạnใช้ ขึ้นกับข้อความจริงของเขา (ผมยืนยันแทนคุณไม่ได้) ถ้ากังวล ให้ใช้แบบ manual หรือถามผู้ให้บริการก่อน
4. **ความเสี่ยงที่แท้จริงคือ prompt injection** — ถ้าหน้าเว็บหรือปลั๊กอินอื่นในเบราว์เซอร์แอบยิงเข้าพอร์ต 8787 ได้ มันจะพิมพ์ข้อความในนามคุณ ผมจึงบังคับ token ทุก request + ผูกกับ 127.0.0.1 เท่านั้น
5. **ช้าและไม่แน่นอน** — ต้องรอข้อความนิ่ง 3 วินาที, 1 คำถามต่อครั้ง, เว้นขั้นต่ำ 15 วินาที

**ทางเลือกที่ผมแนะนำกว่า ถ้ามี API key:** เรียก LLM API ตรง ๆ (เสถียรกว่า ไม่ต้องเปิดเบราว์เซอร์ ไม่ผิด ToS) — bridge นี้เหมาะเมื่อคุณ *ต้องการคำตอบจาก Arena โดยเฉพาะ* เช่นให้ Arena เห็น context ของโปรเจกต์นี้

---

## ติดตั้ง (5 นาที)

**1) เริ่ม bridge บนเครื่องคุณ**

```powershell
$d = Join-Path $env:USERPROFILE 'hermes-bus'
$b = Join-Path $d 'bridge'
python (Join-Path $b 'arena_bridge.py') serve
```

ครั้งแรกมันจะสร้าง token ที่ `bus\.local\bridge.token` — เปิดไฟล์นั้นคัดลอกค่าไว้

**2) โหลดส่วนขยาย**

1. เปิด `chrome://extensions` (หรือ `edge://extensions`)
2. เปิด **Developer mode**
3. **Load unpacked** → เลือกโฟลเดอร์ `bridge\extension`

**3) ผูกกับแท็บ Arena**

1. เปิดแท็บ Arena ที่ล็อกอินแล้ว (แชทที่คุณต้องการให้ Friday คุยด้วย)
2. คลิกไอคอน **Friday bridge** → วาง token → **Save** → **Test** (ต้องขึ้น `bridge ตอบ OK`)
3. กด **Attach to this tab**

แค่นั้น — ตอนนี้ Friday ถามได้แล้ว ป้าย `[friday:xxxxxx]` จะต่อท้ายทุกคำถาม เพื่อให้คุณเห็นชัดว่าอันไหนมาจาก Friday

---

## ใช้งาน

**ถามตรง ๆ จาก command line:**

```powershell
$d = Join-Path $env:USERPROFILE 'hermes-bus'
$b = Join-Path $d 'bridge'
python (Join-Path $b 'arena_bridge.py') ask "notifier ยังไม่ขึ้นข้อความ ควรเช็คอะไรต่อ"
```

**ให้ autopilot ถามเองเมื่อจนมุม** (นี่คือของจริงที่ต่อกับงาน Kick):

```powershell
$bus = Join-Path $d 'bus'
$pilot = Get-ChildItem -LiteralPath $bus -Filter 'autopilot*.py' | Select-Object -First 1
python $pilot.FullName --dir $d run --workflow kick_repair --arena-wait 300
```

เมื่อ step ล้มจนต้องตัดสินใจ autopilot จะ

1. เขียน `bus/requests/Q-*.json` (เหมือนเดิม — มีไว้เป็นหลักฐาน)
2. **ถาม Arena สดผ่านแท็บ** แล้วรอไม่เกิน 300 วินาที
3. ได้คำตอบ → เขียน `bus/answers/Q-*.json` + ใส่ใน report (`arena_answer`) + push
4. ไม่ได้คำตอบ → เดินต่อได้ทันที ไม่ค้าง (ค่าเริ่มต้นคือไม่ถามเลย)

**ดูสถานะ:**

```powershell
python (Join-Path $b 'arena_bridge.py') status
```

```
{ "ok": true, "attached": true, "last_seen_seconds": 1.2,
  "pending": 0, "in_flight": null, "asked": 3, "answered": 3, "expired": 0 }
```

---

## ความปลอดภัยที่บังคับใช้ในโค้ด (ไม่ใช่คำแนะนำ)

| กลไก | รายละเอียด |
| --- | --- |
| bind | `127.0.0.1` เท่านั้น — เครื่องอื่นในวงแลนเข้าไม่ได้ |
| auth | ทุก request ต้องมี `X-Bridge-Token` เทียบแบบ constant-time |
| attach | ต้องกดใน popup เอง (permission `activeTab`) — ไม่มีสคริปต์ไหนแนบตัวเองได้ |
| visible only | ถ้าแท็บถูกซ่อน/สลับไปแท็บอื่น จะไม่พิมพ์เลย |
| 1 in flight | คำถามที่สองระหว่างรอ → `busy` (ไม่ทับกัน) |
| rate limit | เว้นขั้นต่ำ 15 วินาที/คำถาม |
| timeout | ทุกคำถามมีเพดานเวลา + เก็บกวาดคิวเอง |
| audit | `bus/.local/bridge.jsonl` — เก็บขนาด/เวลา **ไม่เก็บเนื้อข้อความ** |
| kill | ปิด popup / ปิดแท็บ / `Ctrl+C` ที่ bridge = หยุดทันที |

---

## แก้ปัญหา

| อาการ | สาเหตุ/วิธีแก้ |
| --- | --- |
| `bridge rejected the token` | token ใน popup ไม่ตรงกับ `bus\.local\bridge.token` |
| `attached: false` ตลอด | ยังไม่ได้กด Attach หรือแท็บถูก reload (ต้อง Attach ใหม่) |
| `no message box found` | หน้านั้นไม่ใช่หน้าแชท หรือยังโหลดไม่เสร็จ |
| `typed the question but no reply appeared` | ปุ่มส่งไม่ทำงาน — เปิด Console ดู `[friday-bridge]` แล้วส่ง log มา |
| คำตอบขาดกลาง | ข้อความยัง stream อยู่แต่เงียบเกิน 3 วิ → เพิ่ม `SETTLE_MS` ใน `content.js` |
| ข้อความไปโผล่ผิดที่ | มีช่องพิมพ์หลายอัน — bridge เลือกอันล่างสุดที่ใกล้ขอบจอ ถ้าผิดให้แจ้ง ผมจะเพิ่ม `data-*` hint |

**ทดสอบโดยไม่ใช้เบราว์เซอร์:**

```powershell
python (Join-Path $b 'arena_bridge.py') selftest   # 12 checks
cd (Join-Path $b 'tests'); python -m unittest -v test_bridge_e2e   # 8 tests
```

---

## สถาปัตยกรรม (สำหรับ debug)

```
Friday script
   │  POST /ask {text, wait}          ← ปิดเมื่อได้คำตอบ หรือครบ wait
   ▼
arena_bridge.py  (127.0.0.1:8787, token ทุก request)
   │  ◄── POST /next     (ทุก 1.5 วิ, จาก background.js)
   │  ──► {id, text}
   │
   │  background.js ──► content.js (ฉีดเข้าแท็บที่คุณกด Attach)
   │                        │ พิมพ์ข้อความ + [friday:xxxxxx]
   │                        │ เฝ้า innerText จนนิ่ง 3 วิ
   │                        ▼
   │                     POST /answer {id, text}
   ▼
bus/answers/Q-*.json  +  report.arena_answer  (push ขึ้น repo)
```

ไฟล์:
- `arena_bridge.py` — เซิร์ฟเวอร์ + client (`serve|ask|status|selftest`)
- `extension/manifest.json` — permission น้อยที่สุด (`activeTab`, `scripting`, `storage`)
- `extension/background.js` — คุยกับ bridge เท่านั้น (เลี่ยง CORS ของหน้าเว็บ)
- `extension/content.js` — พิมพ์/อ่านในหน้าแชท (ไม่มี selector ตายตัว)
- `extension/popup.html|js` — ใส่ token, ทดสอบ, Attach
