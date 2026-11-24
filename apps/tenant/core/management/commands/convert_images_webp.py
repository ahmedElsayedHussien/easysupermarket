"""
Convert images uploaded before the WebP optimizer to resized .webp files.

    python manage.py convert_images_webp            # dry run: what would change, sizes
    python manage.py convert_images_webp --apply    # convert, repoint rows, delete old files

With django-tenants it runs on every tenant schema (or only --schema=...).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.tenant.core.images import IMAGE_FIELDS, convert_field_file, to_webp


def _fmt(n):
    return f'{n / 1024:,.0f} KB'


class Command(BaseCommand):
    help = 'Converts stored product / logo / cheque images to resized WebP files.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually convert (default: dry run).')
        parser.add_argument('--schema', default='', help='Only this tenant schema (django_tenants).')

    def handle(self, *args, apply=False, schema='', **opts):
        if 'django_tenants' not in settings.INSTALLED_APPS:
            return self._run(apply, 'default')
        from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context
        public = get_public_schema_name()
        current = getattr(connection, 'schema_name', None)
        if current and current != public and not schema:
            return self._run(apply, current)        # already scoped (tenant_command)
        tenants = get_tenant_model().objects.exclude(schema_name=public)
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f'Unknown schema "{schema}".')
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                self._run(apply, tenant.schema_name)

    def _run(self, apply, label):
        from django.apps import apps
        self.stdout.write(f'[{label}] {"APPLY" if apply else "DRY RUN"}')
        before_total = after_total = done = failed = missing = 0
        for app_label, model_name, field, max_side, quality in IMAGE_FIELDS:
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                continue
            rows = model._default_manager.exclude(**{field: ''}).exclude(**{f'{field}__isnull': True})
            for obj in list(rows):
                f = getattr(obj, field)
                if f.name.lower().endswith('.webp'):
                    continue
                if not f.storage.exists(f.name):
                    missing += 1
                    self.stdout.write(self.style.WARNING(f'  missing file: {f.name} ({model_name} #{obj.pk})'))
                    continue
                try:
                    size_before = f.size
                    if not apply:
                        with f.open('rb') as fh:
                            size_after = len(to_webp(fh, max_side, quality).read())
                        self.stdout.write(f'  {f.name}: {_fmt(size_before)} -> ~{_fmt(size_after)}')
                    else:
                        storage = f.storage
                        with transaction.atomic():
                            old = convert_field_file(f, max_side, quality)
                            # update the column only: no save() side effects / signals
                            model._default_manager.filter(pk=obj.pk).update(**{field: f.name})
                        size_after = f.size
                        if not model._default_manager.filter(**{field: old}).exists():
                            storage.delete(old)
                        self.stdout.write(f'  {old} -> {f.name}: {_fmt(size_before)} -> {_fmt(size_after)}')
                    before_total += size_before
                    after_total += size_after
                    done += 1
                except Exception as e:  # noqa: BLE001 - report and continue
                    failed += 1
                    self.stdout.write(self.style.ERROR(f'  failed {f.name}: {e}'))
        verb = 'converted' if apply else 'would convert'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {done} image(s): {_fmt(before_total)} -> {_fmt(after_total)}'
            f' | failed {failed} | missing files {missing}'))
        if not apply and done:
            self.stdout.write('Run again with --apply to convert.')
