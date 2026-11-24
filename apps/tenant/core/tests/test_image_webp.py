"""Uploaded images are stored as resized WebP files; orphaned files are removed."""
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from apps.tenant.core.tests.factories import make_category, make_product, seed_chart_of_accounts

TMP_MEDIA = tempfile.mkdtemp(prefix='media-test-')


def image_bytes(size=(2400, 1600), fmt='JPEG', mode='RGB', color=(200, 80, 20)):
    buf = io.BytesIO()
    Image.new(mode, size, color if mode == 'RGB' else (*color, 120)).save(buf, fmt)
    return buf.getvalue()


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ImageWebpTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        seed_chart_of_accounts()
        self.cat = make_category()

    def test_upload_becomes_small_webp(self):
        raw = image_bytes()
        p = make_product(self.cat, 'صورة')
        with self.captureOnCommitCallbacks(execute=True):
            p.image = SimpleUploadedFile('photo.jpg', raw, content_type='image/jpeg')
            p.save()
        p.refresh_from_db()
        self.assertTrue(p.image.name.startswith('products/'))
        self.assertTrue(p.image.name.endswith('.webp'))
        with p.image.open('rb') as fh:
            img = Image.open(fh)
            self.assertEqual(img.format, 'WEBP')
            self.assertEqual(max(img.size), 800)
            self.assertEqual(img.size, (800, 533))       # aspect ratio kept
        self.assertLess(p.image.size, len(raw))

    def test_transparency_is_kept(self):
        p = make_product(self.cat, 'شفاف')
        p.image = SimpleUploadedFile('logo.png', image_bytes((300, 300), 'PNG', 'RGBA'), content_type='image/png')
        p.save()
        with p.image.open('rb') as fh:
            self.assertEqual(Image.open(fh).mode, 'RGBA')

    def test_small_image_not_upscaled(self):
        p = make_product(self.cat, 'صغير')
        p.image = SimpleUploadedFile('s.jpg', image_bytes((300, 200)), content_type='image/jpeg')
        p.save()
        with p.image.open('rb') as fh:
            self.assertEqual(Image.open(fh).size, (300, 200))

    def test_replacing_and_deleting_remove_old_files(self):
        p = make_product(self.cat, 'استبدال')
        with self.captureOnCommitCallbacks(execute=True):
            p.image = SimpleUploadedFile('a.jpg', image_bytes(), content_type='image/jpeg')
            p.save()
        first = p.image.name
        storage = p.image.storage
        self.assertTrue(storage.exists(first))
        with self.captureOnCommitCallbacks(execute=True):
            p.image = SimpleUploadedFile('b.jpg', image_bytes(color=(10, 10, 200)), content_type='image/jpeg')
            p.save()
        self.assertNotEqual(p.image.name, first)
        self.assertFalse(storage.exists(first))
        second = p.image.name
        with self.captureOnCommitCallbacks(execute=True):
            p.delete()
        self.assertFalse(storage.exists(second))

    def test_saving_other_fields_keeps_image(self):
        p = make_product(self.cat, 'ثابت')
        with self.captureOnCommitCallbacks(execute=True):
            p.image = SimpleUploadedFile('a.jpg', image_bytes(), content_type='image/jpeg')
            p.save()
        name = p.image.name
        with self.captureOnCommitCallbacks(execute=True):
            p.name = 'ثابت 2'
            p.save()
        p.refresh_from_db()
        self.assertEqual(p.image.name, name)
        self.assertTrue(p.image.storage.exists(name))

    def test_unreadable_file_is_kept_as_is(self):
        p = make_product(self.cat, 'تالف')
        p.image = SimpleUploadedFile('x.jpg', b'not an image', content_type='image/jpeg')
        p.save()   # never blocks the save
        self.assertTrue(p.image.name.endswith('.jpg'))

    def test_convert_command_for_old_files(self):
        from django.core.files.base import ContentFile
        from apps.tenant.inventory.models import Product
        p = make_product(self.cat, 'قديم')
        storage = p.image.storage
        old_name = storage.save('products/old.jpg', ContentFile(image_bytes()))
        Product.objects.filter(pk=p.pk).update(image=old_name)   # as if uploaded before
        out = io.StringIO()
        call_command('convert_images_webp', stdout=out)          # dry run
        p.refresh_from_db()
        self.assertEqual(p.image.name, old_name)
        self.assertIn('would convert 1', out.getvalue())
        call_command('convert_images_webp', '--apply', stdout=io.StringIO())
        p.refresh_from_db()
        self.assertTrue(p.image.name.endswith('.webp'))
        self.assertFalse(storage.exists(old_name))
        out = io.StringIO()
        call_command('convert_images_webp', stdout=out)          # nothing left
        self.assertIn('would convert 0', out.getvalue())
