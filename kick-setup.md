# คู่มือตั้งค่า Kick Client ID / Secret + OAuth (Windows)

อัปเดต: 2026-10-14 · ใช้ได้กับ Python 3.10+ บน Windows (ทดสอบบน 3.11/3.12 — ไม่ต้องติดตั้งแพ็กเกจเพิ่ม)

> **TL;DR** — เปิดหน้าต่าง Terminal **ใหม่** แล้วรัน 3 คำสั่งนี้ตามลำดับ
> ```cmd
> cd /d "D:\HermesAI\OBS\files"
> "C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_oauth.py setup
> "C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_oauth.py login
> ```
> คำสั่งแรกกรอก Client ID / Secret (Secret จะไม่โชว์บนจอ) คำสั่งที่สองจะเด้งเบราว์เซอร์ให้กด Authorize แล้วเก็บ token อัตโนมัติ

---

## 0) สาเหตุที่วิธีเดิม (`kick_credentials.py setup`) ค้าง

ยืนยันแล้วว่า **ไม่มี process `kick_credentials.py setup` ที่รอรับ input ค้างอยู่** — ที่กรอกแล้ว "ไม่ไปต่อ" เกิดจากหน้าต่างที่รันสคริปต์นั้นถูกเรียกจากตัวรันแบบไม่มี terminal จริง (stdin ไม่ใช่คีย์บอร์ด) พอสคริปต์เรียก `input()` มันจึงรอตลอดกาลโดยไม่มีอะไรให้พิมพ์

ทางแก้คือรันในหน้าต่าง Terminal ที่ผูกกับคีย์บอร์ดจริง (อย่างที่คุณทำ) และถ้าจะให้ชัวร์ว่าไม่มีตัวเก่าค้างอยู่ ให้ทำตามขั้นตอนที่ 1 ก่อน

---

## 1) เช็คว่าไม่มี process ค้างอยู่

เปิด **Command Prompt ใหม่** แล้วรัน:

```cmd
tasklist | findstr /i python
```

ถ้ามีผลลัพธ์ออกมาแต่คุณไม่ได้ตั้งใจรันอะไรอยู่ ให้ปิดด้วย:

```cmd
taskkill /IM python.exe /F
taskkill /IM pythonw.exe /F
```

ใน PowerShell:

```powershell
Get-Process python, pythonw -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
# ถ้าจะฆ่าทั้งหมด
Get-Process python, pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
```

ดูแบบละเอียดว่าตัวไหนกำลังรอ input (บรรทัดที่มี `python.exe` และสถานะ `Unknown`/ไม่มีหน้าต่าง):

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine | Format-List
```

> ถ้าเห็น `kick_credentials.py setup` แล้วยังเปิดหน้าต่างที่พิมพ์ได้อยู่ ก็กรอก Client ID ต่อได้เลย — แต่ถ้าหน้าต่างนั้นไม่ตอบสนอง ให้ฆ่า process แล้วใช้วิธีในข้อ 3 (สคริปต์ใหม่จะบอกทุกอย่างบนหน้าจอ)

---

## 2) เตรียมของให้ครบ (ทำครั้งเดียว)

| ต้องมี | รายละเอียด |
| --- | --- |
| Python 3.11+ | ที่เครื่องคุณคือ `C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe` |
| บัญชี Kick + เปิด 2FA | Kick บังคับเปิด Two-Factor ก่อนเข้าหน้า Developer |
| แอปใน Developer Settings | ไปที่ <https://kick.com/settings/developer> → สร้าง App → จะได้ **Client ID** + **Client Secret** + ต้องกรอก **Redirect URL** |
| ไฟล์ `kick_oauth.py` | คัดลอกจาก repo นี้ไปวางไว้ใน `D:\HermesAI\OBS\files` (โฟลเดอร์เดียวกับ `kick_credentials.py`) |

**Redirect URL ที่ต้องตั้งในหน้า Developer** ให้ใช้ค่าเดียวกับที่สคริปต์จะใช้:

```
http://localhost:8080/callback
```

> ⚠️ ต้อง **ตรงกันทุกตัวอักษร** ทั้ง scheme, host, พอร์ต และ path ถ้าตั้งใน Kick ว่า `http://localhost:8080/callback` แต่สคริปต์ส่ง `http://127.0.0.1:8080/callback` Kick จะตอบ error `redirect_uri` mismatch
> Kick แนะนำให้ใช้ `localhost` (ไม่ใช่ `127.0.0.1`) — สคริปต์ใส่ workaround ของ Kick ให้อัตโนมัติแล้วถ้าคุณจำเป็นต้องใช้ `127.0.0.1`

---

## 3) บันทึก Client ID / Secret ด้วยสคริปต์ใหม่

```cmd
cd /d "D:\HermesAI\OBS\files"
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_oauth.py setup
```

สิ่งที่ควรเห็นบนหน้าจอ (ตัวอย่างจริง):

