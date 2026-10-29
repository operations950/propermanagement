"""Finding the contact we already have for an owner Yardi lists, so the same person is not created twice.

Priority, strictly in this order; the first rule that finds someone decides, and the later rules are not tried:
  1. the same email address: definitely the same person;
  2. the same phone number (the last ten digits, so formatting does not matter): definitely the same person;
  3. only when neither finds anyone, the name: the same name in any order and punctuation ("Smith, Robert" and "Robert Smith"), or a close one (a first name and its
     nickname or initial, a middle initial, Jr or TR left off).
A name match is not trusted when the person we have already has a DIFFERENT email address (that is probably someone else with the same name), unless they are already
tied to this association. When more than one contact could be the person, nobody is guessed: the owner is listed for a person to decide.
Two owners on the same unit are never the same contact, and a vendor is never matched (a vendor stays a vendor). One exception to "the same email is the same person": a couple
who share an email or a phone (the same last name, a different first name) are two people; each is matched by name.
"""
import re
from collections import defaultdict

from core.models import Contact

NO_MATCH_TYPES = {Contact.ContactType.VENDOR}
NAME_ONLY_SKIP = {Contact.ContactType.VENDOR, Contact.ContactType.ON_SITE_STAFF, Contact.ContactType.STAFF_ADJACENT}
SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'tr', 'trust', 'trustee', 'trustees', 'revocable', 'living', 'est', 'estate'}
COMPANY = re.compile(r'\b(llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|company|co|partners|holdings|associates|properties|realty|enterprises|investments|group|lp|llp)\b', re.I)
NICK = {'bob': 'robert', 'rob': 'robert', 'bobby': 'robert', 'bill': 'william', 'billy': 'william', 'will': 'william', 'jim': 'james', 'jimmy': 'james', 'mike': 'michael', 'dick': 'richard',
        'rick': 'richard', 'tom': 'thomas', 'tommy': 'thomas', 'joe': 'joseph', 'dan': 'daniel', 'danny': 'daniel', 'dave': 'david', 'steve': 'steven', 'chris': 'christopher',
        'kate': 'katherine', 'kathy': 'katherine', 'cathy': 'katherine', 'liz': 'elizabeth', 'beth': 'elizabeth', 'peggy': 'margaret', 'meg': 'margaret', 'jen': 'jennifer',
        'jenny': 'jennifer', 'sue': 'susan', 'suzy': 'susan', 'tony': 'anthony', 'ed': 'edward', 'eddie': 'edward', 'ted': 'edward', 'andy': 'andrew', 'matt': 'matthew', 'nick': 'nicholas',
        'sam': 'samuel', 'ben': 'benjamin', 'charlie': 'charles', 'chuck': 'charles', 'larry': 'lawrence', 'johnny': 'john', 'greg': 'gregory', 'jeff': 'jeffrey', 'ken': 'kenneth',
        'pete': 'peter', 'ron': 'ronald', 'don': 'donald', 'fred': 'frederick', 'phil': 'philip', 'tim': 'timothy', 'deb': 'deborah', 'debbie': 'deborah', 'barb': 'barbara', 'pam': 'pamela'}


