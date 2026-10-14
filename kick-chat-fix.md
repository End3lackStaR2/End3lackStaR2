# แก้ error `App key 32cbd69e4b950bf97679 not in this cluster`

อัปเดต: 2026-10-14 · ใช้กับ `kick_chat.py` ในรีโปนี้

---

## สรุปสั้น (อ่าน 30 วินาที)

| คำถาม | คำตอบ |
| --- | --- |
| error นี้แปลว่าอะไร | **ต่อผิด cluster** — Pusher รู้จัก key นี้ แต่ key นี้ไม่ได้ถูกสร้างใน cluster ที่คุณกำลังต่อ |
| Kick ใช้ cluster ไหน | **`us2`** → `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false` |
| ลอง `ws-eu` / `ws-ap1` / `ws-us-east-1` ดีไหม | **แย่ลง** — สามตัวนี้ไม่ใช่ cluster ของ Kick จึงได้ error เดียวกันแน่นอน (`ws-us-east-1` ยังไม่ใช่ชื่อ cluster ของ Pusher ด้วยซ้ำ) |
| App key มาจาก Kick Developer Portal ไหม | **ไม่** — portal ให้แค่ OAuth `client_id` / `client_secret` ส่วน key นี้เป็น Pusher key สาธารณะที่ฝังอยู่ในหน้าเว็บ Kick (ใครก็เห็นได้ ไม่ใช่ความลับ) |
| ต้องแก้ตรงไหน | host/cluster ใน `config.json` ให้เป็น `us2` เท่านั้น (2 บรรทัด) แล้วรัน `kick_chat.py probe` เพื่อยืนยัน |
| ถ้า host เป็น `us2` แล้วยัง error | ดูข้อ **3.5** — hosts file / proxy / DNS ถูกดัก / key ถูกหมุน / process เก่ายังไม่รีสตาร์ท (ใช้ `fix-config` + `scan`) |
| ถ้าอนาคต Kick เปลี่ยน key | รัน `python kick_chat.py probe` — สคริปต์จะไล่ทดสอบทุก cluster ให้เอง (หรือวาง URL จาก DevTools ผ่าน `discover --ws-url`) |

> **สรุปสาเหตุ:** ไม่ใช่ "token/credentials ผิด" และไม่ใช่ "app key ผิด" — เป็นเพราะปลายทาง WebSocket ที่ต่ออยู่ไม่ใช่ `ws-us2.pusher.com` ครับ

---

## 0) อ่านก่อนเลย: error "not in this cluster" เกิดได้ 3 แบบ — แยกให้ออกใน 10 วินาที

Pusher จะบอก **key ที่มันเห็น** มาในข้อความ error เสมอ ให้จับ 3 อย่างนี้ในบรรทัด error:

```
Pusher : error : {"code":4001,"message":"App key <KEY-ที่-Pusher-เห็น> not in this cluster"}
                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   1) KEY ที่ Pusher เห็น   2) host ที่คุณต่อ   3) process ไหนเป็นคนต่อ (เก่าหรือใหม่?)
```

| KEY ที่ Pusher เห็น | แปลว่า | แก้ตรงไหน |
| --- | --- | --- |
| `32cbd69e4b950bf97679` (key สาธารณะ) | key ถูก — ปัญหาอยู่ที่ host/เส้นทาง/process เก่า | ดูข้อ 3.5 |
| อะไรก็ตามที่ไม่ใช่ key ข้างบน | **มีคนเอาค่าผิดใส่ช่อง `appKey`** (พบบ่อยสุด: เอา OAuth **client_id** จาก Kick Developer Portal มาใส่) | ใส่ `32cbd69e4b950bf97679` แทน — **client_id/client_secret ไม่เกี่ยวอะไรกับ Pusher เลย** |
| key ยาว 40+ ตัว / มีอักขระแปลก | คัดลอกมาครบไม่ถึง หรือมีช่องว่าง/อักขระซ่อน | ใช้ `extract` ให้แกะใหม่ |

**กับดัก 2 ข้อที่ทำให้ "แก้แล้วยังไม่หาย":**

