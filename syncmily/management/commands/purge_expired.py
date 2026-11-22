from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Delete data past its retention time (spec 4.5): expired rate counters, chat"
        " older than 12 months or over the family cap, stale invites and used or"
        " expired email confirmations. The nightly purge timer runs it."
    )

    def handle(self, *args, **options):
        from accounts.services import purge_email_confirmations, purge_stale_invites
        from chat.services import purge_old_messages
        from syncmily.ratelimit import purge_expired_counters

        counters = purge_expired_counters()
        messages = purge_old_messages()
        invites, accounts = purge_stale_invites()
        expired_links, used_records = purge_email_confirmations()
        self.stdout.write(
            f"Removed {counters} rate counter(s), {messages} chat message(s), "
            f"{invites} stale invite(s) and {accounts} unused invite account(s). "
            f"Cleared {expired_links} expired email link(s) and removed "
            f"{used_records} used email confirmation(s)."
        )
