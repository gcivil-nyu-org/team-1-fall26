from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.signals import (
            user_logged_in,
            user_logged_out,
            user_login_failed,
        )
        from axes.signals import user_locked_out
        from django.db.models.signals import post_init, post_save, pre_save
        from django_otp.plugins.otp_static.models import StaticToken
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from . import checks  # noqa: F401  (registers the deploy checks)
        from . import otp_storage
        from .signals import (
            close_chat_sockets_on_sign_out,
            count_failed_sign_in,
            log_failed_sign_in,
            log_lockout,
            log_sign_in,
            normalize_person_names,
            notice_released_address,
        )

        pre_save.connect(
            normalize_person_names,
            sender=get_user_model(),
            dispatch_uid="accounts.normalize_person_names",
        )
        # Two-step secrets at rest: encrypted seeds, hashed backup codes.
        post_init.connect(
            otp_storage.open_seed,
            sender=TOTPDevice,
            dispatch_uid="accounts.otp_open_seed",
        )
        pre_save.connect(
            otp_storage.seal_seed,
            sender=TOTPDevice,
            dispatch_uid="accounts.otp_seal_seed",
        )
        post_save.connect(
            otp_storage.restore_seed,
            sender=TOTPDevice,
            dispatch_uid="accounts.otp_restore_seed",
        )
        pre_save.connect(
            otp_storage.hash_token_before_save,
            sender=StaticToken,
            dispatch_uid="accounts.otp_hash_token",
        )
        user_login_failed.connect(
            count_failed_sign_in, dispatch_uid="accounts.count_failed_sign_in"
        )
        user_logged_in.connect(
            notice_released_address, dispatch_uid="accounts.notice_released_address"
        )
        user_logged_out.connect(
            close_chat_sockets_on_sign_out,
            dispatch_uid="accounts.close_chat_sockets_on_sign_out",
        )
        user_logged_in.connect(log_sign_in, dispatch_uid="accounts.log_sign_in")
        user_login_failed.connect(
            log_failed_sign_in, dispatch_uid="accounts.log_failed_sign_in"
        )
        user_locked_out.connect(log_lockout, dispatch_uid="accounts.log_lockout")
