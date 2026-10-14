@echo off
chcp 65001 >nul
setlocal
rem ─────────────────────────────────────────────────────────────────────────────
rem  ดับเบิลคลิกไฟล์นี้ได้เลย: จะรันตรวจทั้งชุด (key/เส้นทาง, ไฟล์ที่มี key เก่า, log)
rem  แล้วรวมผลเป็นไฟล์เดียวไว้ส่งกลับมาให้ดู
rem
rem  ถ้าอยากกำหนดโฟลเดอร์/log/config เอง แก้ค่าด้านล่างได้
rem ─────────────────────────────────────────────────────────────────────────────
set "ROOT=D:\HermesAI\OBS"
set "LOG=%ROOT%\files\kick_notifier.log"
set "CONFIG=%ROOT%\files\config.json"

set "PY=C:\Users\mail3\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%~dp0.."
echo กำลังตรวจ... (รอสักครู่)
if exist "%LOG%" (
  "%PY%" "tools\run_and_report.py" --root "%ROOT%" --log "%LOG%" --config "%CONFIG%" --follow 20
) else (
  echo [!] ไม่พบไฟล์ log ที่ %LOG%
  echo     จะตรวจโดยไม่ดู log (ระบุ path เองได้ในไฟล์นี้)
  "%PY%" "tools\run_and_report.py" --root "%ROOT%" --no-log
)

echo.
echo ถ้าต้องการส่งผลกลับ: เปิดไฟล์ kick_report_*.txt ที่โฟลเดอร์นี้ แล้วคัดลอกมาวาง
pause
