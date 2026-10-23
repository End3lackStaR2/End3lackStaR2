@echo off
rem selfcheck.cmd - prove the tooling itself works on this machine before you run it for real.
rem Runs: hermes.ps1 -SelfTest, the Pusher protocol self test, and the patcher unit tests.
setlocal
set "HERE=%~dp0"
set RC=0

echo === 1/3  hermes.ps1 self test ==============================
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%hermes.ps1" -SelfTest
if errorlevel 1 set RC=1

echo.
echo === 2/3  websocket protocol self test (stdlib only) ========
where python >nul 2>nul
if errorlevel 1 (
    echo [skip] python is not on PATH
) else (
    python "%HERE%tools\kick_pusher_probe.py" --selftest
    if errorlevel 1 set RC=1
)

echo.
echo === 3/3  patcher tests =====================================
where python >nul 2>nul
if errorlevel 1 (
    echo [skip] python is not on PATH
) else (
    pushd "%HERE%tools"
    python -m unittest test_patch_kick_app
    if errorlevel 1 set RC=1
    popd
)

echo.
if "%RC%"=="0" (echo RESULT: all self checks passed) else (echo RESULT: something failed - see above)
echo.
pause
exit /b %RC%
