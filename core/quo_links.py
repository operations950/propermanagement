"""Links that open a call or a text in the Quo phone app (Quo was OpenPhone), from a phone number stored however it was typed.

Quo documents two URL schemes for its mobile app:
  openphone://dial?number=<E.164 number>&action=call          action=call dials at once; `from` (optional) picks the line
  openphone://message?number=<E.164 number>&text=<optional>    `from` and `text` are optional
Quo says these are for mobile apps only (not web pages or the desktop app), so the screens use them on phones, with the plain tel: / sms: link as the fallback
(static/js/quo-links.js).

The number must be E.164 (+15615550100). A number that cannot be made into one, or that carries an extension (Quo cannot dial an extension), gets no Quo link at all and
keeps its ordinary tel: / sms: link.
"""
import re
from urllib.parse import quote

# an extension at the end: "x12", "ext. 12", "extension 12", "#12"
_EXTENSION = re.compile(r'(?:\bext(?:ension)?\.?|\bx|#)\s*\d+\s*$', re.IGNORECASE)


def to_e164(phone):
    """The number in E.164 form (+15615550100), or '' when it is empty, junk, has an extension, or cannot be told apart from another country's number.
    Ten digits (or eleven starting with 1) are taken as US/Canada; anything else must start with + and be 8 to 15 digits long."""
    raw = str(phone or '').strip()
    if not raw or _EXTENSION.search(raw):
        return ''
    if re.search(r'[A-Za-z]', raw):                      # words, a vanity number, "call after 5": not a number we can trust
        return ''
    digits = re.sub(r'\D', '', raw)
    if raw.startswith('+'):
        if digits.startswith('1'):
            return f'+{digits}' if len(digits) == 11 and digits[1] in '23456789' else ''
        return f'+{digits}' if 8 <= len(digits) <= 15 else ''
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in '23456789':
        return f'+1{digits}'
    return ''


def _from_number():
    """The line to send from, when one is set (QUO_FROM_NUMBER); blank means Quo uses whichever line the person last had selected."""
    from django.conf import settings
    return to_e164(getattr(settings, 'QUO_FROM_NUMBER', '') or '')


def _url(kind, number, extra):
    query = [f'number={quote(number, safe="")}'] + extra
    line = _from_number()
    if line:
        query.append(f'from={quote(line, safe="")}')
    return f'openphone://{kind}?' + '&'.join(query)


def dial_url(phone):
    """openphone://dial?... for this number, or '' when it has no Quo form."""
    number = to_e164(phone)
    return _url('dial', number, ['action=call']) if number else ''


def message_url(phone, text=''):
    """openphone://message?... for this number, or '' when it has no Quo form."""
    number = to_e164(phone)
    if not number:
        return ''
    return _url('message', number, [f'text={quote(text, safe="")}'] if text else [])
