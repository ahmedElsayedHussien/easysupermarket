"""
Per-tenant database backup / restore.

Security model
--------------
* Every tenant has its own folder: BACKUP_DIR/<schema_name>/ (SQLite mode:
  BACKUP_DIR/default/). Views only ever list / download / delete / restore
  inside the current tenant's folder.
* PostgreSQL backups dump ONLY the current tenant schema (``pg_dump -n``), so
  they never contain other tenants' data or the shared auth_user table.
* Every backup the server creates gets an HMAC-SHA256 sidecar (``<file>.sig``)
  binding the file hash to the schema and engine. The key is derived from
  settings.SECRET_KEY. Restore refuses any file without a valid signature for
  the current schema, so a user can never feed arbitrary SQL to psql.
* Downloads are a ``.zip`` bundle (dump + .sig); only such bundles can be
  uploaded back, and the signature inside is verified before anything runs.
* Defence in depth: psql runs with ON_ERROR_STOP, in a single transaction,
  without psqlrc, and SQL files containing psql backslash meta-commands are
  rejected.
* Errors shown to users are generic; details go to the logger.
"""
import os
import re
import glob
import hmac
import json
import shutil
import hashlib
import logging
import zipfile
import tempfile
import subprocess
import datetime

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

SIG_VERSION = 1
BACKUP_EXTENSIONS = ('.sql', '.db')
BACKUP_NAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+\.(sql|db)$')
SCHEMA_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
# Max size of an uploaded .zip bundle and of the extracted dump.
MAX_UPLOAD_BYTES = getattr(settings, 'BACKUP_MAX_UPLOAD_BYTES', 512 * 1024 * 1024)
MAX_EXTRACTED_BYTES = getattr(settings, 'BACKUP_MAX_EXTRACTED_BYTES', 4 * 1024 * 1024 * 1024)

GENERIC_ERROR = 'تعذر إتمام العملية. راجع سجل الخادم للتفاصيل أو تواصل مع الدعم الفني.'


class BackupError(Exception):
    """Error with a message that is safe to show to the user."""


# ---------------------------------------------------------------------------
# Tenant / paths
# ---------------------------------------------------------------------------

def is_postgres():
    return 'postgresql' in connection.settings_dict.get('ENGINE', '')


def current_schema_name():
    """Schema of the current tenant (django-tenants) or 'default' (SQLite)."""
    schema = getattr(connection, 'schema_name', None)
    if not schema:
        return 'default'
    if not SCHEMA_NAME_RE.match(schema):
        raise BackupError('اسم مساحة العمل غير صالح.')
    return schema


def get_backup_dir():
    """The current tenant's private backup folder."""
    path = os.path.join(BACKUP_DIR, current_schema_name())
    os.makedirs(path, exist_ok=True)
    return path


def resolve_backup_path(filename):
    """Map a user-supplied filename to a file inside the tenant folder, or raise."""
    safe_name = os.path.basename(str(filename or ''))
    if safe_name != filename or not BACKUP_NAME_RE.match(safe_name):
        raise BackupError('اسم ملف النسخة الاحتياطية غير صالح.')
    b_dir = get_backup_dir()
    path = os.path.join(b_dir, safe_name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(b_dir):
        raise BackupError('اسم ملف النسخة الاحتياطية غير صالح.')
    if not os.path.isfile(path):
        raise BackupError('ملف النسخة الاحتياطية غير موجود.')
    return path


def find_pg_tool(tool_name='pg_dump'):
    """Search for pg_dump / psql on PATH or the standard Windows install dirs."""
    found = shutil.which(tool_name)
    if found:
        return found
    patterns = [
        rf'C:\Program Files\PostgreSQL\*\bin\{tool_name}.exe',
        rf'C:\Program Files (x86)\PostgreSQL\*\bin\{tool_name}.exe',
        rf'D:\Program Files\PostgreSQL\*\bin\{tool_name}.exe',
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0]
    return None


def get_current_db_info():
    """Returns engine type ('postgresql' or 'sqlite'), db_name, host, port, user."""
    if is_postgres():
        return {
            'engine': 'postgresql',
            'display_engine': 'PostgreSQL',
            'name': connection.settings_dict.get('NAME', ''),
            'user': connection.settings_dict.get('USER', ''),
            'host': connection.settings_dict.get('HOST', '') or 'localhost',
            'port': str(connection.settings_dict.get('PORT', '') or '5432'),
            'schema': current_schema_name(),
        }
    db_path = connection.settings_dict.get('NAME')
    size_bytes = os.path.getsize(db_path) if db_path and os.path.exists(str(db_path)) else 0
    return {
        'engine': 'sqlite',
        'display_engine': 'SQLite',
        'name': os.path.basename(str(db_path)),
        'path': str(db_path),
        'size': size_bytes,
        'schema': current_schema_name(),
    }


def _pg_env():
    env = os.environ.copy()
    pwd = connection.settings_dict.get('PASSWORD', '')
    if pwd:
        env['PGPASSWORD'] = str(pwd)
    return env


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def _signing_key():
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        b'easysupermarket.backup.signature.v1',
        hashlib.sha256,
    ).digest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _sig_message(schema, engine, filename, digest):
    return f'v{SIG_VERSION}\n{schema}\n{engine}\n{filename}\n{digest}'.encode('utf-8')


def _sig_path(path):
    return path + '.sig'


def write_signature(path, schema, engine):
    digest = _sha256_file(path)
    filename = os.path.basename(path)
    mac = hmac.new(_signing_key(), _sig_message(schema, engine, filename, digest), hashlib.sha256).hexdigest()
    data = {'v': SIG_VERSION, 'schema': schema, 'engine': engine,
            'filename': filename, 'sha256': digest, 'hmac': mac}
    with open(_sig_path(path), 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    return data


def _parse_signature(raw):
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('v') != SIG_VERSION:
            return None
        for key in ('schema', 'engine', 'filename', 'sha256', 'hmac'):
            if not isinstance(data.get(key), str):
                return None
        return data
    except (ValueError, TypeError):
        return None


def verify_signature(path, sig_data=None):
    """True if ``path`` was produced by this server for the CURRENT schema/engine."""
    if sig_data is None:
        sig_file = _sig_path(path)
        if not os.path.isfile(sig_file):
            return False
        try:
            with open(sig_file, 'r', encoding='utf-8') as fh:
                sig_data = _parse_signature(fh.read(4096))
        except OSError:
            return False
    if not sig_data:
        return False
    schema = current_schema_name()
    engine = 'postgresql' if is_postgres() else 'sqlite'
    if sig_data['schema'] != schema or sig_data['engine'] != engine:
        return False
    if sig_data['filename'] != os.path.basename(path):
        return False
    digest = _sha256_file(path)
    if not hmac.compare_digest(digest, sig_data['sha256']):
        return False
    expected = hmac.new(_signing_key(), _sig_message(schema, engine, sig_data['filename'], digest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_data['hmac'])


# ---------------------------------------------------------------------------
# Create / list / delete
# ---------------------------------------------------------------------------

def create_backup():
    """
    Creates a signed backup of the current tenant in its folder.
    Returns (success: bool, filename_or_error: str, filepath: str)
    """
    try:
        b_dir = get_backup_dir()
        schema = current_schema_name()
    except BackupError as e:
        return False, str(e), ''
    now_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

    if is_postgres():
        pg_dump_exe = find_pg_tool('pg_dump')
        if not pg_dump_exe:
            return False, 'لم يتم العثور على أداة pg_dump في مسارات PostgreSQL المعتادة.', ''
        db = get_current_db_info()
        filename = f'backup_{schema}_{now_str}.sql'
        filepath = os.path.join(b_dir, filename)
        cmd = [
            pg_dump_exe,
            '-h', db['host'], '-p', db['port'], '-U', db['user'], '-d', db['name'],
            '-n', schema,          # ONLY this tenant's schema
            '--clean', '--if-exists',
            '--no-owner', '--no-privileges',
            '--encoding=UTF8',
            '-f', filepath,
        ]
        try:
            res = subprocess.run(cmd, env=_pg_env(), capture_output=True, text=True, timeout=600)
        except Exception:
            logger.exception('pg_dump failed to run for schema %s', schema)
            _safe_remove(filepath)
            return False, GENERIC_ERROR, ''
        if res.returncode != 0:
            logger.error('pg_dump failed for schema %s (rc=%s): %s', schema, res.returncode, res.stderr)
            _safe_remove(filepath)
            return False, GENERIC_ERROR, ''
        write_signature(filepath, schema, 'postgresql')
        return True, filename, filepath

    # SQLite (single-instance mode): the whole file is the tenant.
    import sqlite3
    filename = f'backup_sqlite_{now_str}.db'
    filepath = os.path.join(b_dir, filename)
    source_db = connection.settings_dict.get('NAME')
    try:
        src = sqlite3.connect(str(source_db))
        dst = sqlite3.connect(filepath)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception:
        logger.exception('SQLite backup failed')
        _safe_remove(filepath)
        return False, GENERIC_ERROR, ''
    write_signature(filepath, schema, 'sqlite')
    return True, filename, filepath


def _safe_remove(path):
    for p in (path, _sig_path(path)):
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except OSError:
            logger.warning('Could not remove %s', p)


def list_backups():
    """List the current tenant's backup files, newest first."""
    b_dir = get_backup_dir()
    files = []
    for fname in os.listdir(b_dir):
        fpath = os.path.join(b_dir, fname)
        if not (os.path.isfile(fpath) and BACKUP_NAME_RE.match(fname)):
            continue
        stat = os.stat(fpath)
        size_mb = stat.st_size / (1024 * 1024)
        size_str = f'{size_mb:.2f} MB' if size_mb >= 1 else f'{stat.st_size / 1024:.1f} KB'
        created_at = datetime.datetime.fromtimestamp(stat.st_mtime)
        files.append({
            'filename': fname,
            'filepath': fpath,
            'size_bytes': stat.st_size,
            'size_display': size_str,
            'created_at': created_at,
            'created_at_display': created_at.strftime('%Y-%m-%d %I:%M %p'),
            'engine': 'PostgreSQL' if fname.endswith('.sql') else 'SQLite',
            'signed': os.path.isfile(_sig_path(fpath)),
        })
    files.sort(key=lambda x: x['created_at'], reverse=True)
    return files


def delete_backup(filename):
    path = resolve_backup_path(filename)
    _safe_remove(path)
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Download bundle / upload
# ---------------------------------------------------------------------------

def build_download_bundle(filename):
    """
    Returns (tmp_zip_path, download_name). The zip holds the dump and its .sig.
    Caller is responsible for deleting tmp_zip_path.
    """
    path = resolve_backup_path(filename)
    sig = _sig_path(path)
    if not os.path.isfile(sig):
        raise BackupError('هذه النسخة غير موقّعة من النظام ولا يمكن تنزيلها كحزمة قابلة للاستعادة.')
    fd, tmp = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    base = os.path.basename(path)
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=base)
        zf.write(sig, arcname=base + '.sig')
    return tmp, base + '.zip'


def import_uploaded_bundle(uploaded_file):
    """
    Validate an uploaded .zip bundle (dump + .sig produced by this server for
    this schema) and store it in the tenant folder. Returns the stored path.
    """
    name = os.path.basename(getattr(uploaded_file, 'name', '') or '')
    if not name.lower().endswith('.zip'):
        raise BackupError('يجب رفع ملف ‎.zip‎ تم تنزيله من صفحة النسخ الاحتياطي بهذا النظام.')
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        raise BackupError(f'حجم الملف يتجاوز الحد المسموح ({MAX_UPLOAD_BYTES // (1024 * 1024)} MB).')

    b_dir = get_backup_dir()
    fd, tmp_zip = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    tmp_dump = None
    try:
        with open(tmp_zip, 'wb') as dst:
            for chunk in uploaded_file.chunks():
                dst.write(chunk)
        try:
            zf = zipfile.ZipFile(tmp_zip)
        except zipfile.BadZipFile:
            raise BackupError('الملف المرفوع ليس حزمة نسخ احتياطي صالحة.')
        with zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            dumps = [i for i in members if BACKUP_NAME_RE.match(i.filename)]
            if len(members) != 2 or len(dumps) != 1:
                raise BackupError('الملف المرفوع ليس حزمة نسخ احتياطي صالحة.')
            dump_info = dumps[0]
            sig_name = dump_info.filename + '.sig'
            try:
                sig_info = zf.getinfo(sig_name)
            except KeyError:
                raise BackupError('الحزمة لا تحتوي على توقيع صالح.')
            if sig_info.file_size > 4096 or dump_info.file_size > MAX_EXTRACTED_BYTES:
                raise BackupError('الملف المرفوع ليس حزمة نسخ احتياطي صالحة.')
            sig_data = _parse_signature(zf.read(sig_info).decode('utf-8', 'replace'))
            if not sig_data:
                raise BackupError('الحزمة لا تحتوي على توقيع صالح.')

            final_path = os.path.join(b_dir, dump_info.filename)
            if os.path.exists(final_path):
                # Same file already present: just verify that one.
                if verify_signature(final_path):
                    return final_path
                raise BackupError('يوجد ملف بنفس الاسم بالفعل.')

            fd, tmp_dump = tempfile.mkstemp(dir=b_dir, suffix='.part')
            os.close(fd)
            written = 0
            with zf.open(dump_info) as src, open(tmp_dump, 'wb') as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b''):
                    written += len(chunk)
                    if written > MAX_EXTRACTED_BYTES:
                        raise BackupError('حجم النسخة الاحتياطية يتجاوز الحد المسموح.')
                    dst.write(chunk)

        # Verify against the *final* filename (it is part of the signed message).
        os.replace(tmp_dump, final_path)
        tmp_dump = None
        if not verify_signature(final_path, sig_data):
            _safe_remove(final_path)
            raise BackupError('توقيع النسخة الاحتياطية غير صالح أو أنها تخص مساحة عمل أخرى. لا يمكن استعادتها.')
        with open(_sig_path(final_path), 'w', encoding='utf-8') as fh:
            json.dump(sig_data, fh)
        return final_path
    finally:
        for p in (tmp_zip, tmp_dump):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

