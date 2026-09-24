"""Idempotent seed for the standard legal forms (legalforms/catalog.py). Runs on every deploy from the Procfile, so it is
purely additive: a template that already exists is left alone — an admin may have edited its wording in Django admin,
and a deploy must never overwrite what counsel has changed. --refresh SLUG deliberately replaces one template's wording
and fields from the catalog (which bumps its version)."""
from django.core.management.base import BaseCommand

from legalforms import catalog
from legalforms.models import DocumentTemplate


class Command(BaseCommand):
    help = 'Adds any standard legal form that is missing (never overwrites an existing one). --refresh SLUG replaces one from the catalog.'

    def add_arguments(self, parser):
        parser.add_argument('--refresh', action='append', default=[], help='Replace this template\'s wording and fields from the catalog.')

    def handle(self, *args, **options):
        by_slug = {}
        for spec in catalog.TEMPLATES:
            companion = by_slug.get(spec.get('companion_of'))
            defaults = {k: v for k, v in spec.items() if k not in ('slug', 'companion_of')}
            defaults['companion_of'] = companion
            obj, created = DocumentTemplate.objects.get_or_create(slug=spec['slug'], defaults=defaults)
            if not created and spec['slug'] in options['refresh']:
                for k, v in defaults.items():
                    setattr(obj, k, v)
                obj.save()
                self.stdout.write(f'Refreshed {obj.name} (version {obj.version})')
            else:
                self.stdout.write(f'{"Created" if created else "Found"} {obj.name}')
            by_slug[spec['slug']] = obj
            if not created and obj.companion_of_id is None and companion is not None:
                obj.companion_of = companion
                obj.save(update_fields=['companion_of'])
        self.stdout.write(self.style.SUCCESS('Legal form templates seeded.'))
