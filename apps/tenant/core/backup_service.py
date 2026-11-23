import os
import glob
import shutil
import subprocess
import datetime
from django.conf import settings
from django.db import connection

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

def get_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR

def find_pg_tool(tool_name='pg_dump'):
    """
    Search for pg_dump.exe, pg_restore.exe, or psql.exe on Windows.
    """
    # 1. Check system PATH
    found = shutil.which(tool_name)
    if found:
        return found
    
    # 2. Check standard Windows PostgreSQL install directories
    patterns = [
        rf'C:\Program Files\PostgreSQL\*\bin\{tool_name}.exe',
        rf'C:\Program Files (x86)\PostgreSQL\*\bin\{tool_name}.exe',
        rf'D:\Program Files\PostgreSQL\*\bin\{tool_name}.exe',
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
        
    if candidates:
        # Sort descending to pick newest PostgreSQL version installed
        candidates.sort(reverse=True)
        return candidates[0]
        
    return None


def get_current_db_info():
    """
    Returns engine type ('postgresql' or 'sqlite'), db_name, host, port, user.
    """
    engine = connection.settings_dict.get('ENGINE', '')
    is_pg = 'postgresql' in engine
    
    if is_pg:
        db_name = connection.settings_dict.get('NAME', 'easysupermarket')
        user = connection.settings_dict.get('USER', 'postgres')
        host = connection.settings_dict.get('HOST', 'localhost')
        port = str(connection.settings_dict.get('PORT', '5432'))
        return {
            'engine': 'postgresql',
            'display_engine': 'PostgreSQL',
            'name': db_name,
            'user': user,
            'host': host,
            'port': port,
        }
    else:
        db_path = connection.settings_dict.get('NAME')
        size_bytes = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
        return {
            'engine': 'sqlite',
            'display_engine': 'SQLite',
            'name': os.path.basename(str(db_path)),
            'path': str(db_path),
            'size': size_bytes,
        }


def create_backup():
    """
    Creates a full backup file in BACKUP_DIR.
    Returns (success: bool, filename_or_error: str, filepath: str)
    """
    b_dir = get_backup_dir()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    db_info = get_current_db_info()

    if db_info['engine'] == 'postgresql':
        pg_dump_exe = find_pg_tool('pg_dump')
        if not pg_dump_exe:
            return False, "لم يتم العثور على أداة pg_dump.exe في مسارات PostgreSQL المعتادة.", ""

        filename = f"backup_postgres_{db_info['name']}_{now_str}.sql"
        filepath = os.path.join(b_dir, filename)

        env = os.environ.copy()
        pwd = connection.settings_dict.get('PASSWORD', '')
        if pwd:
            env['PGPASSWORD'] = str(pwd)

        cmd = [
            pg_dump_exe,
            '-h', db_info['host'],
            '-p', db_info['port'],
            '-U', db_info['user'],
            '-d', db_info['name'],
            '--clean',
            '--if-exists',
            '--no-owner',
            '-f', filepath
        ]

        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                err = res.stderr or "حدث خطأ أثناء أخذ النسخة الاحتياطية."
                return False, f"فشل pg_dump: {err}", ""
            return True, filename, filepath
        except Exception as e:
            return False, f"خطأ غير متوقع: {str(e)}", ""

    else:
        # SQLite
        import sqlite3
        filename = f"backup_sqlite_{now_str}.db"
        filepath = os.path.join(b_dir, filename)
        source_db = connection.settings_dict.get('NAME')

        try:
            # Use SQLite backup API for a 100% consistent live snapshot
            src = sqlite3.connect(str(source_db))
            dst = sqlite3.connect(filepath)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            return True, filename, filepath
        except Exception as e:
            return False, f"فشل نسخ SQLite: {str(e)}", ""


def list_backups():
    """
    List all backup files in BACKUP_DIR sorted newest first.
    """
    b_dir = get_backup_dir()
    files = []
    for fname in os.listdir(b_dir):
        fpath = os.path.join(b_dir, fname)
        if os.path.isfile(fpath) and (fname.endswith('.sql') or fname.endswith('.db') or fname.endswith('.dump') or fname.endswith('.sqlite3')):
            stat = os.stat(fpath)
            size_mb = stat.st_size / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB" if size_mb >= 1 else f"{stat.st_size / 1024:.1f} KB"
            created_at = datetime.datetime.fromtimestamp(stat.st_mtime)
            is_pg = 'postgres' in fname or fname.endswith('.sql')
            files.append({
                'filename': fname,
                'filepath': fpath,
                'size_bytes': stat.st_size,
                'size_display': size_str,
                'created_at': created_at,
                'created_at_display': created_at.strftime('%Y-%m-%d %I:%M %p'),
                'engine': 'PostgreSQL' if is_pg else 'SQLite',
            })

    files.sort(key=lambda x: x['created_at'], reverse=True)
    return files


def restore_backup(filepath):
    """
    Restores the database from a backup file (PostgreSQL .sql or SQLite .db).
    Returns (success: bool, message: str)
    """
    if not os.path.exists(filepath):
        return False, "ملف النسخة الاحتياطية غير موجود."

    db_info = get_current_db_info()

    if db_info['engine'] == 'postgresql':
        psql_exe = find_pg_tool('psql')
        if not psql_exe:
            return False, "لم يتم العثور على أداة psql.exe في مسارات PostgreSQL."

        env = os.environ.copy()
        pwd = connection.settings_dict.get('PASSWORD', '')
        if pwd:
            env['PGPASSWORD'] = str(pwd)

        cmd = [
            psql_exe,
            '-h', db_info['host'],
            '-p', db_info['port'],
            '-U', db_info['user'],
            '-d', db_info['name'],
            '-f', filepath
        ]

        try:
            connection.close()
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
            if res.returncode != 0:
                err = res.stderr or "حدث خطأ أثناء استعادة النسخة الاحتياطية."
                return False, f"فشل psql: {err}"
            
            # Apply any pending migrations/indexes on restored schemas
            try:
                from django.core.management import call_command
                call_command('migrate_schemas', interactive=False)
            except Exception:
                pass

            return True, "تمت استعادة قاعدة بيانات PostgreSQL بنجاح وتحديث فهارسها!"
        except Exception as e:
            return False, f"خطأ أثناء الاستعادة: {str(e)}"

    else:
        # SQLite
        current_db = connection.settings_dict.get('NAME')
        try:
            connection.close()
            # Create a safety backup of current db before overwriting
            bak_path = str(current_db) + '.pre_restore_bak'
            if os.path.exists(current_db):
                shutil.copy2(current_db, bak_path)

            shutil.copy2(filepath, current_db)

            # Apply any pending migrations/indexes on restored SQLite db
            try:
                from django.core.management import call_command
                call_command('migrate', interactive=False)
            except Exception:
                pass

            return True, "تمت استعادة قاعدة بيانات SQLite بنجاح وتحديث فهارسها!"
        except Exception as e:
            return False, f"فشل استعادة SQLite: {str(e)}"



def restore_cli():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
    import django
    django.setup()

    print("=" * 60)
    print("      Cashierak ERP - Database Restore Tool")
    print("=" * 60)
    info = get_current_db_info()
    print(f"  Active Database: {info['display_engine']} ({info['name']})")
    print("=" * 60)

    backups = list_backups()
    target = None
    if not backups:
        print("  No backups found in backups/ directory.")
        target = input("\n  Drag and drop your backup file (.sql or .db) here: ").strip().strip('\"').strip('\'')
    else:
        print("\n  Available Backups:")
        for idx, b in enumerate(backups, 1):
            print(f"  [{idx}] {b['filename']} ({b['size_display']} - {b['created_at_display']})")
        print("  [0] Browse custom file path")

        choice = input("\n  Select backup to restore [1..N] or 0: ").strip()
        if choice == '0':
            target = input("  Drag and drop your backup file here: ").strip().strip('\"').strip('\'')
        else:
            try:
                num = int(choice)
                if 1 <= num <= len(backups):
                    target = backups[num - 1]['filepath']
                else:
                    print("  Invalid selection.")
                    return
            except ValueError:
                target = choice.strip('\"').strip('\'')

    if not target or not os.path.exists(target):
        print(f"  [ERROR] File not found: {target}")
        return

    confirm = input(f"\n  [WARNING] Current database will be OVERWRITTEN by:\n  [{os.path.basename(target)}]\n  Are you sure you want to proceed? [y/n]: ").strip().lower()
    if confirm not in ['y', 'yes', 'نعم']:
        print("  Restore cancelled.")
        return

    print("\n  Restoring database now...")
    ok, msg = restore_backup(target)
    if ok:
        print(f"\n  [SUCCESS] {msg}")
    else:
        print(f"\n  [ERROR] {msg}")
    print("=" * 60)


if __name__ == '__main__':
    import sys
    if '--restore' in sys.argv:
        restore_cli()
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
        import django
        django.setup()

        print("=" * 60)
        print("      Cashierak ERP - Database Backup Tool")
        print("=" * 60)
        info = get_current_db_info()
        print(f"  Active Database: {info['display_engine']} ({info['name']})")
        print("  Creating backup...")
        ok, msg, path = create_backup()
        if ok:
            print(f"\n  [SUCCESS] Backup created successfully!")
            print(f"  File : {msg}")
            print(f"  Path : {path}")
            sz = os.path.getsize(path)
            sz_str = f"{sz / (1024*1024):.2f} MB" if sz >= 1024*1024 else f"{sz / 1024:.1f} KB"
            print(f"  Size : {sz_str}")
        else:
            print(f"\n  [ERROR] Failed to create backup: {msg}")
        print("=" * 60)


