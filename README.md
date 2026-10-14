# End3lackStaR2

Config files for my GitHub profile.

## เครื่องมือในโปรเจกต์นี้

- **[kick-setup.md](kick-setup.md)** — คู่มือตั้งค่า Kick Client ID / Client Secret และทำ OAuth 2.1 (PKCE) ให้ได้ token บน Windows
- **[kick_oauth.py](kick_oauth.py)** — CLI ตัวช่วย: `setup` / `login` / `check` / `refresh` / `revoke` (Python 3.10+, ไม่ต้องติดตั้งแพ็กเกจเพิ่ม)
- **`tests/`** — ชุดทดสอบออฟไลน์: `python tests/test_kick_oauth.py` และ `python tests/test_kick_chat.py`
- **[kick-chat-fix.md](kick-chat-fix.md)** — แก้ error `App key ... not in this cluster` (Kick chat ใช้ Pusher cluster **us2**)
- **[kick_chat.py](kick_chat.py)** — วินิจฉัย/ซ่อม config (`report`, `fix-config`, `scan`, `extract`, `probe`), ฟังแชทสด (`listen`), ส่งข้อความ (`send`), `resolve`

> ไฟล์ `kick_credentials.json` / `kick_tokens.json` เป็นข้อมูลลับ ถูกกันไว้ใน `.gitignore` แล้ว