1. **Token หมดอายุไม่เกี่ยว** — การอ่านแชทผ่าน Pusher **ไม่ใช้ token เลย** (token ใช้เฉพาะตอน *ส่ง* ข้อความผ่าน API ทางการ) → อย่าเสียเวลารีเซ็ต token เพื่อแก้ error นี้
2. **Process เก่ายังรันอยู่** — ถ้ามี `python` หลายตัว ตัวเก่าที่เริ่มก่อนแก้ config จะต่อด้วยค่าเดิมและพ่น error เดิมลง log เดิม ทำให้เข้าใจว่าแก้ไม่สำเร็จ:
   ```cmd
   tasklist | findstr /i python
   taskkill /IM python.exe /F
   :: แล้วเช็คเวลาของ log ว่าไม่มีบรรทัดใหม่จาก process เก่าโผล่มาอีก
   tasklist | findstr /i python
   :: สตาร์ทตัวใหม่ 1 ตัวเท่านั้น
   ```
   ตรวจว่า process ไหนรันด้วย argument อะไร (เห็น command line เต็ม):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,CreationDate,CommandLine | Format-List
   ```

💡 **ทางลัด:** `kick_chat.py report` (ข้อ 3.6) เก็บทั้ง 3 อย่างนี้มาให้ในบล็อกเดียว

---

## 1) หลักฐานว่ายังใช้ `us2` (ตรวจเมื่อ 14 ต.ค. 2026)

ผมไล่ดูโค้ดที่ Push ล่าสุดบน GitHub ซึ่งยังใช้ Pusher ของ Kick อยู่:

| Repo | Push ล่าสุด | ค่าที่ใช้ |
| --- | --- | --- |
| `KickTalkOrg/KickTalk` | 13 ต.ค. 2026 | `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?...` |
| `StreamNook/StreamNook` | 13 ต.ค. 2026 | cluster `us2` + `chatrooms.{id}.v2` |
| `PlayingPossumHiss/possum_chat` | 13 ต.ค. 2026 | `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?...` |
| `Andro2k/MiniKick` | 11 ต.ค. 2026 | `cluster="us2"`, keys=(`34bf7a0ff419a2a775b9`, `32cbd69e4b950bf97679`) |
| `Kounex/obs_blade` | 12 ต.ค. 2026 | Pusher service สำหรับแชท Kick |

และ changelog ของ Kick Dev Docs ไม่มีรายการเกี่ยวกับ chat/websocket ตั้งแต่ **11 ส.ค. 2026** → ยังไม่มีการย้าย cluster

**ข้อสังเกต":** บางโปรเจกต์เก็บ key เก่า `34bf7a0ff419a2a775b9` ไว้เป็นตัวสำรอง — ในสคริปต์ของคุณผมใส่ fallback นี้ไว้ให้แล้ว

---

## 2) วินิจฉัยอัตโนมัติ (คำสั่งเดียว)

```cmd
cd /d "D:\HermesAI\OBS\files"
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_chat.py probe
```

ผลลัพธ์ที่ **ถูกต้อง** จะเป็นแบบนี้:

```
=== ทดสอบว่า Kick app key อยู่ cluster ไหน ===
      app key : 32cbd69e4b..., 34bf7a0ff4...
      cluster : us2, us3, us1, eu, ap1, ap2, ap3, ap4, mt1, sa1, in1
      หมดเวลา : 12.0 วินาที/cluster

[OK]  cluster us2  key 32cbd69e4b...  ->  {"socket_id": "123.456.789", "activity_timeout": 120}

[OK]  cluster ที่ใช้งานได้ = us2 (บันทึกใน D:\HermesAI\OBS\files\kick_chat_config.json)
      ws url = wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false

