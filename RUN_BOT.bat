@echo off
title Institutional SMC Trading Bot
color 0b
cd /d "%~dp0"

echo ======================================================================
echo         INSTITUTIONAL SMC INTRADAY TRADING BOT
echo ======================================================================
echo  Working Directory: %CD%
echo  Starting bot in real-time mode...
echo ======================================================================
echo.

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py intraday_bot.py
) else (
    python intraday_bot.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ======================================================================
    echo [!] Bot process stopped. Window kept open so you can see any errors.
    echo ======================================================================
    pause
)
