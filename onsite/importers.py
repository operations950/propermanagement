"""Booking file parsing for the two-phase import (see onsite/views.py's
booking_import_upload/booking_import_apply).

Two shapes, handled differently:
- **.ics** — a per-listing calendar export (dates only, no guest name, per
  RFC5545's iCalendar format). Airbnb/VRBO only ever hand out one of these
  per listing, so it's inherently single-property — the property is picked
  by staff on the upload form.
- **.csv** — the richer, portfolio-wide "reservation report" export both
  platforms also offer, with a row per reservation across every listing the
  host manages, each row carrying that platform's own listing name/title.
  No property is picked upfront; every row's `listing_name` is resolved
  against stored `core.PropertyListingName` rows by the caller
  (see `onsite/services/bookings.py::resolve_listing_names`).

Format is detected from the file extension; which platform (Airbnb vs VRBO)
is picked by staff on the upload form rather than sniffed from content —
the two exports are structurally near-identical, so guessing would be
unreliable where an explicit bubble is trivial."""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime


class BookingFileError(Exception):
    pass


@dataclass
class RawBooking:
    external_uid: str
    check_in: date
    check_out: date
    guest_name: str = ''
    guest_phone_last4: str = ''
    # Only ever populated from a portfolio-wide .csv row — the platform's
    # own listing name/title, resolved against a Property elsewhere (not
    # here; this module knows nothing about Property). Blank for .ics,
    # which is inherently single-listing.
    listing_name: str = ''
    # When the guest actually MADE the reservation (not the stay dates) —
    # only present when the platform's export includes a "Booked"/"Date
    # booked" column, which not every report does (.ics never has one).
    # Feeds onsite.BookingFeedHealth.newest_booked_date — a leading
    # indicator of booking pace, distinct from how far out the calendar is
    # filled (see that model's docstring). None, not a guess, when absent
    # or unparseable — a soft/optional field is never worth failing the
    # whole import over.
    booked_at: date | None = None
    # True when this row's own Status column says it's a cancellation —
    # e.g. Airbnb's "Canceled by guest"/"Canceled by Airbnb", or VRBO's
    # "Canceled" (VRBO mixes reservations and cancellations in one file;
    # Airbnb ships cancellations as a separate file where every row is
    # cancelled). Detected generically as "the word cancel appears in the
    # Status value" rather than hardcoding each platform's exact wording,
    # so a status the two platforms haven't been observed using yet still
    # gets caught. See onsite/services/bookings.py::diff_bookings for how
    # this is used instead of (or alongside) inferring a cancellation from
    # a booking simply vanishing off a later re-upload.
    is_cancelled: bool = False


def detect_format(filename):
    lower = (filename or '').lower()
    if lower.endswith('.ics'):
        return 'ics'
    if lower.endswith('.csv'):
        return 'csv'
    raise BookingFileError('Unrecognized file type — expected a .ics calendar export or .csv reservation report.')


def _unfold_ics_lines(text):
    # RFC5545 line folding: a continuation line starts with a space or tab
    # and should be joined onto the previous line with that whitespace
    # stripped.
    lines = text.replace('\r\n', '\n').split('\n')
    unfolded = []
    for line in lines:
        if line[:1] in (' ', '\t') and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_date(value):
    value = value.strip()
    if len(value) == 8:  # YYYYMMDD (all-day / VALUE=DATE — the common case)
        return datetime.strptime(value, '%Y%m%d').date()
    # YYYYMMDDTHHMMSS[Z] — a timed event; only the date matters here since
    # Booking's actual check_in/check_out time comes from the property's
    # default_check_in_time/default_check_out_time, not the feed.
    return datetime.strptime(value[:8], '%Y%m%d').date()


def parse_ics(file_bytes):
    text = file_bytes.decode('utf-8', errors='replace')
    lines = _unfold_ics_lines(text)

    bookings = []
    current = None
    for line in lines:
        if line.strip() == 'BEGIN:VEVENT':
            current = {}
        elif line.strip() == 'END:VEVENT':
            if current and current.get('uid') and current.get('dtstart') and current.get('dtend'):
                bookings.append(RawBooking(
                    external_uid=current['uid'],
                    check_in=_parse_ics_date(current['dtstart']),
                    check_out=_parse_ics_date(current['dtend']),
                    guest_name=current.get('summary', ''),
                ))
            current = None
        elif current is not None and ':' in line:
            key, _, value = line.partition(':')
            key = key.split(';')[0].strip().upper()
            if key == 'UID':
                current['uid'] = value.strip()
            elif key == 'DTSTART':
                current['dtstart'] = value
            elif key == 'DTEND':
                current['dtend'] = value
            elif key == 'SUMMARY':
                # Airbnb/VRBO exports rarely put a real guest name here for
                # privacy ("Reserved", "Airbnb (Not available)") — kept
                # as-is since it's still useful context when present.
                current['summary'] = value.strip()

    if not bookings:
        raise BookingFileError('No reservations found in this file — is it a valid calendar export?')
    return bookings


