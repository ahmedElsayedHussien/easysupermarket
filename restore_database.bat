@echo off
chcp 65001 >nul
title Database Restore Tool - Masar ERP
color 0E
cd /d %~dp0

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    python manage.py db_backup --restore
) else (
    echo [ERROR] Virtual environment 'venv' not found!
    echo Please run setup_venv.bat first.
)

echo.
pause