เอาไปใส่ config.json ของคุณ:
{
  "pusher": {
    "cluster": "us2",
    "appKey": "32cbd69e4b950bf97679",
    "wsUrl": "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false",
    "channelTemplate": "chatrooms.{chatroom_id}.v2"
  }
}
```

ถ้าต่อผิด cluster จะเห็นบรรทัดนี้ (แปลว่า host นั้นใช้ไม่ได้):

```
[xx]  cluster eu     key 32cbd69e4b...  ->  ต่อผิด cluster: App key 32cbd69e4b950bf97679 not in this cluster
```

ตรวจสุขภาพทั้งระบบ (config + token + cluster) ในคำสั่งเดียว:

```cmd
"...\python.exe" kick_chat.py doctor
```

---

## 3) แก้ `config.json` ของคุณ

หาค่าที่เป็น host/URL ของ Pusher แล้วแก้ให้ตรงนี้ (แทนค่าอะไรก็ได้ที่เดิมชี้ไป `ws-eu` / `ws-ap1` / `ws.pusher.com` / `us-east-1`):

```json
{
  "pusher": {
    "cluster": "us2",
    "appKey": "32cbd69e4b950bf97679",
    "wsUrl": "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false"
  },
  "channelTemplate": "chatrooms.{chatroom_id}.v2"
}
```

**จุดที่พลาดบ่อย**

| อาการ | ต้นเหตุ |
| --- | --- |
| `App key ... not in this cluster` | host ไม่ใช่ `ws-us2.pusher.com` |
| ต่อได้แต่ไม่มีข้อความเข้า | ชื่อ channel ผิด — ต้องเป็น `chatrooms.{chatroom_id}.v2` (มี fallback เก่า `chatrooms.{id}` และ `chatroom_{id}`) |
| ต่อได้ ~2 นาทีแล้วหลุด | ไม่ตอบ `pusher:ping` (script นี้ตอบ `pusher:pong` และส่ง `pusher:ping` เองทุก ~30 วิ) |
| เชื่อมต่อไม่ขึ้นเลย | ไฟร์วอลล์/เน็ตบล็อก wss 443 → ทดสอบด้วย `curl -I https://ws-us2.pusher.com` |

> ถ้าคุณส่ง `config.json` มาให้ผม ผมแก้ให้ตรงจุดได้ทันที (ผมเข้าถึงไดรฟ์ `D:` ของเครื่องคุณไม่ได้)

---

## 3.5) ⚠️ host เป็น `ws-us2.pusher.com` แล้ว "ยัง" ได้ error เดียวกัน

ถ้า host ถูกและ key ถูก ตามทฤษฎีจะ **ต้อง** ต่อได้ — ถ้ายังไม่ได้ แปลว่ามีอย่างใดอย่างหนึ่งใน 6 ข้อนี้ ให้รันคำสั่งเดียวเพื่อเช็คทั้งหมด:

```cmd
cd /d "D:\HermesAI\OBS\files"
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_chat.py fix-config ^
  --config-file "D:\HermesAI\OBS\files\config.json"
```

`fix-config` จะตรวจ + แสดงผลทีละข้อ แล้วเสนอค่าที่ถูกต้อง (ยังไม่แก้ไฟล์จนกว่าจะใส่ `--apply`):

| อาการที่สคริปต์เช็คให้ | ทำไมทำให้ "ต่อผิด cluster" |
| --- | --- |
| บรรทัด `pusher` ใน `C:\Windows\System32\drivers\etc\hosts` | traffic ถูกส่งไปที่เครื่อง/IP อื่น → ไม่ได้คุยกับ cluster จริง |
| `HTTPS_PROXY` / `ALL_PROXY` ใน environment | WebSocket วิ่งผ่าน proxy ที่ไปเข้า Pusher cluster อื่น |
| DNS ของ `ws-us2.pusher.com` ชี้ IP วงใน (127./10./192.168./169.254.) | DNS ถูกแก้ (VPN/โปรแกรมดักจับ) |
| key ใน config ≠ key ที่ Kick ใช้ตอนนี้ | Kick หมุน key (เกิดเป็นครั้งคราว) |
| ค่าอื่นในไฟล์ (เช่น `host` ชนะ `wsUrl`) | โปรแกรมของคุณอาจใช้ฟิลด์คนละตัวกับที่คุณแก้ |
| แก้แล้วยังไม่ restart process | process เก่ายังถือค่าเดิม/URL เดิมอยู่ |

ตัวอย่างผลลัพธ์เมื่อพบปัญหา:

```
--- ตรวจสิ่งรอบข้าง ---
[!]   พบรายการ pusher ใน hosts file:
      C:\Windows\System32\drivers\etc\hosts: 10.0.0.9 ws-us2.pusher.com
[!]   พบ proxy env: HTTPS_PROXY=http://127.0.0.1:8888
      DNS ws-us2.pusher.com      -> 10.0.0.9  <-- ผิดปกติ!

>>> สรุป: ต่อผิด cluster (host ws-us2.pusher.com) — key ถูก แต่ host ไม่ใช่ us2
    ต้องแก้ / ต้องตรวจ:
      - มีบรรทัด pusher ใน hosts file → ต้องลบ (ทำให้ traffic วิ่งไปที่อื่น)
      - มี proxy env ตั้งอยู่ (HTTPS_PROXY) → ปิดก่อนทดสอบ
      - DNS ของ ws-us2.pusher.com ชี้ไป IP วงใน (10.0.0.9) → ถูกดักจับ/แก้ DNS
```

