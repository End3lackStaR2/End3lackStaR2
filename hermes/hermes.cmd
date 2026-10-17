@echo off
rem hermes.cmd - double-clickable launcher for hermes.ps1
rem   hermes.cmd                  -> fix (repair + restart + verify)
rem   hermes.cmd doctor           -> read-only diagnosis
rem   hermes.cmd logs             -> resolved log path + tail
rem   hermes.cmd fix -DryRun      -> show what would change
setlocal
set "HERE=%~dp0"
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%hermes.ps1" fix
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%hermes.ps1" %*
)
echo.
echo exit code: %ERRORLEVEL%
if not "%2"=="-NoPause" pause
endlocal
