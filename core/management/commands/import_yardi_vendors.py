"""One-time load of core/fixtures/yardi_vendors.json (296 vendors exported
from Yardi, covering the Comm-STR-LTR and Associations vendor lists) as
ContactImportCandidate rows for the usual review queue — same hard gate as
Quo/Gmail imports, nothing here is a real Contact until a human approves it.

Deliberately NOT wired into the Procfile — it ran once already, and this
fixture is a fixed snapshot, not a moving feed. Running it on every deploy
resurrected every rejected/cleared vendor on the very next release, since
"cleared" or "rejected" looked identical to "never staged" the moment the
old row was gone/excluded. Only ever run this again manually and on
purpose (e.g. a genuinely new Yardi export).

Dedup: by phone or email against existing Contacts and ANY already-known
candidate for that phone/email — pending, approved, OR rejected, since a
rejected candidate is a person staff already looked at and dismissed, not
someone to offer again. Falls back to an exact case-insensitive name match
against other Yardi-sourced candidates/contacts when neither phone nor
email is on file (most rows have neither) — the fixture is a fixed
snapshot, not a moving feed, so this is really about making re-runs safe
rather than catching real-world renames.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import Contact, ContactImportCandidate

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / 'fixtures' / 'yardi_vendors.json'


class Command(BaseCommand):
    help = 'Idempotently stages core/fixtures/yardi_vendors.json as pending ContactImportCandidate rows.'

    def handle(self, *args, **options):
        vendors = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))

        known_phones = set(Contact.objects.exclude(phone='').values_list('phone', flat=True))
        known_phones |= set(ContactImportCandidate.objects.exclude(phone='').values_list('phone', flat=True))
        known_emails = {e.lower() for e in Contact.objects.exclude(email='').values_list('email', flat=True)}
        known_emails |= {
            e.lower() for e in ContactImportCandidate.objects.exclude(email='').values_list('email', flat=True)
        }
        known_yardi_names = {
            n.lower() for n in ContactImportCandidate.objects.filter(source=Contact.Source.YARDI)
            .values_list('name', flat=True)
        }

        created = skipped = 0
        for v in vendors:
            phone, email, name = v['phone'], v['email'].lower(), v['name']
            if phone and phone in known_phones:
                skipped += 1
                continue
            if email and email in known_emails:
                skipped += 1
                continue
            if not phone and not email and name.lower() in known_yardi_names:
                skipped += 1
                continue

            address_bits = ', '.join(filter(None, [v['address'], v['city'], f"{v['state']} {v['zip_code']}".strip()]))
            context = f"Yardi vendor list ({v['category']}) — {address_bits}"
            if v['notes']:
                context += f" — {v['notes']}"

            ContactImportCandidate.objects.create(
                source=Contact.Source.YARDI, name=name, phone=v['phone'], email=v['email'],
                suggested_contact_type=Contact.ContactType.VENDOR,
                raw_context=context,
            )
            if phone:
                known_phones.add(phone)
            if email:
                known_emails.add(email)
            known_yardi_names.add(name.lower())
            created += 1

        self.stdout.write(self.style.SUCCESS(f'Staged {created} Yardi vendor(s), skipped {skipped} already known.'))
