@echo off
rem Bender installer for Windows: runs install.ps1 next to this file.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
