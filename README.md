# End3lackStaR2

Config files for my GitHub profile.

## เครื่องมือในโปรเจกต์นี้

- **[kick-chat-fix.md](kick-chat-fix.md)** — คู่มือหลัก: **สาเหตุยืนยันแล้ว** — Kick ปิด Pusher app ตัวเก่า (`32cbd69e4b950bf97679`) ต้นเดือน ต.ค. 2026 ทำให้ได้ error `4001 App key ... not in this cluster`; key ปัจจุบันคือ `34bf7a0ff419a2a775b9` (cluster us2) และ Kick มี endpoint ให้ถามได้เสมอว่าใช้ app ไหน
- **[tools/run_and_report.py](tools/run_and_report.py)** + **[tools/RUN-ME-report.cmd](tools/RUN-ME-report.cmd)** — รันตรวจทั้งชุดแล้วรวมผลเป็นไฟล์เดียว (บน Windows ดับเบิลคลิก `.cmd` ได้เลย)
- **[tools/check_connection_log.py](tools/check_connection_log.py)** — อ่าน log ของโปรแกรมแจ้งเตือนของคุณ แล้วบอกว่าต่อติด (`connection_established`) / ยังถูกปฏิเสธ / key เก่า / เน็ตมีปัญหา
- **[tools/github-actions-kick-probe.yml](tools/github-actions-kick-probe.yml)** — template workflow ให้ GitHub รัน probe จากเครือข่ายที่สอง (ลิงก์คลิกเดียวอยู่ที่ `tools/actions-setup-link.txt`)
- **[tools/pusher_sweep.py](tools/pusher_sweep.py)** — ยิงทดสอบทุก cluster × ทุก key พร้อมกัน (`--discover` = ถาม Kick ก่อน, `--json` = เก็บผล)
- **[kick_probe.html](kick_probe.html)** — ทดสอบในเบราว์เซอร์ (ดับเบิลคลิก) ไล่หา cluster+key ที่ Pusher ยอมรับจริง
- **[ws_probe.py](ws_probe.py)** — ไฟล์เดียว 0 dependency: ตรวจ DNS / TCP / TLS (ใบรับรอง จับ MITM) / handshake / key (จับอักขระซ่อน) → ตัดสินว่าปัญหาอยู่ที่เส้นทางหรือที่ key
- **[kick_chat.py](kick_chat.py)** — วินิจฉัย/ซ่อม config (`report`, `fix-config`, `scan`, `extract`, `probe`, `doctor`) + ฟังแชทสด (`listen`) + ส่งข้อความ (`send`)
- **[kick_events.py](kick_events.py)** — Events API ทางการ (webhook): `setup` / `serve` (+overlay โปร่งใสสำหรับ OBS ผ่าน SSE) / `subscribe` / `list` / `delete` / `status` / `verify` (ตรวจลายเซ็น RSA ด้วย stdlib)
- **[kick-setup.md](kick-setup.md)** — ตั้งค่า Kick Client ID / Secret + OAuth 2.1 (PKCE) บน Windows
- **[kick_oauth.py](kick_oauth.py)** — CLI OAuth: `setup` / `login` / `check` / `refresh` / `revoke`
- **`tests/`** — เทสต์ออฟไลน์ 97 เคส + เทสต์หน้าเว็บ 2 ไฟล์ รันทั้งหมดด้วยคำสั่งเดียวได้:
  ```cmd
  python tests/run_all.py
  ```
  (เทสต์ JavaScript ต้องมี Node.js — ถ้าไม่มีจะข้ามให้ · ใช้ `--python-only` ถ้าไม่ต้องการรัน)

> **สำคัญ (ต.ค. 2026):** ถ้าเคยตั้งค่า Pusher key ไว้เป็น `32cbd69e4b950bf97679` ต้องเปลี่ยนเป็น
> `34bf7a0ff419a2a775b9` — key เก่าถูก Kick ปิดแล้ว จะได้ error `4001 not in this cluster` เสมอ
> ตรวจ/ซ่อมอัตโนมัติ: `python kick_chat.py fix-config --config-file <ไฟล์ config ของคุณ> --apply`
> หรือถาม Kick ตรง ๆ: `python ws_probe.py --discover`

> ไฟล์ `kick_credentials.json` / `kick_tokens.json` / `kick_chat_config.json` เป็นข้อมูลลับ ถูกกันไว้ใน `.gitignore` แล้ว