```
=== ตั้งค่า Kick Client ID / Client Secret ===
(คัดลอกจาก https://kick.com/settings/developer -> App ของคุณ)

Client ID: 01ABCDEFGHIJKLMNOPQRSTUVWX      <- พิมพ์ Client ID แล้ว Enter
Client Secret (จะไม่แสดงบนหน้าจอ):          <- พิมพ์/วาง Secret แล้ว Enter (ไม่โชว์ตัวอักษร)
Redirect URI (Enter เพื่อใช้ค่าเริ่มต้น http://localhost:8080/callback):   <- Enter เฉย ๆ ถ้าใช้ค่าเริ่มต้น

[OK]  บันทึกแล้ว: D:\HermesAI\OBS\files\kick_credentials.json
      client_id     = 01ABCD...********
      client_secret = (บันทึกแล้ว, ไม่แสดง)
      redirect_uri  = http://localhost:8080/callback
```

หมายเหตุการพิมพ์: **คลิกขวา = วาง** ใน Command Prompt, ใน Windows Terminal ใช้ `Ctrl+V` ได้ (วาง Secret แล้วตัวอักษรจะไม่ขึ้นบนจอเป็นเรื่องปกติ กด Enter ได้เลย)

ผลลัพธ์ที่ได้ — ไฟล์ `kick_credentials.json`:

```json
{
  "client_id": "01ABCDEFGHIJKLMNOPQRSTUVWX",
  "client_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
  "redirect_uri": "http://localhost:8080/callback"
}
```

ไฟล์นี้อยู่ใน `.gitignore` แล้ว — **ห้าม commit ขึ้น Git เด็ดขาด**

---

## 4) เริ่ม OAuth ต่อ (Authorization Code + PKCE)

```cmd
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_oauth.py login
```

ลำดับที่จะเกิดขึ้น:

1. สคริปต์เปิด local server ที่พอร์ต 8080 และเด้งเบราว์เซอร์ไปหน้าอนุญาตของ Kick (`https://id.kick.com/oauth/authorize?...` + PKCE `S256`)
2. ล็อกอิน Kick → กด **Authorize / อนุญาต**
3. เบราว์เซอร์เด้งกลับ `http://localhost:8080/callback?code=...` แล้วขึ้นข้อความ "ได้รับ authorization code แล้ว" (ปิดแท็บได้)
4. สคริปต์แลก code เป็น token ผ่าน `POST https://id.kick.com/oauth/token` แล้วบันทึกที่ `kick_tokens.json`

```
[OK]  ได้ token เรียบร้อย
      scope      : user:read channel:read chat:write
      access_token : eyJhbG...********
      refresh_token: def502...********
      อายุ token  : เหลืออีก 60 นาที (หมดอายุ 2026-10-14 19:22:10)
      บันทึกไว้ที่ : D:\HermesAI\OBS\files\kick_tokens.json
```

**กรณีที่เบราว์เซอร์เปิดอัตโนมัติไม่ได้/ต้องรันบนเครื่องอื่น** ใช้โหมด manual:

```cmd
"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe" kick_oauth.py login --manual
```

ก๊อป URL ที่สคริปต์แสดงไปเปิดเอง → กด Authorize → ก๊อป URL ปลายทาง (ที่มี `?code=...`) กลับมาวางใน Terminal

> `state` จะถูกตรวจอัตโนมัติ ถ้าไม่ตรงสคริปต์จะยกเลิกเพื่อกันการโจมตี CSRF

---

## 5) ตรวจสอบ / ต่ออายุ / ยกเลิก token

```cmd
:: ตรวจว่า credentials + token ยังใช้ได้ และลองเรียก API จริง (GET /public/v1/users)
"...\python.exe" kick_oauth.py check

:: access token อายุ 1 ชั่วโมง — ขอใหม่จาก refresh token เมื่อหมดอายุ
"...\python.exe" kick_oauth.py refresh

:: ยกเลิก token ฝั่ง Kick และลบไฟล์ token ในเครื่อง
"...\python.exe" kick_oauth.py revoke
```

ตัวอย่างการใช้ token ในโค้ดของคุณ:

```python
import json, urllib.request

tokens = json.load(open(r"D:\HermesAI\OBS\files\kick_tokens.json", encoding="utf-8"))
req = urllib.request.Request(
    "https://api.kick.com/public/v1/users",
    headers={"Authorization": "Bearer " + tokens["access_token"]},
)
print(json.load(urllib.request.urlopen(req)))
```

---

## 6) ตารางแก้ปัญหา

