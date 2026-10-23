@echo off
rem ===========================================================================
rem  Double-click this file. It does the whole Kick repair on this machine:
rem
rem    1. finds or creates a local clone of the repo (%USERPROFILE%\ebs)
rem    2. updates it to the latest branch
rem    3. runs hermes\fix-now.ps1 (found by wildcard, never by typing the name)
rem    4. leaves the verdict on screen and pauses so you can read it
rem
rem  Nothing is typed into a chat, so nothing can be turned into a link.
rem  Arguments are passed straight through, e.g.  run-fix.cmd -DryRun
rem ===========================================================================
setlocal EnableExtensions
set "RC=2"

set "REPO_URL=https://github.com/End3lackStaR2/End3lackStaR2.git"
set "BRANCH=arena/a508b692-end3lackstar2"
set "CLONE=%USERPROFILE%\ebs"

echo ============================================================
echo  Kick notifier repair
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo [X] git was not found on PATH.
    echo     Install Git for Windows, then run this file again.
    goto :finish
)

where powershell >nul 2>nul
if errorlevel 1 (
    echo [X] powershell was not found.
    goto :finish
)

if not exist "%CLONE%\.git" (
    echo [1/3] cloning into "%CLONE%" ...
    git clone --depth 1 --branch %BRANCH% "%REPO_URL%" "%CLONE%"
    if errorlevel 1 (
        echo.
        echo [X] clone failed. Check the network, then run this file again.
        goto :finish
    )
) else (
    echo [1/3] existing clone found - updating ...
    git -C "%CLONE%" fetch --depth 1 origin %BRANCH% >nul 2>nul
    git -C "%CLONE%" checkout -q -B %BRANCH% FETCH_HEAD
    if errorlevel 1 (
        echo [X] could not update the clone. Delete "%CLONE%" and run again.
        goto :finish
    )
)

echo [2/3] looking for the repair script ...
set "FIX="
if exist "%CLONE%\hermes\fix-now.ps1" set "FIX=%CLONE%\hermes\fix-now.ps1"

if not defined FIX (
    echo [X] fix-now.ps1 is not in the clone.
    echo     The branch may have moved. Run: git -C "%CLONE%" log --oneline -3
    goto :finish
)
echo       found: %FIX%

echo [3/3] running it - this may restart the notifier ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%FIX%" %*
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" (
    echo  VERDICT: OK   - the notifier is proven healthy.
    echo  You can close this window.
) else if "%RC%"=="1" (
    echo  NO FRESH EVIDENCE - the notifier started but wrote nothing new.
    echo  Read the detail above, then run:  report-evidence.cmd
) else (
    echo  SETUP FAILED or KEY REJECTED - read the lines above.
    echo  If it names the OLD key, another copy still reads it from code.
)
echo ============================================================
echo.
echo  Report file (if written):
echo    %CLONE%\hermes\files\hermes_fix_now_report.txt
echo.

goto :finish

:finish
echo.
pause
endlocal
exit /b %RC%
