"""Second-factor secrets at rest (finding otp-secrets-plaintext, ASVS 6.5.2 and 13.3.1).

django-otp keeps the TOTP seed as hex in otp_totp_totpdevice.key and the backup codes
as plain text in otp_static_statictoken.token. A database read (a backup, a snapshot,
an injection) would hand out working second factors. This module changes what those
two columns hold, without changing django-otp's models:

* TOTP seeds are encrypted with AES-GCM under a key derived from each
  FIELD_ENCRYPTION_KEYS entry (the same keys that protect calendar tokens). The
  stored value is "e1:" plus base64, 67 characters for the usual 20-byte seed, so
  it fits django-otp's 80-character column (a Fernet token would not). Signals
  decrypt the seed when a device loads and encrypt it before each save, so
  django-otp's own code still sees the hex seed.
* Backup codes are stored as "#" plus 15 hex digits of an HMAC-SHA256, keyed the
  same way. Checking a code computes its HMAC under every key, so rotating keys
  keeps old codes working while the old key stays in the list.
"""

import base64
import hashlib
import hmac
import os
from binascii import Error as BinasciiError
from binascii import unhexlify

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SEED_PREFIX = "e1:"
SEED_COLUMN_MAX = 80
CODE_PREFIX = "#"
CODE_HASH_DIGITS = 15  # "#" + 15 hex digits fills the 16-character column
_SEED_AAD = b"otp_totp.totpdevice.key"


def _master_keys():
    keys = [key for key in settings.FIELD_ENCRYPTION_KEYS if key]
    if not keys:
        raise ImproperlyConfigured(
            "Set FIELD_ENCRYPTION_KEYS to store two-step sign-in secrets."
        )
    try:
        return [base64.urlsafe_b64decode(key) for key in keys]
    except (BinasciiError, ValueError) as error:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS must contain valid Fernet keys."
        ) from error


def _derive(master, purpose):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(
        master
    )


def _seed_keys():
    return [_derive(master, b"syncmily otp seed") for master in _master_keys()]


def _code_keys():
    return [_derive(master, b"syncmily backup code") for master in _master_keys()]


# --- TOTP seeds --------------------------------------------------------------------


def is_encrypted_seed(value):
    return isinstance(value, str) and value.startswith(SEED_PREFIX)


def encrypt_seed(hex_seed):
    """The column value for a hex seed (encrypted with the newest key)."""
    nonce = os.urandom(12)
    sealed = AESGCM(_seed_keys()[0]).encrypt(nonce, unhexlify(hex_seed), _SEED_AAD)
    token = base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")
    value = SEED_PREFIX + token
    if len(value) > SEED_COLUMN_MAX:
        raise ValueError("This seed is too long to store encrypted.")
    return value


def decrypt_seed(value):
    """The hex seed for a stored value; any key in the list can open it."""
    raw = base64.urlsafe_b64decode(value[len(SEED_PREFIX) :] + "==")
    nonce, sealed = raw[:12], raw[12:]
    for key in _seed_keys():
        try:
            return AESGCM(key).decrypt(nonce, sealed, _SEED_AAD).hex()
        except InvalidTag:
            continue
    raise ImproperlyConfigured(
        "A two-step sign-in secret can't be decrypted with FIELD_ENCRYPTION_KEYS."
    )


def open_seed(sender, instance, **kwargs):
    """post_init: give django-otp the hex seed of a device loaded from the database."""
    if "key" in instance.get_deferred_fields():
        return
    if is_encrypted_seed(instance.key):
        instance.key = decrypt_seed(instance.key)


def seal_seed(sender, instance, raw=False, **kwargs):
    """pre_save: write the encrypted seed; post_save puts the hex seed back."""
    if raw or is_encrypted_seed(instance.key):
        return
    instance._plain_key = instance.key
    instance.key = encrypt_seed(instance.key)


def restore_seed(sender, instance, raw=False, **kwargs):
    plain = getattr(instance, "_plain_key", None)
    if plain is not None:
        instance.key = plain
        del instance._plain_key


# --- Backup codes ------------------------------------------------------------------


def is_hashed_code(value):
    return isinstance(value, str) and value.startswith(CODE_PREFIX)


def _code_hash(key, code):
    digest = hmac.new(key, code.strip().lower().encode(), hashlib.sha256).hexdigest()
    return CODE_PREFIX + digest[:CODE_HASH_DIGITS]


def hash_code(code):
    """The stored form of a backup code (under the newest key)."""
    return _code_hash(_code_keys()[0], code)


def code_hashes(code):
    """Every stored form a typed code can have, one per key."""
    return [_code_hash(key, code) for key in _code_keys()]


def hash_token_before_save(sender, instance, raw=False, **kwargs):
    """pre_save on StaticToken: never let a plain backup code reach the database."""
    if raw or is_hashed_code(instance.token):
        return
    instance.token = hash_code(instance.token)