**วิธีแก้ hosts file (ถ้าพบ):** เปิด Notepad **as Administrator** → ไฟล์ `C:\Windows\System32\drivers\etc\hosts` → ลบบรรทัดที่มี `pusher` → `ipconfig /flushdns`

**หา "ทุกที่" ที่มีการตั้งค่า pusher ในเครื่อง** (บางโปรเจกต์มีหลายไฟล์/ค่า hardcode ทับกัน — ตัวนี้คือตัวที่ทำให้คุณแก้ config.json แล้วไม่เปลี่ยนพฤติกรรม):

```cmd
"...\python.exe" kick_chat.py scan --path "D:\HermesAI\OBS"
```

```
D:\HermesAI\OBS\config.json:5: [ws-url] wsUrl = wss://ws-eu.pusher.com/app/ffff...?protocol=7   <-- ไม่ใช่ cluster ที่ Kick ใช้!
D:\HermesAI\OBS\src\chat.py:12: [app-key] appKey = 34bf7a0ff419a2a775b9   <-- key นี้ไม่ใช่ key ล่าสุดที่ Kick ใช้?
D:\HermesAI\OBS\config.json:9: [pusher-host] host = ws-us2.pusher.com
```

**ซ่อมทั้งไฟล์ให้เลย** (สำรอง `.bak-<วันเวลา>` ให้อัตโนมัติ และคงคีย์อื่นในไฟล์ไว้ครบ):

```cmd
"...\python.exe" kick_chat.py fix-config --config-file "D:\HermesAI\OBS\files\config.json" --apply
```

---

### 3.6) ส่งรายงานให้ผมวิเคราะห์: `report` + `extract`

**คำสั่งที่ 1 — รวบรวมทุกอย่างเป็นบล็อกเดียว** (ปิดบัง secret ให้แล้ว ส่งต่อได้ปลอดภัย):

```cmd
cd /d "D:\HermesAI\OBS\files"
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_chat.py report
```

ได้ผลลัพธ์แบบนี้ (คัดลอกทั้งหมดส่งมาได้เลย):

```
=== KICK CHAT REPORT ===
--- process python ที่กำลังรัน ---
Image Name   PID Session Name  Session#  Mem Usage
python.exe  8123 Console        1    58,432 K
PowerShell: ProcessId : 8123  CreationDate : 14/10/2026 05:12:31
             CommandLine : python.exe kick_chat.py listen --chatroom 4242
--- config ---
ไฟล์ config  : D:\HermesAI\OBS\files\kick_chat_config.json (พบ)
ค่าที่อ่านได้  : {"cluster": "us2", "app_key": "0139bca5...", "ws_url": "wss://ws-us2.pusher.com/app/0139bca5...?protocol=7"}
มาจากฟิลด์    : {"cluster": "pusher.cluster", "app_key": "pusher.appKey", "ws_url": "pusher.wsUrl"}
--- token (ไม่ใช้กับ Pusher — ใช้เฉพาะตอนส่งข้อความ) ---
scope       : chat:write
อายุ         : เหลืออีก 42 นาที
--- สิ่งที่อาจดัก/แก้เส้นทาง ---
hosts file   : ไม่มีการแก้ที่เกี่ยวกับ pusher
proxy env    : ไม่มี
DNS ws-us2.pusher.com   : 3.147.174.126, 52.15.178.107, 3.12.46.165
--- ทดสอบเชื่อมต่อ Pusher จริง (ดูข้อความ error ว่ามันบอก key อะไร) ---
ตาม config (cluster=us2):
   ws-us2.pusher.com key 0139bca5... -> wrong-cluster: App key 0139bca5f0e1c1c7f4b0aa11 not in this cluster
มาตรฐาน us2 + key สาธารณะ:
   ws-us2.pusher.com key 32cbd69e... -> ok: {"socket_id": "123.456"}
เทียบ ws-eu + key สาธารณะ (control):
   ws-eu.pusher.com key 32cbd69e... -> wrong-cluster: App key 32cbd69e4b950bf97679 not in this cluster
=== จบบล็อกรายงาน ===
```

