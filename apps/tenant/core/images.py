"""
Uploaded images -> light WebP files on disk.

Images are never stored inside the database: an ImageField keeps only the file
path, the bytes live under MEDIA_ROOT. This module makes those files small:

* every NEW upload on a registered field is resized (longest side <= max_side),
  EXIF-rotated and re-encoded as .webp (transparency kept);
* the file a record no longer points to (replaced / cleared / record deleted) is
  removed from disk, unless another row still uses it;
* ``convert_images_webp`` (management command) converts files uploaded before this.

A file Pillow can't read is kept as uploaded (the form validation already
decided it is an image); conversion never blocks a save.
"""
import io
import logging
import os

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save

logger = logging.getLogger(__name__)

# (app_label, model_name, field, max_side_px, quality)
IMAGE_FIELDS = [
    ('inventory', 'Product', 'image', 800, 80),          # POS cards / product page
    ('core', 'SystemSetting', 'company_logo', 600, 90),  # receipts & printed reports
    ('accounting', 'Cheque', 'attachment', 1600, 85),    # must stay readable
]


def to_webp(fileobj, max_side=800, quality=80):
    """Image file/bytes -> ContentFile of a resized WebP. Raises on unreadable input."""
    from PIL import Image, ImageOps
    if hasattr(fileobj, 'seek'):
        fileobj.seek(0)
    img = Image.open(fileobj)
    img.seek(0)                                # animated GIF/WebP -> first frame
    img = ImageOps.exif_transpose(img)         # phone photos come rotated
    has_alpha = img.mode in ('RGBA', 'LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info)
    img = img.convert('RGBA' if has_alpha else 'RGB')
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, 'WEBP', quality=quality, method=6)
    return ContentFile(out.getvalue())


def _webp_name(name):
    base = os.path.splitext(os.path.basename(name or 'image'))[0] or 'image'
    return f'{base}.webp'


def convert_field_file(field_file, max_side, quality):
    """Re-encode a FieldFile in place (new .webp file; returns old name or None)."""
    old_name = field_file.name
    content = to_webp(field_file, max_side, quality)   # raises -> file left untouched
    try:
        field_file.close()   # Windows: an open handle blocks deleting the old file
    except Exception:  # noqa: BLE001
        pass
    field_file.save(_webp_name(old_name), content, save=False)
    return old_name


def _still_used(model, field, name, exclude_pk=None):
    qs = model._default_manager.filter(**{field: name})
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _delete_file_later(storage, name):
    def _rm():
        try:
            if name and storage.exists(name):
                storage.delete(name)
        except Exception:  # noqa: BLE001 - cleanup must never break a request
            logger.warning('Could not delete old image %s', name, exc_info=True)
    transaction.on_commit(_rm)


def _make_handlers(model, field, max_side, quality):
    def before_save(sender, instance, raw=False, **kwargs):
        if raw:
            return
        f = getattr(instance, field)
        # previous file of this row (to clean up after a replace / clear)
        instance._old_image_names = getattr(instance, '_old_image_names', {})
        if instance.pk:
            instance._old_image_names[field] = (
                sender._default_manager.filter(pk=instance.pk).values_list(field, flat=True).first() or '')
        if f and not getattr(f, '_committed', True):   # a fresh upload
            try:
                convert_field_file(f, max_side, quality)
            except Exception:  # noqa: BLE001
                logger.warning('Image %s kept as uploaded (not convertible to WebP)', f.name, exc_info=True)

    def after_save(sender, instance, raw=False, **kwargs):
        if raw:
            return
        old = (getattr(instance, '_old_image_names', {}) or {}).pop(field, '')
        new = getattr(instance, field).name or ''
        if old and old != new and not _still_used(sender, field, old):
            _delete_file_later(getattr(instance, field).storage, old)

    def after_delete(sender, instance, **kwargs):
        f = getattr(instance, field)
        if f and f.name and not _still_used(sender, field, f.name, exclude_pk=instance.pk):
            _delete_file_later(f.storage, f.name)

    return before_save, after_save, after_delete


_connected = False


def connect_image_optimizers():
    global _connected
    if _connected:
        return
    from django.apps import apps
    for app_label, model_name, field, max_side, quality in IMAGE_FIELDS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        before, after, deleted = _make_handlers(model, field, max_side, quality)
        uid = f'webp-{app_label}-{model_name}-{field}'
        pre_save.connect(before, sender=model, weak=False, dispatch_uid=uid + '-pre')
        post_save.connect(after, sender=model, weak=False, dispatch_uid=uid + '-post')
        post_delete.connect(deleted, sender=model, weak=False, dispatch_uid=uid + '-del')
    _connected = True
