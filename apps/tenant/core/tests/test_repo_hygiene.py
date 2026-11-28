"""P3-3: build artifacts (the signing tool's exe, PyInstaller build folder) are never tracked in git."""
import shutil
import subprocess
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)


class RepoHygieneTests(SimpleTestCase):
    def tracked(self):
        if shutil.which('git') is None or not (ROOT / '.git').exists():
            raise unittest.SkipTest('not a git checkout')
        out = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return out.splitlines()

    def test_no_build_artifacts_tracked(self):
        bad = [f for f in self.tracked()
               if f.lower().endswith(('.exe', '.pyz', '.pkg')) or f.startswith(('local_signer/build/', 'release/'))]
        self.assertEqual(bad, [], 'build artifacts must not be in git (see .gitignore)')

    def test_gitignore_covers_them(self):
        rules = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        for rule in ('*.exe', 'local_signer/build/'):
            self.assertIn(rule, rules)
