"""Encrypt stored TOTP seeds and hash stored backup codes (finding
otp-secrets-plaintext; see accounts/otp_storage.py).

Rows already in the new form are skipped, so the step can run twice. The reverse
decrypts the seeds again. Backup codes cannot be turned back into plain text: after
a reverse, people with two-step on make new backup codes from their profile.
"""

from django.db import migrations

from accounts import otp_storage


def protect(apps, schema_editor):
    TOTPDevice = apps.get_model("otp_totp", "TOTPDevice")
    StaticToken = apps.get_model("otp_static", "StaticToken")
    for device in TOTPDevice.objects.exclude(key__startswith=otp_storage.SEED_PREFIX):
        TOTPDevice.objects.filter(pk=device.pk).update(
            key=otp_storage.encrypt_seed(device.key)
        )
    for token in StaticToken.objects.exclude(token__startswith=otp_storage.CODE_PREFIX):
        StaticToken.objects.filter(pk=token.pk).update(
            token=otp_storage.hash_code(token.token)
        )


def unprotect(apps, schema_editor):
    TOTPDevice = apps.get_model("otp_totp", "TOTPDevice")
    for device in TOTPDevice.objects.filter(key__startswith=otp_storage.SEED_PREFIX):
        TOTPDevice.objects.filter(pk=device.pk).update(
            key=otp_storage.decrypt_seed(device.key)
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_unique_lower_username_and_email"),
        ("otp_totp", "0003_add_timestamps"),
        ("otp_static", "0003_add_timestamps"),
    ]

    operations = [migrations.RunPython(protect, unprotect)]