อ่านผลเองก็ได้: ถ้าบรรทัด **"มาตรฐาน us2 + key สาธารณะ" ขึ้น `ok`** แต่บรรทัดตาม config ขึ้น `wrong-cluster` → **key ใน config ของคุณผิด** (ตัวที่พบบ่อยคือเอา OAuth client_id มาใส่)

**คำสั่งที่ 2 — วางข้อความ/ล็อก/URL อะไรก็ได้ แล้วให้แกะค่าให้** (กันคัดลอกผิดช่อง):

```cmd
:: วางจากคลิปบอร์ด
"...\python.exe" kick_chat.py extract --file "%USERPROFILE%\Desktop\clip.txt"

:: หรือพิมพ์ตรง ๆ
"...\python.exe" kick_chat.py extract --text "wss://ws-us2.pusher.com/app/xxxx?protocol=7"

:: แกะ + ทดสอบ + แก้ config ให้เลยในคำสั่งเดียว
"...\python.exe" kick_chat.py extract --file clip.txt --probe ^
  --config-file "D:\HermesAI\OBS\files\config.json" --apply
```

ตัวอย่างผลลัพธ์เมื่อพบว่า key ในล็อกไม่ใช่ key จริง:

```
ค่าที่เลือกใช้:
{ "cluster": "us2", "app_key": "0139bca5f0e1c1c7f4b0aa11", ... }
[!]   key ที่พบไม่ใช่ key สาธารณะที่รู้จัก (32cbd69e4b...) — มักเกิดจากการเอา OAuth client_id มาใส่ช่อง appKey

ค่ามาตรฐานที่ Kick ใช้จริง (ถ้าใช้ค่านี้จะต่อได้แน่):
{ "cluster": "us2", "app_key": "32cbd69e4b950bf97679", ... }

--- ทดสอบเชื่อมต่อ ---
      ค่าจากข้อความ  -> wrong-cluster: App key 0139bca5... not in this cluster
      ค่ามาตรฐาน us2 -> ok: {"socket_id": "..."}
[OK]  ค่ามาตรฐานใช้ได้ (ค่าจากข้อความใช้ไม่ได้) — จะใช้ค่ามาตรฐานในการแก้ไฟล์
```

> `extract` จะสลับไปใช้ค่ามาตรฐานให้อัตโนมัติ **เฉพาะเมื่อทดสอบแล้วว่าค่ามาตรฐานต่อได้จริง** และค่าที่พบต่อไม่ได้

---

### วิธี "ผู้เชี่ยวชาญ" หา cluster จริงใน 10 วินาที (ไม่ต้องเดา)

เปิด <https://kick.com/> (หรือหน้าช่องคุณ) → กด **F12** → แท็บ **Console** → วางบรรทัดนี้:

```js
(() => {
  const ws = performance.getEntriesByType('resource')
    .map(e => e.name).filter(n => /pusher/i.test(n));
  const msg = ws.length ? ws.join('\n') : 'ไม่พบ push er ในหน่วยความจำ — รีเฟรชหน้า (F5) แล้วรันอีกครั้ง';
  copy(msg); console.log(msg); return msg;
})()
```

มันจะคัดลอก URL ที่เบราว์เซอร์ใช้จริงลงคลิปบอร์ด (เช่น `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&...`) →
ถ้ายังได้ `ไม่พบ...` ให้ใช้แท็บ **Network** แทน (กรอง `pusher` → รีเฟรช → คลิกบรรทัด type `websocket` → คัดลอก Request URL) →
เอามาวางในคำสั่งนี้ แล้วสคริปต์จะทดสอบ + แก้ config ให้ตรงกับความจริงทันที:

```cmd
"...\python.exe" kick_chat.py discover --ws-url "wss://...ที่คัดลอกมา..."
```

> **หมายเหตุ 1:** token ที่หมดอายุ **ไม่ทำให้เกิด error นี้** — Pusher ไม่ใช้ token ในการอ่านแชท (token ใช้แค่ตอน `send` ข้อความ)
>
> **หมายเหตุ 2:** ผม (ผู้ช่วย) รัน `probe` แทนคุณไม่ได้ เพราะสภาพแวดล้อมที่ผมรันอยู่ต่อ `pusher.com`/`kick.com` ไม่ได้ (ทดสอบแล้ว: DNS resolve ได้ แต่ TCP/TLS ถูกบล็อก) — คำสั่ง `fix-config`/`probe`/`scan` ถูกออกแบบมาให้รันบนเครื่องคุณแล้วรายงานผลเหมือนที่ผมจะทำ

