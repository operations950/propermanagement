"""Rules for a property's FAQ, shared by the assistant's API and the staff screen.

  * Secrets stay in one place. A door, lockbox, gate or alarm code, or the wifi
    password, is refused inside a question or answer: it belongs on the property
    record, where changing it updates every reader at once. An FAQ that copied it
    would keep serving the old code after a change.
  * An entry a person has reviewed (or wrote) is locked against the assistant.
  * The same question isn't stored twice: questions are normalised into a key, and
    writing an existing question corrects it in place (if it isn't locked).
"""
import re

from django.db import transaction
from django.utils import timezone

from .models import PropertyFAQ

QUESTION_MAX = 300
ANSWER_MAX = 2000
SOURCE_NOTE_MAX = 200
MIN_SECRET_LEN = 3

# Words that don't change what a question is about, so "Can I bring a dog?" and
# "Do you allow dogs" still look alike to the duplicate check.
_STOP = frozenset('a an the is are was were do does did can could will would may might i you we they he she it my your our their to of in at on for with there here be am this that any'.split())
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


class FAQError(ValueError):
    """A write that can't be accepted. `code` is machine-readable, `status` the
    HTTP status the API answers with, `existing` the entry in the way (if any)."""

    def __init__(self, code, message, status=422, existing=None):
        super().__init__(message)
        self.code, self.status, self.existing = code, status, existing


def clean(text, limit):
    text = _CONTROL.sub('', text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:limit] if len(text) > limit else text


def question_key(question):
    tokens = [t for t in re.findall(r'[a-z0-9]+', question.lower()) if t not in _STOP]
    return ' '.join(tokens)[:320]


def secret_values(prop):
    """Every value on this property (and its units) that must not be copied into
    an FAQ."""
    values = {prop.gate_code, prop.door_code, prop.lockbox_code, prop.alarm_code, prop.wifi_password}
    values |= {u.access_code for u in prop.units.all()}
    return sorted((v.strip() for v in values if v and len(v.strip()) >= MIN_SECRET_LEN), key=len, reverse=True)


def find_secret(prop, *texts):
    """The first secret of this property found in any of `texts` (as a whole
    token, so a 4-digit code doesn't match inside a longer number), or None."""
    haystack = '\n'.join(t for t in texts if t)
    for secret in secret_values(prop):
        if re.search(r'(?<![A-Za-z0-9])' + re.escape(secret) + r'(?![A-Za-z0-9])', haystack, re.IGNORECASE):
            return secret
    return None


def validate(prop, question, answer):
    """(question, answer, key) cleaned, or a FAQError saying what is wrong."""
    question, answer = clean(question, QUESTION_MAX), clean(answer, ANSWER_MAX)
    if not question or not answer:
        raise FAQError('empty', 'Both a question and an answer are needed.')
    key = question_key(question)
    if not key:
        raise FAQError('empty', 'The question has no usable words.')
    if find_secret(prop, question, answer):
        raise FAQError(
            'contains_secret',
            "That text contains one of this property's access codes or its wifi password. Don't copy secrets into an "
            'FAQ (a later change would leave a stale copy): word the answer as "the code is on the property record" '
            'and read it from the access info instead.',
        )
    return question, answer, key


def similar(prop, key, unit=None, exclude_pk=None, limit=5):
    """Active entries whose questions share most of their words with `key`, to
    help the writer notice a near-duplicate."""
    mine = set(key.split())
    if not mine:
        return []
    found = []
    for entry in PropertyFAQ.objects.filter(property=prop, status=PropertyFAQ.Status.ACTIVE).exclude(pk=exclude_pk):
        theirs = set(entry.question_key.split())
        if theirs and len(mine & theirs) / len(mine | theirs) >= 0.5:
            found.append(entry)
    return found[:limit]


def _active(prop, key, unit):
    return PropertyFAQ.objects.filter(property=prop, question_key=key, unit=unit, status=PropertyFAQ.Status.ACTIVE).first()


