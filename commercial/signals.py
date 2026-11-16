"""A new commercial property starts with its books switched on (the user's rule: we do the financials for every commercial property)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Property


@receiver(post_save, sender=Property)
def commercial_books_on(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.property_type != Property.Type.COMMERCIAL:
        return
    from accounting.models import AssociationProfile
    profile, made = AssociationProfile.objects.get_or_create(property=instance, defaults={'we_do_financials': True})
    if not made and not profile.we_do_financials:
        profile.we_do_financials = True
        profile.save(update_fields=['we_do_financials'])
