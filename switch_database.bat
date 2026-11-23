@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Database Switcher ^& Diagnostics - Masar ERP

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    python switch_db.py
) else (
    echo [ERROR] Virtual environment 'venv' not found!
    echo Please run setup_venv.bat first.
    pause
)
