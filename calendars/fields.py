from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache
def _fernet_for(keys):
    if not keys:
        raise ImproperlyConfigured(
            "Set FIELD_ENCRYPTION_KEYS to store calendar tokens."
        )
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (TypeError, ValueError) as error:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS must contain valid Fernet keys."
        ) from error


def get_fernet():
    """Encrypt with the first key in FIELD_ENCRYPTION_KEYS; decrypt with any of them."""
    return _fernet_for(tuple(settings.FIELD_ENCRYPTION_KEYS))


class EncryptedTextField(models.TextField):
    """A text field stored as Fernet ciphertext. It can't be used in lookups."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return get_fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return get_fernet().decrypt(value.encode()).decode()
        except InvalidToken as error:
            raise ImproperlyConfigured(
                "A stored value can't be decrypted with FIELD_ENCRYPTION_KEYS."
            ) from error