| อาการ | สาเหตุ / วิธีแก้ |
| --- | --- |
| `'python' is not recognized...` | ใช้ path เต็ม `"C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe"` หรือเพิ่ม Python ลง PATH |
| `python: can't open file 'kick_oauth.py'` | ยังไม่ได้ `cd /d "D:\HermesAI\OBS\files"` หรือยังไม่ได้คัดลอก `kick_oauth.py` ไปโฟลเดอร์นั้น |
| สคริปต์กรอก ID แล้วค้าง | รันใน Terminal จริง (Command Prompt / Windows Terminal / PowerShell) ไม่ใช่จากโปรแกรมอื่นที่ป้อน stdin ให้ไม่ได้ — ดูข้อ 1 เพื่อฆ่า process เก่า |
| สคริปต์ถาม Secret แต่ตัวอักษรไม่ขึ้น | เป็นเรื่องปกติ (โหมดซ่อน) — วางแล้วกด Enter ได้เลย ถ้าต้องการเห็นเพื่อตรวจ ให้เพิ่ม `--client-secret` หรือแก้ไฟล์ JSON ด้วย Notepad แทน |
| ตอน `login` ขึ้นว่า `เปิดพอร์ต 8080 ไม่ได้` | มีโปรแกรมอื่นใช้พอร์ตอยู่ → เปลี่ยนเป็นพอร์ตอื่น **และแก้ Redirect URL ในหน้า Developer ให้ตรงกันด้วย** เช่น `python kick_oauth.py login --redirect-uri http://localhost:8090/callback` |
| Kick ตอบ error `redirect_uri` / `invalid_request` | ค่าในหน้า Developer กับที่สคริปต์ส่งไม่ตรงกันเป๊ะ — แก้ให้เหมือนกันทุกตัวอักษร หรือรัน `login --redirect-uri "<ค่าที่ตั้งไว้>"` |
| `invalid_client` | Client ID / Secret ไม่ตรง หรือ Secret มีช่องว่างติดมา → รัน `setup` ใหม่ (เลือกเขียนทับ) |
| `invalid_grant` ตอน `refresh` | refresh token หมดอายุ/ถูกใช้ไปแล้ว (Kick หมุน refresh token ทุกครั้ง) → รัน `login` ใหม่ |
| เข้าหน้า Developer ไม่ได้ | ต้องเปิด 2FA ที่บัญชี Kick ก่อน |
| `id.kick.com` ตอบ 404 / เชื่อมต่อไม่ได้ | เป็นปัญหาฝั่ง Kick ที่เคยเกิด (ดู issue #421) หรือเน็ต/ไฟร์วอลล์ของคุณเอง — ลองใหม่อีกครั้ง หรือเช็ค <https://github.com/KickEngineering/KickDevDocs/issues> |
| ข้อความภาษาไทยใน Terminal เพี้ยน | `chcp 65001` แล้วรันใหม่ (สคริปต์บังคับ output เป็น UTF-8 ให้อยู่แล้ว) |
| token หลุด/คิดว่าไม่ปลอดภัย | รัน `revoke` แล้วแก้ Client Secret ในหน้า Developer (rotate) จากนั้น `setup` ใหม่ |

---

## 7) ไฟล์ในชุดนี้

| ไฟล์ | หน้าที่ |
| --- | --- |
| `kick_oauth.py` | CLI OAuth 2.1 (PKCE) ครบในไฟล์เดียว: `setup` / `login` / `check` / `refresh` / `revoke` |
| `tests/test_kick_oauth.py` | ชุดทดสอบออฟไลน์ (7 เคส: PKCE, URL, การอ่านไฟล์ config, ลำดับความสำคัญ, callback server) — รัน `python tests/test_kick_oauth.py` |
| `kick-setup.md` | คู่มือนี้ |
| `kick_credentials.json`, `kick_tokens.json` | ไฟล์ที่เกิดจากการรันจริงในเครื่องคุณ (**ห้าม commit**) — ถูกกันไว้ใน `.gitignore` แล้ว |

### ตัวเลือกของ `kick_oauth.py`

```
--credentials FILE   ระบุไฟล์เก็บ client_id/client_secret (JSON หรือ .env) แทนการค้นหาอัตโนมัติ
--tokens FILE        ระบุไฟล์เก็บ token (ค่าเริ่มต้น kick_tokens.json ข้างไฟล์ credentials)
--redirect-uri URI   override redirect URI
--client-id / --client-secret   ระบุค่าตรง ๆ (ใช้ครั้งเดียว ไม่ต้องมีไฟล์)
--scopes "..."       ค่าเริ่มต้น user:read channel:read chat:write
--manual             login แบบไม่ต้องมี local server
--timeout N          วินาทีที่รอ callback (ค่าเริ่มต้น 300)
```

สคริปต์ยังอ่านค่าจาก environment variable ได้ด้วย: `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`, `KICK_REDIRECT_URI`

---

## 8) อ้างอิง

- เอกสาร Kick อย่างเป็นทางการ: <https://github.com/KickEngineering/KickDevDocs>
- หน้า Developer (สร้าง App / ตั้ง Redirect URL): <https://kick.com/settings/developer>
- สโคปที่ใช้ได้: `user:read`, `channel:read`, `channel:write`, `chat:write`, `streamkey:read`, `events:subscribe`, `moderation:ban`, `moderation:chat_message:manage`, `channel:rewards:read`, `channel:rewards:write`, `kicks:read`, `ads:read`, `ads:write`
- OAuth server: `https://id.kick.com` · API server: `https://api.kick.com`
