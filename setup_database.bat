@echo off
title Masar ERP - Setup Database ^& Superuser
color 0B
cd /d %~dp0

echo ===================================================
echo   Masar ERP - Database Setup ^& Superuser
echo ===================================================
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

echo [1/3] Running makemigrations...
python manage.py makemigrations --noinput

echo.
echo [2/3] Applying migrations (migrate)...
python manage.py migrate --noinput

echo.
echo [3/3] Setting up superuser 'admin'...
python manage.py shell -c "from django.contrib.auth import get_user_model; User = get_user_model(); u, created = User.objects.get_or_create(username='admin', defaults={'email': 'admin@example.com'}); u.set_password('admin123'); u.is_superuser = True; u.is_staff = True; u.is_active = True; u.save(); print('>>> [SUCCESS] Superuser admin created!' if created else '>>> [SUCCESS] Superuser admin password updated!')"

echo.
echo ===================================================
echo   Setup completed successfully!
echo   Username : admin
echo   Password : admin123
echo ===================================================
echo.
pause
