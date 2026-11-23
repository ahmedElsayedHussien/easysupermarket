import os
import sys
import shutil
import stat
import compileall

def remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
DEST_DIR = r"E:\easysupermarket_release"

INCLUDE_DIRS = ['apps', 'config', 'templates', 'static', 'staticfiles', 'locale', 'media']
INCLUDE_FILES = [
    'manage.py', 'run_waitress.py', 'start_server.bat', 'switch_database.bat',
    'setup_database.bat', 'setup_venv.bat', 'backup_database.bat', 'restore_database.bat',
    'switch_db.py', 'logo.png', '.env', 'requirements.txt', 'db.sqlite3'
]

print(f"=== Starting Bytecode Release Build ===")
print(f"Source: {SOURCE_DIR}")
print(f"Destination: {DEST_DIR}")

if os.path.exists(DEST_DIR):
    print(f"Cleaning existing destination: {DEST_DIR}...")
    for item in os.listdir(DEST_DIR):
        item_path = os.path.join(DEST_DIR, item)
        try:
            if os.path.isdir(item_path):
                shutil.rmtree(item_path, onerror=remove_readonly)
            else:
                try:
                    os.chmod(item_path, stat.S_IWRITE)
                except Exception:
                    pass
                os.remove(item_path)
        except Exception as e:
            print(f"Notice cleaning {item}: {e}")

os.makedirs(DEST_DIR, exist_ok=True)

# 1. Copy selected directories
for d in INCLUDE_DIRS:
    src_d = os.path.join(SOURCE_DIR, d)
    dst_d = os.path.join(DEST_DIR, d)
    if os.path.exists(src_d):
        print(f"Copying directory: {d}...")
        shutil.copytree(src_d, dst_d, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git*'))

# Ensure logs directory exists for Django logging
os.makedirs(os.path.join(DEST_DIR, 'logs'), exist_ok=True)

# 2. Copy selected files
for f in INCLUDE_FILES:
    src_f = os.path.join(SOURCE_DIR, f)
    dst_f = os.path.join(DEST_DIR, f)
    if os.path.exists(src_f):
        print(f"Copying file: {f}...")
        shutil.copy2(src_f, dst_f)

# 3. Compile all python files to legacy .pyc (placed directly alongside .py)
print("\nCompiling all Python files to bytecode (.pyc)...")
compiled = compileall.compile_dir(DEST_DIR, force=True, legacy=True, quiet=1)
if not compiled:
    print("Warning: some files may have failed compilation, continuing...")

# 4. Remove all .py source files from apps and config in destination
print("\nRemoving source .py files from release...")
removed_count = 0
for root, dirs, files in os.walk(DEST_DIR):
    # Also remove any __pycache__ folders if created
    if '__pycache__' in dirs:
        shutil.rmtree(os.path.join(root, '__pycache__'), ignore_errors=True)
        dirs.remove('__pycache__')
        
    for file in files:
        if file.endswith('.py'):
            if file in ('manage.py', 'switch_db.py') and os.path.abspath(root) == os.path.abspath(DEST_DIR):
                continue
            py_path = os.path.join(root, file)
            pyc_path = py_path + 'c'
            if os.path.exists(pyc_path):
                os.remove(py_path)
                removed_count += 1
            else:
                print(f"Notice: skipped deleting {file} because .pyc was not found.")

print(f"Successfully compiled and deleted {removed_count} original .py files!")

# 5. Verify release by testing Django import
print("\nVerifying release build with Django...")
sys.path.insert(0, DEST_DIR)
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.development'
import django
django.setup()
print("[OK] Django successfully initialized from compiled bytecode!")

from config.wsgi import application
print("[OK] WSGI application successfully loaded from compiled bytecode!")

print(f"\n=== Build Complete Successfully! ===")
print(f"Release folder ready at: {DEST_DIR}")
