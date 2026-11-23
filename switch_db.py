# -*- coding: utf-8 -*-
"""
أداة إدارة وتبديل وفحص قاعدة البيانات - Cashierak ERP
تبديل آمن بين PostgreSQL و SQLite مع فحص الاتصال التلقائي وإنشاء الجداول والشركاء تلقائياً.
"""
import os
import sys
import socket
import subprocess

# Set encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from decouple import Config, RepositoryEnv

def get_env_dict():
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return {}
    c = Config(RepositoryEnv(env_path))
    return {
        'DB_ENGINE': c('DB_ENGINE', default='sqlite').strip().lower(),
        'DB_NAME': c('DB_NAME', default='easysupermarket').strip(),
        'DB_USER': c('DB_USER', default='postgres').strip(),
        'DB_PASSWORD': c('DB_PASSWORD', default='thesordd12').strip(),
        'DB_HOST': c('DB_HOST', default='localhost').strip(),
        'DB_PORT': c('DB_PORT', default='5432').strip(),
    }

def update_env_file(key, value):
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(f"{key}={value}\n")
        return

    with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"\n{key}={value}\n")

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)


def test_tcp_port(host, port, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def switch_to_sqlite():
    print("\n" + "="*60)
    print("  Switching to SQLite...")
    print("="*60)
    update_env_file('DB_ENGINE', 'sqlite')
    print("  [OK] Updated .env with DB_ENGINE=sqlite")

    sqlite_path = os.path.join(BASE_DIR, 'db.sqlite3')
    if not os.path.exists(sqlite_path):
        print("  [INFO] Initializing new SQLite database...")
        cmd = [sys.executable, 'manage.py', 'migrate', '--noinput']
        subprocess.run(cmd)

    print("\n  [SUCCESS] Switched successfully to SQLite (db.sqlite3)")
    print("="*60)


def switch_to_postgresql():
    print("\n" + "="*60)
    print("  Switching to PostgreSQL (Testing & Setup)...")
    print("="*60)

    try:
        import psycopg2
    except ImportError:
        print("  [WARNING] psycopg2 is not installed! Installing psycopg2-binary...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'psycopg2-binary'])
        try:
            import psycopg2
        except ImportError:
            print("  [ERROR] Failed to install psycopg2-binary.")
            return False

    env = get_env_dict()
    host = env['DB_HOST']
    port = env['DB_PORT']
    user = env['DB_USER']
    pwd = env['DB_PASSWORD']
    dbname = env['DB_NAME']

    print(f"  [1/6] Testing connection to PostgreSQL server [{host}:{port}]...")
    if not test_tcp_port(host, port):
        print(f"\n  [ERROR] Cannot reach PostgreSQL server at [{host}:{port}]!")
        print("  ------------------------------------------------------------")
        print("  Please check:")
        print("  1. Make sure PostgreSQL is installed on this PC.")
        print("  2. Open Windows Services (services.msc) and ensure 'postgresql-...' is RUNNING.")
        print("  3. Check DB_PORT in .env if you installed on a different port.")
        print("  ------------------------------------------------------------")
        return False
    print("        Port 5432 is OPEN and server is responding.")

    print(f"  [2/6] Authenticating user [{user}]...")
    conn = None
    while True:
        try:
            conn = psycopg2.connect(dbname='postgres', user=user, password=pwd, host=host, port=port, connect_timeout=5)
            print("        Authentication SUCCESSFUL!")
            break
        except psycopg2.OperationalError as e:
            err_msg = str(e)
            if 'password authentication failed' in err_msg or 'fe_sendauth' in err_msg:
                print(f"\n  [ERROR] Password authentication failed for user [{user}]!")
                print(f"  Current password in .env is: '{pwd}'")
                new_pwd = input("  Enter the PostgreSQL password set during installation: ").strip()
                if not new_pwd:
                    print("  Operation cancelled.")
                    return False
                pwd = new_pwd
                update_env_file('DB_PASSWORD', pwd)
            else:
                print(f"\n  [ERROR] Connection error: {err_msg}")
                return False

    print(f"  [3/6] Checking database [{dbname}]...")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (dbname,))
    exists = cur.fetchone()
    if not exists:
        print(f"        Database [{dbname}] does not exist. Creating it now...")
        try:
            cur.execute(f'CREATE DATABASE "{dbname}" WITH ENCODING \'UTF8\';')
            print(f"        [OK] Database [{dbname}] created successfully!")
        except Exception as e:
            print(f"  [ERROR] Failed to create database: {e}")
            conn.close()
            return False
    else:
        print(f"        Database [{dbname}] EXISTS and ready.")
    conn.close()

    print("  [4/6] Updating .env to DB_ENGINE=postgresql...")
    update_env_file('DB_ENGINE', 'postgresql')
    print("        [OK] DB_ENGINE set to postgresql.")

    print("  [5/6] Applying database migrations (migrate_schemas)...")
    os.environ['DB_ENGINE'] = 'postgresql'
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.development'

    cmd_migrate = [sys.executable, 'manage.py', 'migrate_schemas', '--noinput']
    subprocess.run(cmd_migrate)

    print("  [6/6] Verifying Tenant & Domain setup...")
    setup_tenant_script = """
import django
django.setup()
from apps.public.tenants.models import Tenant, Domain

public_tenant, created_pub = Tenant.objects.get_or_create(
    schema_name='public',
    defaults={'name': 'EasySupermarket SaaS'}
)
if created_pub or not Domain.objects.filter(tenant=public_tenant, domain='saas.localhost').exists():
    Domain.objects.get_or_create(domain='saas.localhost', tenant=public_tenant, defaults={'is_primary': True})

main_tenant = Tenant.objects.exclude(schema_name='public').first()
if not main_tenant:
    main_tenant = Tenant.objects.create(schema_name='main', name='Main Supermarket')

for d in ['localhost', '127.0.0.1']:
    if not Domain.objects.filter(domain=d).exists():
        Domain.objects.create(domain=d, tenant=main_tenant, is_primary=(d == 'localhost'))

print('TENANT_READY')
"""
    cmd_tenant = [sys.executable, '-c', setup_tenant_script]
    subprocess.run(cmd_tenant)

    print("\n" + "="*60)
    print("  [SUCCESS] Successfully switched to PostgreSQL!")
    print(f"  Database : {dbname}")
    print(f"  User     : {user}")
    print(f"  Server   : {host}:{port}")
    print("  URL      : http://localhost:8080")
    print("="*60)
    return True


def run_diagnostics():
    print("\n" + "="*60)
    print("             Database Diagnostics Report")
    print("="*60)
    env = get_env_dict()
    curr_engine = env.get('DB_ENGINE', 'sqlite').upper()
    print(f"  Current Active Engine in .env: [ {curr_engine} ]")

    # 1. SQLite Check
    sqlite_file = os.path.join(BASE_DIR, 'db.sqlite3')
    if os.path.exists(sqlite_file):
        sz = os.path.getsize(sqlite_file) / (1024*1024)
        print(f"  SQLite File (db.sqlite3):      EXISTS ({sz:.2f} MB)")
    else:
        print("  SQLite File (db.sqlite3):      NOT FOUND")

    # 2. PostgreSQL Check
    host = env.get('DB_HOST', 'localhost')
    port = env.get('DB_PORT', '5432')
    user = env.get('DB_USER', 'postgres')
    pwd = env.get('DB_PASSWORD', 'thesordd12')
    dbname = env.get('DB_NAME', 'easysupermarket')

    print(f"\n  PostgreSQL Server [{host}:{port}]:")
    port_open = test_tcp_port(host, port)
    if not port_open:
        print(f"  - Port {port}:                  CLOSED / UNREACHABLE")
        print("    --> PostgreSQL service is stopped or not installed.")
    else:
        print(f"  - Port {port}:                  OPEN (Server is active)")
        try:
            import psycopg2
            conn = psycopg2.connect(dbname='postgres', user=user, password=pwd, host=host, port=port, connect_timeout=4)
            print(f"  - User [{user}]:             Authentication SUCCESS")
            cur = conn.cursor()
            cur.execute("SELECT datname FROM pg_database WHERE datname NOT LIKE 'template%';")
            dbs = [r[0] for r in cur.fetchall()]
            print(f"  - Databases on Server:       {', '.join(dbs)}")
            if dbname in dbs:
                print(f"  - Target DB [{dbname}]:       EXISTS and READY")
            else:
                print(f"  - Target DB [{dbname}]:       NOT FOUND (will be created automatically on switch)")
            conn.close()
        except Exception as e:
            print(f"  - Authentication Error:      {e}")

    print("="*60)


def main():
    if '--diag' in sys.argv:
        run_diagnostics()
        return

    while True:
        env = get_env_dict()
        curr = env.get('DB_ENGINE', 'sqlite').upper()
        print("\n" + "="*60)
        print("       Cashierak ERP - Database Switcher & Diagnostics")
        print("="*60)
        print(f"  Current Active Engine: [ {curr} ]")
        print("="*60)
        print("  [1] Switch to SQLite (Local File - db.sqlite3)")
        print("  [2] Switch to PostgreSQL (Auto-Check, Auto-Create & Setup)")
        print("  [3] Run Diagnostics (Test connections & status)")
        print("  [4] Exit")
        print("="*60)

        choice = input("  Select an option [1, 2, 3, or 4]: ").strip()
        if choice == '1':
            switch_to_sqlite()
            break
        elif choice == '2':
            switch_to_postgresql()
            break
        elif choice == '3':
            run_diagnostics()
            input("\n  Press Enter to continue...")
        elif choice == '4':
            print("  Exiting...")
            break
        else:
            print("  Invalid choice, please select 1, 2, 3, or 4.")


if __name__ == '__main__':
    main()
