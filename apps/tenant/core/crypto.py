"""
Secrets stored in the database (e-invoicing Client Secret, Local Signer token) are encrypted at rest, so a
database backup alone never reveals them.

Key: FIELD_ENCRYPTION_KEY from the environment (.env) when set (a Fernet key), else derived from
SECRET_KEY — the backup files (pg_dump / sqlite copy) don't contain either. If the key changes, the old
values can't be read any more: they come back empty and the user enters them again (logged).

    EncryptedCharField  a CharField that encrypts on save and decrypts on load (old plain values still read).
"""
import base64
import hashlib
import logging

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)
PREFIX = 'enc1$'


def _fernet():
    from cryptography.fernet import Fernet
    key = getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or ''
    if not key:
        import os
        key = os.environ.get('FIELD_ENCRYPTION_KEY', '')
    if not key:
        digest = hashlib.sha256(('field-encryption:' + settings.SECRET_KEY).encode('utf-8')).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(value):
    if not value or str(value).startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(str(value).encode('utf-8')).decode('ascii')


def decrypt(value):
    if not value or not str(value).startswith(PREFIX):
        return value                       # empty, or a value saved before encryption
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(str(value)[len(PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        logger.error('an encrypted setting can no longer be read (the encryption key changed) — enter it again')
        return ''


class EncryptedCharField(models.CharField):
    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        return decrypt(super().to_python(value))

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))
