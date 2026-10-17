@echo off
rem Double-click this file. It starts the bridge + mock chat and prints the token.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 pause
