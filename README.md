# End3lackStaR2

Config files for my GitHub profile.

## เครื่องมือในโปรเจกต์นี้

- **[kick-setup.md](kick-setup.md)** — คู่มือตั้งค่า Kick Client ID / Client Secret และทำ OAuth 2.1 (PKCE) ให้ได้ token บน Windows
- **[kick_oauth.py](kick_oauth.py)** — CLI ตัวช่วย: `setup` / `login` / `check` / `refresh` / `revoke` (Python 3.10+, ไม่ต้องติดตั้งแพ็กเกจเพิ่ม)
- **[tests/test_kick_oauth.py](tests/test_kick_oauth.py)** — ชุดทดสอบออฟไลน์: `python tests/test_kick_oauth.py`

> ไฟล์ `kick_credentials.json` / `kick_tokens.json` เป็นข้อมูลลับ ถูกกันไว้ใน `.gitignore` แล้ว