@transaction.atomic
def bot_write(prop, question, answer, api_key, unit=None, basis=PropertyFAQ.Basis.INFERRED, source_note=''):
    """The assistant adds or corrects an entry. Returns (entry, created). An
    existing entry that a person reviewed or wrote is never overwritten (409)."""
    question, answer, key = validate(prop, question, answer)
    if basis not in (PropertyFAQ.Basis.HOST_REPLY, PropertyFAQ.Basis.PROPERTY_RECORD, PropertyFAQ.Basis.INFERRED):
        basis = PropertyFAQ.Basis.INFERRED
    note = clean(source_note, SOURCE_NOTE_MAX)
    existing = _active(prop, key, unit)
    if existing is not None:
        if existing.locked_against_bot():
            raise FAQError(
                'locked', 'A person has reviewed or written this entry, so it can be used but not overwritten. '
                'If you have reason to think it is wrong, leave it for staff to review.', status=409, existing=existing,
            )
        # The wording of the question stays as first written; only what it says changes.
        existing.answer, existing.basis, existing.source_note = answer, basis, note
        existing.created_by_key = api_key
        existing.save(update_fields=['answer', 'basis', 'source_note', 'created_by_key', 'updated_at'])
        return existing, False
    entry = PropertyFAQ.objects.create(
        property=prop, unit=unit, question=question, question_key=key, answer=answer, origin=PropertyFAQ.Origin.BOT,
        basis=basis, source_note=note, created_by_key=api_key,
    )
    return entry, True


@transaction.atomic
def bot_edit(entry, api_key, question=None, answer=None, basis=None, source_note=None):
    """The assistant corrects one of its own unreviewed entries."""
    if entry.locked_against_bot():
        raise FAQError('locked', 'A person has reviewed or written this entry, so the assistant cannot change it.', status=409, existing=entry)
    q = question if question is not None else entry.question
    a = answer if answer is not None else entry.answer
    q, a, key = validate(entry.property, q, a)
    clash = PropertyFAQ.objects.filter(
        property=entry.property, question_key=key, unit=entry.unit, status=PropertyFAQ.Status.ACTIVE,
    ).exclude(pk=entry.pk).first()
    if clash:
        raise FAQError('duplicate', 'Another entry already answers that question.', status=409, existing=clash)
    entry.question, entry.answer, entry.question_key = q, a, key
    if basis in (PropertyFAQ.Basis.HOST_REPLY, PropertyFAQ.Basis.PROPERTY_RECORD, PropertyFAQ.Basis.INFERRED):
        entry.basis = basis
    if source_note is not None:
        entry.source_note = clean(source_note, SOURCE_NOTE_MAX)
    entry.created_by_key = api_key
    entry.save()
    return entry


def bot_archive(entry):
    if entry.locked_against_bot():
        raise FAQError('locked', 'A person has reviewed or written this entry, so the assistant cannot remove it.', status=409, existing=entry)
    entry.status = PropertyFAQ.Status.ARCHIVED
    entry.save(update_fields=['status', 'updated_at'])
    return entry


def record_use(entry):
    PropertyFAQ.objects.filter(pk=entry.pk).update(times_used=entry.times_used + 1, last_used_at=timezone.now())


# --- staff side --------------------------------------------------------------------

@transaction.atomic
def staff_add(prop, user, question, answer, unit=None):
    question, answer, key = validate(prop, question, answer)
    if _active(prop, key, unit):
        raise FAQError('duplicate', 'That question is already in the FAQ.', status=409, existing=_active(prop, key, unit))
    return PropertyFAQ.objects.create(
        property=prop, unit=unit, question=question, question_key=key, answer=answer, origin=PropertyFAQ.Origin.STAFF,
        basis=PropertyFAQ.Basis.STAFF, reviewed=True, reviewed_by=user, reviewed_at=timezone.now(),
    )


@transaction.atomic
def staff_edit(entry, user, question, answer):
    """Staff correct an entry; doing so counts as reviewing it."""
    question, answer, key = validate(entry.property, question, answer)
    clash = PropertyFAQ.objects.filter(
        property=entry.property, question_key=key, unit=entry.unit, status=PropertyFAQ.Status.ACTIVE,
    ).exclude(pk=entry.pk).first()
    if clash:
        raise FAQError('duplicate', 'Another entry already answers that question.', status=409, existing=clash)
    entry.question, entry.answer, entry.question_key = question, answer, key
    entry.reviewed, entry.reviewed_by, entry.reviewed_at = True, user, timezone.now()
    entry.save()
    return entry


def staff_review(entry, user):
    entry.reviewed, entry.reviewed_by, entry.reviewed_at = True, user, timezone.now()
    entry.save(update_fields=['reviewed', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return entry


def staff_archive(entry):
    entry.status = PropertyFAQ.Status.ARCHIVED
    entry.save(update_fields=['status', 'updated_at'])
    return entry
