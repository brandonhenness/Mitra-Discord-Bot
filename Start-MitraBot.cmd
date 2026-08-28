@echo off
setlocal
title Mitra Discord Bot

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-MitraBot.ps1"
set "MITRA_EXIT=%ERRORLEVEL%"

if not "%MITRA_EXIT%"=="0" (
    echo.
    echo Mitra stopped with exit code %MITRA_EXIT%.
    pause
)

exit /b %MITRA_EXIT%
