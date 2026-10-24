@echo off
title Reset & Start Fresh - Institutional Trading Bot
color 0a
cd /d "%~dp0"

echo ======================================================================
echo         RESETTING BOT STATE & STARTING FRESH
echo ======================================================================
echo.

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py intraday_bot.py --reset
    echo.
    echo ======================================================================
    echo Starting fresh trading loop now...
    echo ======================================================================
    py intraday_bot.py
) else (
    python intraday_bot.py --reset
    echo.
    echo ======================================================================
    echo Starting fresh trading loop now...
    echo ======================================================================
    python intraday_bot.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    pause
)