def emails_of(text):
    return [e.lower() for e in re.findall(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+', text or '')]


def phone10(text):
    d = re.sub(r'\D', '', text or '')
    return d[-10:] if len(d) >= 10 else ''


def _letters(s):
    return re.sub(r'[^a-z]', '', (s or '').lower())


def name_parts(name):
    """(first, last, tokens) of a person's name in "Last, First M" or "First M Last" form, without Jr, TR and the like; first or last is '' when it cannot be told."""
    s = re.sub(r'\([^)]*\)', ' ', (name or '').lower())
    if ',' in s:
        last_part, rest = s.split(',', 1)
        last_tokens = [t for t in re.findall(r"[a-z']+", last_part) if t not in SUFFIXES]
        first_tokens = [t for t in re.findall(r"[a-z']+", rest) if t not in SUFFIXES]
        tokens = first_tokens + last_tokens
        return (first_tokens[0] if first_tokens else ''), (last_tokens[-1] if last_tokens else ''), tokens
    tokens = [t for t in re.findall(r"[a-z']+", s) if t not in SUFFIXES]
    if len(tokens) == 1:
        return '', tokens[0], tokens
    return (tokens[0] if tokens else ''), (tokens[-1] if tokens else ''), tokens


def _first_ok(a, b):
    """The same first name: equal, a nickname of the same name, or one is the other's initial."""
    if not a or not b:
        return False
    a, b = NICK.get(a, a), NICK.get(b, b)
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def same_name(a, b):
    """'exact' (the same words in any order), 'close' (nickname, initial, middle name) or ''."""
    if _letters(a) == _letters(b) and _letters(a):
        return 'exact'
    ta, tb = sorted(name_parts(a)[2]), sorted(name_parts(b)[2])
    if ta and ta == tb:
        return 'exact'
    if COMPANY.search(a or '') or COMPANY.search(b or ''):
        return ''
    fa, la, toka = name_parts(a)
    fb, lb, tokb = name_parts(b)
    if la and la == lb and _first_ok(fa, fb):
        return 'close'
    return ''


class Matcher:
    """Built once per load: an index of every contact we already have by email, phone and last name."""

    def __init__(self, association, overrides=None):
        self.association = association
        self.overrides = {k.lower(): int(v) for k, v in (overrides or {}).items()}
        self.by_email, self.by_phone, self.by_last = defaultdict(list), defaultdict(list), defaultdict(list)
        self.contacts = {}
        self.here = set(Contact.objects.filter(properties=association).values_list('pk', flat=True))
        from .models import OwnerProfile
        alt = dict(OwnerProfile.objects.exclude(phone_alt='').values_list('contact_id', 'phone_alt'))
        for c in Contact.objects.only('id', 'name', 'email', 'phone', 'contact_type').order_by('id'):
            self.contacts[c.pk] = c
            for e in emails_of(c.email):
                self.by_email[e].append(c)
            for p in (phone10(c.phone), phone10(alt.get(c.pk))):
                if p:
                    self.by_phone[p].append(c)
            if c.contact_type not in NAME_ONLY_SKIP:
                self.by_last[name_parts(c.name)[1] or _letters(c.name)].append(c)

    def add(self, c):
        """A contact made during the load, so the same person on another unit finds it instead of being made twice."""
        self.contacts[c.pk] = c
        for e in emails_of(c.email):
            self.by_email[e].append(c)
        if phone10(c.phone):
            self.by_phone[phone10(c.phone)].append(c)
        if c.contact_type not in NAME_ONLY_SKIP:
            self.by_last[name_parts(c.name)[1] or _letters(c.name)].append(c)
        self.here.add(c.pk)

    @staticmethod
    def _household(name, other):
        """Two people who share an email or a phone but are plainly different people: the same last name and a different first name (a couple)."""
        f1, l1, _ = name_parts(name)
        f2, l2, _ = name_parts(other)
        return bool(f1 and f2 and l1 and l1 == l2 and not _first_ok(f1, f2) and not COMPANY.search(name) and not COMPANY.search(other))

    def _pick(self, found, taken, name):
        """Among the contacts that share this email or phone: not one already used on this unit, never a vendor, never a spouse of the same last name and another first name; if
        several are left, the one whose name fits, else one already tied to this association, else the oldest."""
        found = [c for c in found if c.pk not in taken and c.contact_type not in NO_MATCH_TYPES and not self._household(name, c.name)]
        if not found:
            return None
        named = [c for c in found if same_name(name, c.name)]
        return sorted(named or found, key=lambda c: (c.pk not in self.here, c.pk))[0]

    def find(self, row, taken=()):
        """For one owner row {name, email, phone, phone_alt}: (contact or None, reason, candidates). `taken` is the contacts already used by the other owners of this unit.
        `candidates` is a list of (contact, why) to show a person when nobody could be chosen safely."""
        name = row['name']
        if name.lower() in self.overrides and self.overrides[name.lower()] in self.contacts and self.overrides[name.lower()] not in taken:
            return self.contacts[self.overrides[name.lower()]], 'you said so', []
        # 1. the same email
        mine = emails_of(row.get('email'))
        for e in mine:
            c = self._pick(self.by_email.get(e, []), taken, name)
            if c:
                return c, 'same email', []
        # 2. the same phone
        for p in [phone10(row.get('phone')), phone10(row.get('phone_alt'))]:
            if p:
                c = self._pick(self.by_phone.get(p, []), taken, name)
                if c:
                    return c, 'same phone', []
        # 3. only now, the name
        last = name_parts(name)[1] or _letters(name)
        pool = [c for c in self.by_last.get(last, []) if c.pk not in taken]
        found = [(c, same_name(name, c.name)) for c in pool]
        found = [(c, how) for c, how in found if how]
        for how in ('exact', 'close'):
            level = [c for c, h in found if h == how]
            if not level:
                continue
            clash = lambda c: bool(mine) and bool(emails_of(c.email)) and not (set(mine) & set(emails_of(c.email)))
            if len(level) > 1:
                here = [c for c in level if c.pk in self.here]
                if len(here) == 1 and not clash(here[0]):
                    return here[0], 'same name' if how == 'exact' else 'close name', []
                return None, '', [(c, f'{how} name' + (', a different email' if clash(c) else '')) for c in level]
            c = level[0]
            if clash(c) and c.pk not in self.here:
                return None, '', [(c, f'{how} name, but a different email')]
            return c, 'same name' if how == 'exact' else 'close name', []
        # a vendor with this email or phone is not matched, but a person should know
        vend = [c for e in mine for c in self.by_email.get(e, []) if c.contact_type in NO_MATCH_TYPES]
        if vend:
            return None, '', [(vend[0], 'a vendor has this email')]
        return None, '', []