_ALLOWED_META = ('\\restrict', '\\unrestrict')  # emitted by recent pg_dump versions
_COPY_START_RE = re.compile(r'^COPY\s.+\sFROM\s+stdin;\s*$', re.IGNORECASE)


def _check_sql_file(path, schema):
    """Reject psql backslash meta-commands (except pg_dump's \\restrict) outside COPY data."""
    in_copy = False
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.rstrip('\r\n')
            if in_copy:
                if stripped == '\\.':
                    in_copy = False
                continue
            if _COPY_START_RE.match(stripped):
                in_copy = True
                continue
            lead = stripped.lstrip()
            if lead.startswith('\\'):
                if lead.split()[0] in _ALLOWED_META:
                    continue
                logger.error('Backup %s rejected: psql meta-command at line %d', path, lineno)
                return False
            if '\\!' in stripped:
                logger.error('Backup %s rejected: shell escape at line %d', path, lineno)
                return False
    return True


def restore_backup(filepath):
    """
    Restores the current tenant from a server-signed backup file.
    Returns (success: bool, message: str)
    """
    try:
        schema = current_schema_name()
        b_dir = os.path.abspath(get_backup_dir())
    except BackupError as e:
        return False, str(e)
    if not os.path.isfile(filepath) or os.path.dirname(os.path.abspath(filepath)) != b_dir:
        return False, 'ملف النسخة الاحتياطية غير موجود.'
    if not verify_signature(filepath):
        logger.warning('Restore refused: invalid/missing signature for %s (schema %s)', filepath, schema)
        return False, 'توقيع النسخة الاحتياطية غير صالح أو مفقود؛ لا يمكن استعادة إلا النسخ التي أنشأها النظام لهذه المساحة.'

    if is_postgres():
        if not filepath.endswith('.sql'):
            return False, 'نوع ملف النسخة الاحتياطية غير مناسب لقاعدة PostgreSQL.'
        if schema == 'public':
            return False, 'لا يمكن استعادة المخطط العام من الواجهة.'
        psql_exe = find_pg_tool('psql')
        if not psql_exe:
            return False, 'لم يتم العثور على أداة psql في مسارات PostgreSQL.'
        if not _check_sql_file(filepath, schema):
            return False, 'محتوى ملف النسخة الاحتياطية غير مسموح به.'
        db = get_current_db_info()
        cmd = [
            psql_exe,
            '-X',                       # ignore psqlrc
            '-q',
            '-v', 'ON_ERROR_STOP=1',
            '--single-transaction',
            '-h', db['host'], '-p', db['port'], '-U', db['user'], '-d', db['name'],
            '-f', filepath,
        ]
        try:
            connection.close()
            res = subprocess.run(cmd, env=_pg_env(), capture_output=True, text=True, timeout=1800)
        except Exception:
            logger.exception('psql restore failed to run for schema %s', schema)
            return False, GENERIC_ERROR
        if res.returncode != 0:
            logger.error('psql restore failed for schema %s (rc=%s): %s', schema, res.returncode, res.stderr)
            return False, GENERIC_ERROR
        try:
            from django.core.management import call_command
            call_command('migrate_schemas', schema_name=schema, interactive=False, verbosity=0)
        except Exception:
            logger.exception('migrate_schemas after restore failed for schema %s', schema)
        return True, 'تمت استعادة بيانات مساحة العمل بنجاح.'

    # SQLite
    if not filepath.endswith('.db'):
        return False, 'نوع ملف النسخة الاحتياطية غير مناسب لقاعدة SQLite.'
    with open(filepath, 'rb') as fh:
        if fh.read(16) != b'SQLite format 3\x00':
            return False, 'ملف النسخة الاحتياطية تالف.'
    current_db = connection.settings_dict.get('NAME')
    try:
        connection.close()
        bak_path = str(current_db) + '.pre_restore_bak'
        if os.path.exists(str(current_db)):
            shutil.copy2(current_db, bak_path)
        shutil.copy2(filepath, current_db)
    except Exception:
        logger.exception('SQLite restore failed')
        return False, GENERIC_ERROR
    try:
        from django.core.management import call_command
        call_command('migrate', interactive=False, verbosity=0)
    except Exception:
        logger.exception('migrate after SQLite restore failed')
    return True, 'تمت استعادة قاعدة بيانات SQLite بنجاح.'