---

## 4) ถ้า `probe` ไม่เจอ cluster ที่ใช้ได้เลย

แปลว่า Kick เปลี่ยน key ใหม่ (เกิดขึ้นได้เป็นครั้งคราว) — ใช้สคริปต์ดึงจากเว็บอัตโนมัติ หรือคัดลอกจาก DevTools:

**วิธีดูจาก DevTools (ชัวร์สุด)**
1. เปิด <https://kick.com/> (หรือหน้าช่องของคุณ) ใน Chrome
2. กด **F12** → แท็บ **Network** → กรองคำว่า `pusher`
3. กด **F5** รีเฟรช จะเห็นบรรทัด type `websocket` → คัดลอก URL ทั้งเส้น เช่น
   `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false`
4. เอามาวางในคำสั่งนี้ (สคริปต์จะแยก cluster/key แล้วทดสอบให้):

```cmd
"...\python.exe" kick_chat.py discover --ws-url "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false"
```

หรือให้สคริปต์ลองหาจากหน้าเว็บเอง:

```cmd
"...\python.exe" kick_chat.py discover --channel yourslug
```

(วิธีหลังอาจติด Cloudflare — ถ้าไม่ได้ใช้ DevTools ตามข้อ 1–4)

---

## 5) ทดสอบว่า process ทำงานจริง

หา `chatroom_id` ของช่องก่อน:

```cmd
"...\python.exe" kick_chat.py resolve --channel yourslug
```

ถ้า `kick.com` บล็อก (Cloudflare) ให้เอา id เองจากเบราว์เซอร์: เปิดหน้าเพจช่อง → F12 → Console → พิมพ์

```js
fetch('/api/v2/channels/yourslug').then(r=>r.json()).then(d=>console.log(d.chatroom.id, d.id, d.user_id))
```

แล้วสั่งฟังแชท:

```cmd
"...\python.exe" kick_chat.py listen --chatroom 123456
```

ตัวอย่างผลลัพธ์ (ข้อความจริงจะออกทาง stdout ส่วนสถานะออกทาง stderr):

```
[OK]  ฟังแชทจากช่อง chatrooms.123456.v2 (กด Ctrl+C เพื่อหยุด)
[12:34:56] (moderator) viewer1: สวัสดีครับ
[12:35:02] someone: hello
```

โหมด JSON สำหรับต่อเข้า OBS / HermesAI (1 บรรทัด = 1 ข้อความ):

```cmd
"...\python.exe" kick_chat.py listen --chatroom 123456 --json --limit 0 > chat.ndjson
```

ข้อความที่ได้จะมี `id, created_at, user_id, username, color, badges, content, type, raw` พร้อมใช้ทันที

**รันค้างไว้เป็น process (Windows)**

```cmd
:: รันเบื้องหลัง พร้อม log
start /B "" "C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" ^
  "D:\HermesAI\OBS\files\kick_chat.py" listen --chatroom 123456 --json >> "D:\HermesAI\OBS\files\kick_chat.log" 2>&1

:: เช็คว่ามี process อยู่
tasklist | findstr /i python

:: หยุด
taskkill /IM python.exe /F
```

อยากให้รันเองตลอดเวลา/รีสตาร์ทอัตโนมัติ ใช้ **Task Scheduler** (Trigger: At startup, Action: python.exe + argument `kick_chat.py listen --chatroom 123456`) หรือ **NSSM** สำหรับทำเป็น Windows service

---

## 6) ทางเลือกทางการ (ไม่พึ่ง Pusher key อีกเลย)

ถ้าไม่อยากเจอปัญหา key/cluster เปลี่ยนอีก ใช้ **Events API (webhook)** ของ Kick:

| อย่าง | Pusher WebSocket (ที่ใช้อยู่) | Events API ทางการ |
| --- | --- | --- |
| รับแชท | `chatrooms.{id}.v2` ผ่าน Pusher | webhook `chat.message.sent` |
| สโคปที่ต้องมี | ไม่ต้องใช้ token (อ่านสาธารณะ) | `events:subscribe` |
| ต้องมี public endpoint | ไม่ต้อง | **ต้องมี** HTTPS ปลายทางที่ Kick ยิงมาได้ |
| เสถียรภาพ | Kick เปลี่ยน key/cluster ได้เงียบ ๆ | เอกสารรับรอง + มี changelog |
| ข้อจำกัด | ไม่มี replay ถ้าหลุด | chat.message.sent สูงสุด 1,000 subscription สำหรับแอปที่ยังไม่ verified |

