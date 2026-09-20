"""Parses the calendar (.ics) link Airbnb/VRBO give for a listing, as
fetched by onsite/services/feeds.py. Separate from importers.parse_ics —
that one serves the manual file upload and keys bookings by the calendar's
own UID; a live feed needs more care: it has to tell real reservations from
"not available" blocks, recover the platform's confirmation code (so the
same reservation isn't duplicated when a CSV report also brings it in), and
refuse anything that isn't actually a calendar (a revoked link often
answers with an HTML login page and a 200)."""
import re
from dataclasses import dataclass
from datetime import date

from .importers import BookingFileError, _parse_ics_date, _unfold_ics_lines

# Airbnb puts "Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMXXXXXXXX"
# in each reservation's description; that trailing code is the same
# confirmation code its CSV reports use.
_AIRBNB_CODE = re.compile(r'reservations/details/([A-Za-z0-9]+)')
_PHONE_LAST4 = re.compile(r'Phone Number \(Last 4 Digits\):\s*(\d{4})')
# Summaries of calendar entries that are the owner/platform blocking dates,
# not a guest's stay.
_BLOCK_WORDS = re.compile(r'not available|unavailable|blocked|\bblock\b|\bclosed\b|maintenance|\bowner\b|\bhold\b', re.IGNORECASE)


@dataclass
class FeedEvent:
    uid: str
    check_in: date
    check_out: date
    summary: str = ''
    code: str = ''
    phone_last4: str = ''

    @property
    def is_reservation(self):
        """False for date blocks. A confirmation code proves it's a
        reservation whatever the summary says; without one, a summary
        that reads like a block means it isn't."""
        if self.code:
            return True
        return not _BLOCK_WORDS.search(self.summary or '')


def _unescape(value):
    return value.replace('\\n', '\n').replace('\\N', '\n').replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')


def parse_feed_ics(file_bytes):
    """All VEVENTs in the calendar, as FeedEvents (blocks included — callers
    filter on is_reservation). Raises BookingFileError if this isn't a
    calendar at all. A valid calendar with no events returns []."""
    text = file_bytes.decode('utf-8', errors='replace')
    if 'BEGIN:VCALENDAR' not in text:
        raise BookingFileError("That address didn't return a calendar — the link may have expired or been revoked.")
    events, current = [], None
    for line in _unfold_ics_lines(text):
        stripped = line.strip()
        if stripped == 'BEGIN:VEVENT':
            current = {}
        elif stripped == 'END:VEVENT':
            # A STATUS:CANCELLED entry is the platform saying it's gone — same as absent.
            if current and not current.get('cancelled') and current.get('uid') and current.get('dtstart') and current.get('dtend'):
                try:
                    check_in = _parse_ics_date(current['dtstart'])
                    check_out = _parse_ics_date(current['dtend'])
                except ValueError:
                    check_in = check_out = None
                if check_in and check_out:
                    description = current.get('description', '')
                    code_match = _AIRBNB_CODE.search(description)
                    phone_match = _PHONE_LAST4.search(description)
                    events.append(FeedEvent(
                        uid=current['uid'], check_in=check_in, check_out=check_out,
                        summary=current.get('summary', ''),
                        code=code_match.group(1) if code_match else '',
                        phone_last4=phone_match.group(1) if phone_match else '',
                    ))
            current = None
        elif current is not None and ':' in line:
            key, _, value = line.partition(':')
            key = key.split(';')[0].strip().upper()
            if key == 'UID':
                current['uid'] = value.strip()
            elif key in ('DTSTART', 'DTEND'):
                current[key.lower()] = value
            elif key == 'SUMMARY':
                current['summary'] = _unescape(value.strip())
            elif key == 'DESCRIPTION':
                current['description'] = _unescape(value)
            elif key == 'STATUS' and value.strip().upper() == 'CANCELLED':
                current['cancelled'] = True
    return [e for e in events if e]
