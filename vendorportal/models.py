from django.db import models
from django.utils import timezone


class AccessAttempt(models.Model):
    """Minimal per-IP hit log so the no-auth completion link can be
    throttled without needing Redis/memcached (see settings.CACHES notes)."""

    ip_address = models.GenericIPAddressField()
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=['ip_address', 'occurred_at'])]

    @classmethod
    def is_rate_limited(cls, ip_address, limit=30, window_minutes=5):
        since = timezone.now() - timezone.timedelta(minutes=window_minutes)
        cls.objects.create(ip_address=ip_address)
        recent_count = cls.objects.filter(ip_address=ip_address, occurred_at__gte=since).count()
        return recent_count > limit
