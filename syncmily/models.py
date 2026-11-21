from django.db import models


class RateCounter(models.Model):
    """One rate-limit window: how many hits a key has made until expires_at.

    syncmily.ratelimit.hit counts with a single atomic upsert, so the count is exact
    under concurrency and shared by every process. A nightly job removes expired rows
    (syncmily.ratelimit.purge_expired_counters).
    """

    key = models.CharField(max_length=255, primary_key=True)
    count = models.PositiveIntegerField(default=0, db_default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.key} = {self.count}"
