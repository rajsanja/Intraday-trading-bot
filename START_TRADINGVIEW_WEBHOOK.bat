@echo off
title TradingView Webhook Bridge - Delta / Binance
color 0e
cd /d "%~dp0"

echo ======================================================================
echo         TRADINGVIEW WEBHOOK BRIDGE FOR AUTOMATED ORDERS
echo ======================================================================
echo  Working Directory: %CD%
echo  Listening for TradingView Alerts on port 5000...
echo ======================================================================
echo.

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py tradingview_webhook.py
) else (
    python tradingview_webhook.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ======================================================================
    echo [!] Server stopped. Window kept open so you can see any messages.
    echo ======================================================================
    pause
)
