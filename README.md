# End3lackStaR2

Config files for my GitHub profile.

## เครื่องมือในโปรเจกต์นี้

- **[kick-chat-fix.md](kick-chat-fix.md)** — คู่มือหลัก: แก้ error `App key ... not in this cluster` (Kick chat ใช้ Pusher cluster **us2**), วิธีโหลดไฟล์, กับดัก log เก่า และทางเลือก Events API
- **[kick_probe.html](kick_probe.html)** — ทดสอบในเบราว์เซอร์ (ดับเบิลคลิก) ไล่หา cluster+key ที่ Pusher ยอมรับจริง
- **[ws_probe.py](ws_probe.py)** — ไฟล์เดียว 0 dependency: ตรวจ DNS / TCP / TLS (ใบรับรอง จับ MITM) / handshake / key (จับอักขระซ่อน) → ตัดสินว่าปัญหาอยู่ที่เส้นทางหรือที่ key
- **[kick_chat.py](kick_chat.py)** — วินิจฉัย/ซ่อม config (`report`, `fix-config`, `scan`, `extract`, `probe`, `doctor`) + ฟังแชทสด (`listen`) + ส่งข้อความ (`send`)
- **[kick_events.py](kick_events.py)** — Events API ทางการ (webhook): `setup` / `serve` (+overlay โปร่งใสสำหรับ OBS ผ่าน SSE) / `subscribe` / `list` / `delete` / `status` / `verify` (ตรวจลายเซ็น RSA ด้วย stdlib)
- **[kick-setup.md](kick-setup.md)** — ตั้งค่า Kick Client ID / Secret + OAuth 2.1 (PKCE) บน Windows
- **[kick_oauth.py](kick_oauth.py)** — CLI OAuth: `setup` / `login` / `check` / `refresh` / `revoke`
- **`tests/`** — เทสต์ออฟไลน์ 63 เคส (`tests/test_kick_chat.py`, `tests/test_ws_probe_events.py`, `tests/test_kick_oauth.py`)

> ไฟล์ `kick_credentials.json` / `kick_tokens.json` / `kick_chat_config.json` เป็นข้อมูลลับ ถูกกันไว้ใน `.gitignore` แล้ว
