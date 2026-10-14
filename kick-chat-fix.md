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
| ถ้าอนาคต Kick เปลี่ยน key | รัน `python kick_chat.py probe` — สคริปต์จะไล่ทดสอบทุก cluster ให้เอง (หรือวาง URL จาก DevTools ผ่าน `discover --ws-url`) |

> **สรุปสาเหตุ:** ไม่ใช่ "token/credentials ผิด" และไม่ใช่ "app key ผิด" — เป็นเพราะปลายทาง WebSocket ที่ต่ออยู่ไม่ใช่ `ws-us2.pusher.com` ครับ

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
| `kick_chat.py doctor` | ตรวจ config/token/cluster ทั้งหมด (ค่าเริ่มต้นเมื่อไม่ใส่คำสั่ง) |
| `kick_chat.py discover --ws-url "..."` | รับ URL จาก DevTools แล้วแยก cluster/key + ทดสอบให้ |
| `kick_chat.py resolve --channel <slug>` | หา channel_id / chatroom_id / broadcaster_user_id |
| `kick_chat.py listen --chatroom <id> [--json]` | ฟังแชทสด (keepalive + reconnect อัตโนมัติ) |
| `kick_chat.py send --channel <slug> --message "..."` | ส่งข้อความผ่าน API ทางการ (ต้องมี scope `chat:write`) |
| `tests/test_kick_chat.py` | เทสต์ออฟไลน์ 13 เคส (มี Pusher server จำลองในตัว) |

อ้างอิง: [Kick Dev Docs](https://github.com/KickEngineering/KickDevDocs) · [Pusher Channels protocol](https://pusher.com/docs/channels/library_auth_reference/pusher-websockets-protocol/) · repos ที่อ้างในข้อ 1
