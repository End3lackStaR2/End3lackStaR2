@echo off
rem Double-click this file. It starts the bridge + mock chat and prints the token.
setlocal
rem  Examples
rem    setup.cmd                 start the bridge + mock page, print the token
rem    setup.cmd -Ask            additionally ask once and print PROVEN on success
rem    setup.cmd -Stop           stop only the processes this script started
rem    setup.cmd -Port 8788      use another port
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 pause