# ---------------------------------------------------------------------------
# CLI (server operators only)
# ---------------------------------------------------------------------------

def _cli_setup():
    import sys
    sys.path.insert(0, BASE_DIR)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
    import django
    django.setup()


def _cli_schema_context(argv):
    """`--schema=<name>` selects the tenant in django-tenants mode."""
    import contextlib
    schema = None
    for arg in argv:
        if arg.startswith('--schema='):
            schema = arg.split('=', 1)[1]
    if schema and hasattr(connection, 'set_schema'):
        from django_tenants.utils import schema_context
        return schema_context(schema)
    return contextlib.nullcontext()


def restore_cli(argv):
    print('=' * 60)
    print('      Cashierak ERP - Database Restore Tool')
    print('=' * 60)
    with _cli_schema_context(argv):
        info = get_current_db_info()
        print(f"  Active Database: {info['display_engine']} ({info['name']}) schema={info['schema']}")
        backups = list_backups()
        if not backups:
            print('  No backups found for this schema.')
            return
        for idx, b in enumerate(backups, 1):
            print(f"  [{idx}] {b['filename']} ({b['size_display']} - {b['created_at_display']})")
        choice = input('\n  Select backup to restore [1..N]: ').strip()
        try:
            target = backups[int(choice) - 1]['filepath']
        except (ValueError, IndexError):
            print('  Invalid selection.')
            return
        confirm = input(f'\n  [WARNING] Current data will be OVERWRITTEN by [{os.path.basename(target)}]. Proceed? [y/n]: ').strip().lower()
        if confirm not in ['y', 'yes', 'نعم']:
            print('  Restore cancelled.')
            return
        ok, msg = restore_backup(target)
        print(f"\n  [{'SUCCESS' if ok else 'ERROR'}] {msg}")


def backup_cli(argv):
    print('=' * 60)
    print('      Cashierak ERP - Database Backup Tool')
    print('=' * 60)
    with _cli_schema_context(argv):
        info = get_current_db_info()
        print(f"  Active Database: {info['display_engine']} ({info['name']}) schema={info['schema']}")
        ok, msg, path = create_backup()
        if ok:
            print(f'  [SUCCESS] {msg}\n  Path : {path}')
        else:
            print(f'  [ERROR] {msg}')


if __name__ == '__main__':
    import sys
    _cli_setup()
    if '--restore' in sys.argv:
        restore_cli(sys.argv)
    else:
        backup_cli(sys.argv)