ส่งข้อความเข้าหาแชท (ฝั่งส่ง) ใช้ API ทางการอยู่แล้ว ซึ่งสคริปต์นี้รองรับ:

```cmd
:: ต้องมี token ที่มี scope chat:write (จาก kick_oauth.py login)
"...\python.exe" kick_chat.py send --channel yourslug --broadcaster-user-id 123456 --message "สวัสดีครับ"
```

ตัวอย่าง webhook ที่ Kick ยิงมา (สำหรับไปเขียนฝั่งรับ):

```json
{
  "message_id": "unique_message_id_123",
  "broadcaster": { "user_id": 123456789, "username": "broadcaster_name" },
  "sender": { "user_id": 987654321, "username": "sender_name",
              "identity": { "username_color": "#FF5733",
                            "badges": [{ "type": "moderator", "text": "Moderator" }] } },
  "content": "Hello [emote:4148074:HYPERCLAP]",
  "created_at": "2025-01-14T16:08:06Z"
}
```

---

## 7) ความหมาย error code ของ Pusher (อ่าน log ให้ออก)

| code | ความหมาย | ต้องทำอะไร |
| --- | --- | --- |
| `4001` + "not in this cluster" | ต่อผิด cluster | ใช้ `us2` (`ws-us2.pusher.com`) |
| `4001` + "Application does not exist" | key ไม่ถูกต้อง/ถูกเปลี่ยน | ไล่หาด้วย `probe` หรือ DevTools |
| `4003` | แอปถูกปิด | key เก่า — หา key ใหม่ |
| `4004` | เกินโควตาการเชื่อมต่อ | ลดจำนวน connection/chatroom |
| `4009` | client version ไม่รองรับ | อัปเดตค่า `version=` ใน URL |
| `4201` | ไม่ตอบ pong ทันเวลา | keepalive (สคริปต์จัดการให้แล้ว) |
| `4202` | ถูกปิดเพราะ idle | เหมือนกัน — ส่ง `pusher:ping` ทุก ~30 วิ |

---

## 8) ไฟล์/คำสั่งที่เกี่ยวข้อง

| คำสั่ง | ทำอะไร |
| --- | --- |
| `kick_chat.py probe` | ไล่ทดสอบทุก cluster + บันทึก cluster ที่ใช้ได้ลง config |
| `kick_chat.py fix-config --config-file "<ไฟล์>" [--apply]` | วินิจฉัย hosts/proxy/DNS/key แล้วซ่อม config ให้ (สำรอง .bak) |
| `kick_chat.py scan --path "<โฟลเดอร์>"` | หาทุกไฟล์ในเครื่องที่มีค่า pusher เพื่อจับตัวที่ override |
| `kick_chat.py report` | รวมทุกอย่าง (process/config/token/hosts/DNS/probe) เป็นบล็อกเดียวสำหรับส่งต่อ |
| `kick_chat.py extract --file/-–text "..." [--probe] [--apply]` | แกะค่า pusher จากข้อความ/URL ที่วางมา แล้วเทียบกับค่ามาตรฐาน |
| `kick_chat.py doctor` | ตรวจ config/token/cluster ทั้งหมด (ค่าเริ่มต้นเมื่อไม่ใส่คำสั่ง) |
| `kick_chat.py discover --ws-url "..."` | รับ URL จาก DevTools/Console แล้วแยก cluster/key + ทดสอบให้ |
| `kick_chat.py resolve --channel <slug>` | หา channel_id / chatroom_id / broadcaster_user_id |
| `kick_chat.py listen --chatroom <id> [--json]` | ฟังแชทสด (keepalive + reconnect อัตโนมัติ) |
| `kick_chat.py send --channel <slug> --message "..."` | ส่งข้อความผ่าน API ทางการ (ต้องมี scope `chat:write`) |
| `tests/test_kick_chat.py` | เทสต์ออฟไลน์ 13 เคส (มี Pusher server จำลองในตัว) |

อ้างอิง: [Kick Dev Docs](https://github.com/KickEngineering/KickDevDocs) · [Pusher Channels protocol](https://pusher.com/docs/channels/library_auth_reference/pusher-websockets-protocol/) · repos ที่อ้างในข้อ 1
