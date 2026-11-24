@echo off
chcp 65001 >nul
title Masar ERP - Setup Virtual Environment
color 0B
cd /d %~dp0

echo =========================================================
echo   Masar ERP - Creating Virtual Environment [venv]
echo =========================================================
echo.

:CHECK_PYTHON
set PY_CMD=

:: 1. Check py -3.11 launcher first
py -3.11 --version >nul 2>&1
if %errorlevel% equ 0 (
    set PY_CMD=py -3.11
    goto PY_FOUND
)

:: 2. Check standard Python 3.11 install locations
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto PY_FOUND
)

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python311\python.exe"
    goto PY_FOUND
)

if exist "C:\Python311\python.exe" (
    set "PY_CMD=C:\Python311\python.exe"
    goto PY_FOUND
)

:: 3. Check if default python in PATH is Python 3.11
python --version > temp_py_ver.txt 2>&1
findstr /i "3.11" temp_py_ver.txt >nul 2>&1
if %errorlevel% equ 0 goto PYTHON_PATH_IS_311

del temp_py_ver.txt >nul 2>&1
goto TRY_INSTALLER

:PYTHON_PATH_IS_311
del temp_py_ver.txt >nul 2>&1
set PY_CMD=python
goto PY_FOUND

:TRY_INSTALLER
:: If Python 3.11 is not found, try to install it using python_installer.exe
if not exist "python_installer.exe" goto NO_INSTALLER

echo [INFO] Python 3.11 is not installed on this machine.
echo [INFO] Running 'python_installer.exe' to install Python 3.11 automatically...
echo Please wait for the installer to finish...
start /wait "" python_installer.exe /passive PrependPath=1 Include_test=0
echo [INFO] Installer finished. Checking again...
ping 127.0.0.1 -n 4 >nul

py -3.11 --version >nul 2>&1
if %errorlevel% equ 0 (
    set PY_CMD=py -3.11
    goto PY_FOUND
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto PY_FOUND
)

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python311\python.exe"
    goto PY_FOUND
)

if exist "C:\Python311\python.exe" (
    set "PY_CMD=C:\Python311\python.exe"
    goto PY_FOUND
)

:NO_INSTALLER
echo.
echo [ERROR] Python 3.11 was not found and could not be installed!
echo Please install Python 3.11 manually [check 'Add python.exe to PATH'].
echo.
pause
exit /b 1

:PY_FOUND
echo [OK] Using Python: %PY_CMD%
echo.

:: Check existing venv
if not exist "venv\Scripts\python.exe" goto CREATE_VENV

:: Check if existing venv is Python 3.11
"venv\Scripts\python.exe" --version > temp_venv_ver.txt 2>&1
findstr /i "3.11" temp_venv_ver.txt >nul 2>&1
if %errorlevel% equ 0 goto VENV_IS_311

del temp_venv_ver.txt >nul 2>&1
echo [WARNING] Existing 'venv' is not Python 3.11.
echo [INFO] Removing old venv to recreate with Python 3.11...
goto CREATE_VENV

:VENV_IS_311
del temp_venv_ver.txt >nul 2>&1
echo [1/4] Virtual environment 'venv' already exists [Python 3.11 verified].
goto ACTIVATE_VENV

:CREATE_VENV
echo [1/4] Creating new virtual environment 'venv' with Python 3.11...
taskkill /F /IM python.exe >nul 2>&1
ping 127.0.0.1 -n 2 >nul
if exist "venv\" rmdir /s /q venv >nul 2>&1

if "%PY_CMD%"=="py -3.11" goto VENV_VIA_PY_LAUNCHER
"%PY_CMD%" -m venv venv
goto CHECK_VENV_CREATED

:VENV_VIA_PY_LAUNCHER
py -3.11 -m venv venv

:CHECK_VENV_CREATED
if not exist "venv\Scripts\python.exe" goto VENV_FAILED
echo [OK] Virtual environment created successfully.
goto ACTIVATE_VENV

:VENV_FAILED
echo.
echo [ERROR] Failed to create virtual environment 'venv'!
echo.
pause
exit /b 1

:ACTIVATE_VENV
echo [2/4] Activating virtual environment...
rem (no activate.bat: the venv python is called directly, folder-name independent)

:: Check and install requirements
echo [3/4] Checking required packages...
venv\Scripts\python.exe -c "import sys; from importlib.metadata import distributions; norm = lambda n: n.strip().lower().replace('_', '-'); installed = {norm(d.metadata['Name']): d.version for d in distributions() if d.metadata.get('Name')}; reqs = [l.strip() for l in open('requirements.txt', encoding='utf-8') if l.strip() and not l.strip().startswith('#')]; bad = [r for r in reqs if norm(r.split('==')[0].split('>=')[0].split('<=')[0]) not in installed or ('==' in r and installed.get(norm(r.split('==')[0])) != r.split('==')[1].strip())]; sys.exit(1 if bad else 0)" >nul 2>&1

if %errorlevel% equ 0 goto PACKAGES_ALREADY_INSTALLED

echo [INFO] Installing packages from requirements.txt...
venv\Scripts\python.exe -m pip install --timeout 120 --retries 5 -r requirements.txt
if %errorlevel% neq 0 goto PIP_FAILED
goto RUN_DJANGO_SETUP

:PACKAGES_ALREADY_INSTALLED
echo [OK] All required packages are already installed.
goto RUN_DJANGO_SETUP

:PIP_FAILED
echo.
echo [ERROR] Failed to install packages! Please check your internet connection.
echo.
pause
exit /b 1

:RUN_DJANGO_SETUP
echo [4/4] Database setup (.env, migrations, chart of accounts, admin user) and static files...
set "SETUP_NO_PAUSE=1"
call setup_database.bat
set "SETUP_NO_PAUSE="
venv\Scripts\python.exe manage.py sync_admin_permissions
venv\Scripts\python.exe manage.py collectstatic --noinput

echo.
echo =========================================================
echo   Setup Completed Successfully!
echo   You can now start the server by clicking 'start_server.bat'
echo =========================================================
echo.
pause
