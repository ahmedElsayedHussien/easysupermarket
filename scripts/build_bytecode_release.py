"""
Build a bytecode-only release (no .py sources) that runs on any new Windows machine.

    venv\\Scripts\\python.exe scripts\\build_bytecode_release.py [DEST_DIR]

- Code is copied and compiled to legacy .pyc; .py sources are removed
  (except manage.py and switch_db.py, which the .bat tools run directly).
- NO machine data is shipped: no .env, no database, no uploaded media, no
  backups. On a new machine start_server.bat runs the first-time setup
  (venv, .env with a fresh SECRET_KEY, database, chart of accounts, admin user).
- Rebuilding into an existing release folder KEEPS that machine's data:
  .env, db.sqlite3*, media/, backups/, logs/, venv/ are never deleted.
"""
import os
import sys
import shutil
import stat
import compileall
import subprocess


def remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (script lives in scripts/)
DEST_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else r"E:\easysupermarket_release"

# Code & assets (copied fresh on every build)
INCLUDE_DIRS = ['apps', 'config', 'templates', 'static', 'staticfiles', 'locale', 'guides']
INCLUDE_FILES = [
    'manage.py', 'run_waitress.py', 'switch_db.py',
    'start_server.bat', 'setup_venv.bat', 'setup_database.bat', 'switch_database.bat',
    'backup_database.bat', 'restore_database.bat', 'schedule_backup.bat',
    'requirements.txt', '.env.example', 'logo.png',
    'SETUP_GUIDE.txt', 'دليل تثبيت وتشغيل نظام.docx',
]
# Extra folders mapped to a different name in the release
EXTRA_DIRS = {
    os.path.join('local_signer', 'dist'): 'signer',   # e-invoice signing tool (exe + config.ini)
}
# Machine data: never shipped, never deleted on rebuild
PRESERVE = {'.env', 'media', 'backups', 'logs', 'venv'}
PRESERVE_PREFIXES = ('db.sqlite3',)
# Not needed at runtime
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '.git*', 'tests', 'tests.py', 'test.py')


def is_preserved(name):
    return name in PRESERVE or name.startswith(PRESERVE_PREFIXES)


print("=== Starting Bytecode Release Build ===")
print(f"Source: {SOURCE_DIR}")
print(f"Destination: {DEST_DIR}")
if os.path.abspath(DEST_DIR) == os.path.abspath(SOURCE_DIR):
    sys.exit("[ERROR] Destination must not be the source folder.")

if os.path.exists(DEST_DIR):
    print("Cleaning old code from destination (keeping .env, database, media, backups, logs, venv)...")
    for item in os.listdir(DEST_DIR):
        if is_preserved(item):
            print(f"  keeping: {item}")
            continue
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

# 1. Copy code directories
for d in INCLUDE_DIRS:
    src_d = os.path.join(SOURCE_DIR, d)
    if os.path.exists(src_d):
        print(f"Copying directory: {d}...")
        shutil.copytree(src_d, os.path.join(DEST_DIR, d), dirs_exist_ok=True, ignore=IGNORE)
for src_rel, dst_rel in EXTRA_DIRS.items():
    src_d = os.path.join(SOURCE_DIR, src_rel)
    if os.path.exists(src_d):
        print(f"Copying directory: {src_rel} -> {dst_rel}...")
        shutil.copytree(src_d, os.path.join(DEST_DIR, dst_rel), dirs_exist_ok=True)

# Empty runtime folders (kept if they already exist)
for d in ('logs', 'media', 'backups'):
    os.makedirs(os.path.join(DEST_DIR, d), exist_ok=True)

# 2. Copy files
for f in INCLUDE_FILES:
    src_f = os.path.join(SOURCE_DIR, f)
    if os.path.exists(src_f):
        print(f"Copying file: {f}...")
        shutil.copy2(src_f, os.path.join(DEST_DIR, f))
    else:
        print(f"Notice: {f} not found, skipped.")

# 3. Compile all python files to legacy .pyc (placed directly alongside .py)
print("\nCompiling all Python files to bytecode (.pyc)...")
code_dirs = [os.path.join(DEST_DIR, d) for d in INCLUDE_DIRS if os.path.isdir(os.path.join(DEST_DIR, d))]
ok = all(compileall.compile_dir(d, force=True, legacy=True, quiet=1) for d in code_dirs)
for f in ('manage.py', 'run_waitress.py', 'switch_db.py'):
    p = os.path.join(DEST_DIR, f)
    if os.path.exists(p):
        ok = compileall.compile_file(p, force=True, legacy=True, quiet=1) and ok
if not ok:
    sys.exit("[ERROR] Some files failed to compile - release NOT usable.")

# 4. Remove .py sources (manage.py and switch_db.py stay: the .bat tools run them)
print("\nRemoving source .py files from release...")
removed_count = 0
for root, dirs, files in os.walk(DEST_DIR):
    rel_top = os.path.relpath(root, DEST_DIR).split(os.sep)[0]
    if is_preserved(rel_top):
        dirs[:] = []          # never touch venv / media / backups / logs
        continue
    if '__pycache__' in dirs:
        shutil.rmtree(os.path.join(root, '__pycache__'), ignore_errors=True)
        dirs.remove('__pycache__')
    for file in files:
        if not file.endswith('.py'):
            continue
        if file in ('manage.py', 'switch_db.py') and os.path.abspath(root) == os.path.abspath(DEST_DIR):
            continue
        py_path = os.path.join(root, file)
        if os.path.exists(py_path + 'c'):
            os.remove(py_path)
            removed_count += 1
        else:
            print(f"Notice: skipped deleting {file} because .pyc was not found.")
print(f"Compiled and removed {removed_count} .py source files.")

# 5. Verify in a separate process from inside the release folder
print("\nVerifying release (manage.py check)...")
env = dict(os.environ, DJANGO_SETTINGS_MODULE='config.settings.development')
if not os.path.exists(os.path.join(DEST_DIR, '.env')):
    env.setdefault('DB_ENGINE', 'sqlite')   # fresh release: same default a new machine gets
res = subprocess.run([sys.executable, 'manage.py', 'check'], cwd=DEST_DIR, env=env, capture_output=True, text=True)
print(res.stdout.strip() or res.stderr.strip()[-2000:])
if res.returncode != 0:
    sys.exit("[ERROR] Release verification failed.")

print("\n=== Build Complete Successfully! ===")
print(f"Release folder ready at: {DEST_DIR}")
print("New machine: copy the folder (without venv), double-click start_server.bat.")
