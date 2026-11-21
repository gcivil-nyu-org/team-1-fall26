"""List usernames that break the username rules (hardening spec 3.2 and 9.1).

Existing accounts keep their names until their next rename, so this only reports:
names outside [A-Za-z0-9._-]{3,30} after NFKC, reserved names people chose, and
names that differ only in letter case (those block the unique index in migration
accounts 0012). Usernames are printed as escaped ASCII, so a name cannot change the
terminal.
"""

from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from accounts.services import USERNAME_PREFIXES
from syncmily.safety import USERNAME_PATTERN, is_reserved_username, normalize_username


def _generated(name):
    """True for the invite flow's own names (Parent2, Kid3), which are allowed."""
    return any(
        name.startswith(prefix) and name[len(prefix) :].isdigit()
        for prefix, _start in USERNAME_PREFIXES.values()
    )


class Command(BaseCommand):
    help = "List usernames that break the username rules, for the owner to review."

    def handle(self, *args, **options):
        by_lower = defaultdict(list)
        charset = reserved = 0
        users = get_user_model().objects.order_by("pk").values_list("pk", "username")
        for pk, username in users.iterator():
            name = normalize_username(username)
            by_lower[username.lower()].append(pk)
            if not USERNAME_PATTERN.fullmatch(name):
                charset += 1
                self.stdout.write(f"charset   user {pk}: {ascii(username)}")
            elif is_reserved_username(name) and not _generated(name):
                reserved += 1
                self.stdout.write(f"reserved  user {pk}: {ascii(username)}")
        clashes = {name: pks for name, pks in by_lower.items() if len(pks) > 1}
        for name, pks in sorted(clashes.items()):
            self.stdout.write(f"case      users {pks}: {ascii(name)}")
        self.stdout.write(
            f"{charset} outside the charset, {reserved} reserved, "
            f"{len(clashes)} case-only clashes."
        )
