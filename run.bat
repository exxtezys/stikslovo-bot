@echo off
title StickerBot
echo =======================================
echo   StickerBot - Telegram Sticker Search
echo =======================================
echo.
cd /d "%~dp0"

:: Check for .env
if not exist ".env" (
    echo [!] .env file not found!
    echo.
    echo Copy .env.example to .env and set your BOT_TOKEN:
    echo   1. Create .env file in this folder
    echo   2. Add: BOT_TOKEN=your_token_from_BotFather
    echo.
    pause
    exit /b 1
)

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install dependencies
echo [*] Installing dependencies...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [!] Failed to install dependencies.
    pause
    exit /b 1
)

echo [*] Starting bot...
python main.py

pause
