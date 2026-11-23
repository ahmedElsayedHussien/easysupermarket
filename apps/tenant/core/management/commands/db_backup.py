"""
Interactive database backup / restore for server operators.

    python manage.py db_backup                 # backup menu
    python manage.py db_backup --restore       # restore menu
    python manage.py db_backup --schema=main   # skip the menu, one supermarket

Used by backup_database.bat and restore_database.bat.
PostgreSQL menu: one supermarket, all supermarkets, or a FULL database dump
(for moving the whole system to a new machine). SQLite: copies db.sqlite3.
"""
import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Interactive database backup / restore (operators only).'

    def add_arguments(self, parser):
        parser.add_argument('--restore', action='store_true', help='Restore instead of backup')
        parser.add_argument('--schema', type=str, help='Tenant schema (PostgreSQL), skips the menu')

    def handle(self, *args, **options):
        from apps.tenant.core import backup_service

        os.chdir(backup_service.BASE_DIR)
        argv = []
        if options['restore']:
            argv.append('--restore')
        if options.get('schema'):
            argv.append(f"--schema={options['schema']}")
        backup_service.run_cli(argv)