_CSV_FIELD_ALIASES = {
    # 'reservation id' is VRBO's own header; Airbnb calls it 'confirmation code'.
    'external_uid': ['confirmation code', 'confirmation_code', 'reservation id', 'reservation_id', 'uid'],
    # 'check-in date'/'check-out date' (with " date") are VRBO's real headers —
    # distinct from the plain 'check-in'/'check-out' guesses below, which
    # don't match anything VRBO or Airbnb actually ships. 'start date'/
    # 'end date' are Airbnb's.
    'check_in': ['start date', 'start_date', 'check-in date', 'check-in', 'check_in', 'checkin', 'arrival'],
    'check_out': ['end date', 'end_date', 'check-out date', 'check-out', 'check_out', 'checkout', 'departure'],
    # Airbnb's single combined column. VRBO instead splits first/last name
    # into two columns — see guest_first_name/guest_last_name below, which
    # parse_csv falls back to combining when this one isn't found.
    'guest_name': ['guest name', 'guest_name', 'name'],
    'guest_first_name': ['guest first name', 'first name'],
    'guest_last_name': ['guest last name', 'last name'],
    # 'contact' is Airbnb's real header for the phone column; 'guest phone'
    # is VRBO's.
    'guest_phone': ['guest phone', 'phone number', 'phone_number', 'phone', 'contact'],
    # A cancellation reason/marker, when present — read generically (see
    # RawBooking.is_cancelled) rather than hardcoded per platform, so a
    # value neither platform has been observed using yet still gets caught.
    'status': ['status'],
    # Not required — a single-listing CSV export legitimately has no such
    # column. When absent, every row's listing_name stays '' and the whole
    # file is treated as belonging to whichever property staff pick anyway
    # (see onsite/views.py's format branch). 'property name' is VRBO's own
    # header; Airbnb's is just 'listing'.
    'listing_name': ['listing', 'listing name', 'listing_name', 'property', 'property name', 'property_name', 'unit', 'unit name'],
    # Also not required — when reservation date (when the guest booked,
    # not the stay dates) is on the file, populates RawBooking.booked_at
    # for onsite.BookingFeedHealth. 'booked or inquired on' is VRBO's own
    # header; Airbnb's is just 'booked'. Some exports have neither.
    'booked_at': ['booked', 'booked date', 'booked_date', 'date booked', 'booking date', 'reservation date', 'booked or inquired on'],
}


def _find_column(fieldnames, aliases):
    lowered = {f.strip().lower(): f for f in fieldnames}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def _parse_csv_date(value):
    value = value.strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise BookingFileError(f'Could not parse date "{value}" in the CSV.')


def parse_csv(file_bytes):
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BookingFileError('This CSV has no header row.')

    columns = {key: _find_column(reader.fieldnames, aliases) for key, aliases in _CSV_FIELD_ALIASES.items()}
    missing_required = [k for k in ('external_uid', 'check_in', 'check_out') if not columns[k]]
    if missing_required:
        raise BookingFileError(
            'Missing required column(s): ' + ', '.join(missing_required)
            + f'. Found columns: {", ".join(reader.fieldnames)}',
        )

    bookings = []
    for row in reader:
        uid = (row.get(columns['external_uid']) or '').strip()
        if not uid:
            continue
        phone = (row.get(columns['guest_phone']) or '').strip() if columns['guest_phone'] else ''
        digits = re.sub(r'\D', '', phone)
        booked_at = None
        if columns['booked_at']:
            raw_booked = (row.get(columns['booked_at']) or '').strip()
            if raw_booked:
                try:
                    booked_at = _parse_csv_date(raw_booked)
                except BookingFileError:
                    pass  # optional field — an unparseable value just means "unknown," not a failed import

        if columns['guest_name']:
            guest_name = (row.get(columns['guest_name']) or '').strip()
        else:
            # VRBO splits first/last into two columns instead of Airbnb's
            # single one.
            first = (row.get(columns['guest_first_name']) or '').strip() if columns['guest_first_name'] else ''
            last = (row.get(columns['guest_last_name']) or '').strip() if columns['guest_last_name'] else ''
            guest_name = f'{first} {last}'.strip()

        status_value = (row.get(columns['status']) or '').strip() if columns['status'] else ''

        bookings.append(RawBooking(
            external_uid=uid,
            check_in=_parse_csv_date(row[columns['check_in']]),
            check_out=_parse_csv_date(row[columns['check_out']]),
            guest_name=guest_name,
            guest_phone_last4=digits[-4:] if digits else '',
            listing_name=(row.get(columns['listing_name']) or '').strip() if columns['listing_name'] else '',
            booked_at=booked_at,
            is_cancelled='cancel' in status_value.lower(),
        ))

    if not bookings:
        raise BookingFileError('No reservation rows found in this file.')
    return bookings


def parse_booking_file(uploaded_file):
    fmt = detect_format(uploaded_file.name)
    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    if fmt == 'ics':
        return parse_ics(file_bytes)
    return parse_csv(file_bytes)


def read_csv_header(uploaded_file):
    """Peeks at just an uploaded .csv's header row (leaving the file
    position untouched, same as parse_booking_file) so a daily-upload slot
    can check the file's shape against DailyUploadSlot.required_columns
    before anything is actually parsed or saved. Returns None for a non-csv
    file (.ics has no header row to check) or one with no header at all."""
    if detect_format(uploaded_file.name) != 'csv':
        return None
    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames
