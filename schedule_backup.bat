@echo off
chcp 65001 >nul
title Schedule Automatic Backup - Masar ERP
color 0B
cd /d %~dp0

rem Registers a daily Windows Task Scheduler task that runs "manage.py auto_backup"
rem at 02:00. The command itself only backs up supermarkets that turned on
rem "auto backup" in System Settings, and keeps the configured number of copies.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "MANAGE=%ROOT%\manage.py"
set "TASK_NAME=MasarERP AutoBackup"

echo ===================================================
echo   جدولة النسخ الاحتياطي التلقائي اليومي
echo   Schedule the daily automatic backup
echo ===================================================
echo.

if not exist "%PY%" (
    echo [خطأ] لم يتم العثور على البيئة الافتراضية venv. شغّل setup_venv.bat أولاً.
    echo [ERROR] Virtual environment 'venv' not found! Please run setup_venv.bat first.
    pause
    exit /b 1
)

echo سيتم إنشاء مهمة يومية الساعة 02:00 صباحاً باسم "%TASK_NAME%"
echo A daily task will be created at 02:00 named "%TASK_NAME%"
echo   %PY% %MANAGE% auto_backup
echo.
echo ملاحظة: يجب تفعيل "نسخ احتياطي تلقائي يومي" من إعدادات النظام لكل سوبر ماركت.
echo Note: turn on "auto backup" in System Settings for each supermarket.
echo ملاحظة: المهمة تعمل والمستخدم مسجل الدخول على الجهاز، والجهاز يجب أن يكون يعمل في هذا الوقت.
echo Note: the task runs while this user is logged on; the PC must be on at that time.
echo.

schtasks /Create /SC DAILY /ST 02:00 /TN "%TASK_NAME%" /TR "\"%PY%\" \"%MANAGE%\" auto_backup" /F
if %errorlevel% neq 0 (
    echo.
    echo [خطأ] فشل إنشاء المهمة. جرّب تشغيل الملف كمسؤول ^(Run as administrator^).
    echo [ERROR] Could not create the task. Try "Run as administrator".
    pause
    exit /b 1
)

echo.
echo [تم] تمت جدولة النسخ الاحتياطي التلقائي يومياً الساعة 02:00.
echo [OK] Automatic backup scheduled daily at 02:00.
echo لإلغاء الجدولة / To remove it:  schtasks /Delete /TN "%TASK_NAME%" /F
echo لتجربتها الآن / To run it now:   schtasks /Run /TN "%TASK_NAME%"
echo.
pause
exit /b 0
