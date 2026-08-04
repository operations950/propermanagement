# `onsite` — on-site visits module (design)

Companion to `CLAUDE.md`. This describes a new Django app for scheduling,
tracking, and verifying on-site work at short-term rentals: turnover cleans,
deep cleans, and quarterly inspections. Written so a fresh session can
implement it without re-deriving the decisions below.

## Why a new app, and why it's not called `cleanings`

A cleaning turned out not to be the unit. Turnover cleans, deep cleans, and
quarterly inspections are the same shape: **one person, physically at one
property, working an ordered checklist, required to report back with photos
and a signature.** They differ only in which checklist they get and how they're
scheduled. So the model is `Visit` with a `visit_type`, not `Cleaning`.

This is deliberately *not* built on `processes`. `processes` is a 17-step-type
engine for administrative staff at a computer, with branching and external
parties. On-site work is a flat, ordered, mandatory list completed on a phone
by someone standing in a house. Sharing the engine would mean bending both.
The two systems stay separate and neither constrains the other.

Per `CLAUDE.md`'s rule of thumb — a genuinely separate domain concept with its
own models gets its own app — this is a new app: `onsite`.

## Models

### Booking

Populated by file import (below). The source of truth for "is there a checkout
today, and when is the next check-in."

| Field | Notes |
|---|---|
| `property` | FK, required |
| `source` | `airbnb` / `vrbo` / `manual` |
| `external_uid` | UID from the ICS/CSV row. Unique per `(source, external_uid)`. The idempotency key for re-imports. |
| `guest_name`, `guest_phone_last4` | Often absent from Airbnb ICS exports — nullable |
| `check_in`, `check_out` | Datetimes. Time defaults to the property's configured check-in/check-out times when the source only gives dates (ICS usually does). |
| `status` | `active` / `cancelled` |
| `last_seen_at` | Timestamp of the most recent import that still contained this UID. Drives cancellation detection. |

### Visit

The center of this module. Mirrors `Ticket`'s assignment and token shape
deliberately.

| Field | Notes |
|---|---|
| `property` | FK, required |
| `visit_type` | FK `VisitType` — a real model, not an enum. See below. |
| `booking` | FK nullable — the checkout that generated this visit |
| `next_booking` | FK nullable — the check-in this visit has to beat |
| `scheduled_date`, `scheduled_start` | |
| `ready_by` | Datetime. Defaults to `next_booking.check_in`; overridable. Null means no hard deadline (deep cleans between guests). |
| `assigned_staff` | FK `StaffProfile`, nullable |
| `assigned_contact` | FK `Contact`, nullable |
| `status` | `unassigned` → `scheduled` → `in_progress` → `submitted` → `verified`, plus `cancelled` / `skipped` |
| `started_at`, `submitted_at` | Set by the assignee's own actions, not by staff |
| `access_token`, `token_expires_at` | UUID, per-visit. Same shape as `Ticket.completion_token`. |
| `google_event_id`, `google_sync_pending` | See calendar section |
| `signature_image`, `signed_name`, `signed_at`, `signed_ip` | |
| `notes` | Staff-authored, visible to the assignee |

**Assignment uses the dual-FK pattern verbatim** from `Ticket`
(`assigned_staff` xor `assigned_contact`), with a `CheckConstraint` allowing
*at most* one — because unassigned is a legal, and specifically flagged,
state. This resolves the internal-vs-external cleaner question: internal
cleaners are `StaffProfile`s with `role='cleaner'` and log in to see their day;
external cleaners are `Contact`s and only ever get a token link. Both hit the
same checklist UI.

**One token per visit**, as specified. A cleaner with three properties gets
three links. No per-cleaner day page for external cleaners; internal cleaners
get one in-app.

### VisitType

A model, not an enum, so new kinds of on-site work can be added without a
deploy. Fields: `name`, `slug`, `color`, `default_duration_minutes`,
`is_active`, `requires_deadline` (turnovers must beat a check-in; deep cleans
needn't).

Seeded with turnover / deep clean / inspection. **A `VisitType` cannot be saved
without at least one standard checklist item** — enforced in the create form,
not just by convention. A type without a checklist is a type nobody can
actually perform.

## The checklist model

This is the part most worth getting right, so it's spelled out separately.

**A property's checklist is never stored. It is computed.** The standard list
is the living document; per-property configuration is stored only as
*deviations from it*. Nothing is ever copied down, so nothing ever has to be
pushed — add an item to the standard list and it appears at every property on
the next page load, by construction rather than by a sync job.

Three stored things resolve into one list:

### StandardChecklistItem

The reservoir, per `VisitType`. Fields: `visit_type`, `section`, `order`,
`text`, `mandatory` (default **True**), `requires_photo`, `requires_note`,
`is_active`, and:

- **`required_attributes`** — M2M to the existing `PropertyAttribute` tags.
  An item tagged `has_grill` simply never resolves at properties without that
  attribute. This is what makes a large reservoir workable: the default state
  of an uncurated property is *approximately right* rather than "every task we
  have ever thought of, all blocking." Attribute scoping does the bulk
  filtering; manual hiding handles the genuine oddities.

### PropertyChecklistOverride

Exists only where a property deviates. `property`, `visit_type`,
`standard_item`, plus `is_hidden`, `mandatory_override` (nullable),
`order_override` (nullable), `reviewed_at`.

**Absence of a row means inherit.** Hiding is `is_hidden=True`, never deletion —
the standard item stays visible in the property's checklist editor, greyed, with
a one-click restore. You can always see the full reservoir and what you've
chosen not to use.

### PropertyChecklistItem

Property-specific additions that aren't in the reservoir at all — the gate code
quirk, the one genuinely unique thing. `property`, `visit_type`, `text`,
`mandatory`, `requires_photo`, `is_active`, `order`.

These are not something to minimize. They're the most valuable content in the
system: the thing nobody else would know. They should be **visible and
promotable** — an admin view lists every custom item across all properties, and
when the same one appears at several properties, a promote action lifts it into
the standard list and converts the per-property rows into inheritance. That
turns drift into a signal instead of a mess.

### Resolution, and new-item safety

Resolution order for a `(property, visit_type)` pair:

1. Active `StandardChecklistItem`s for the type, filtered by
   `required_attributes` against the property's tags
2. Minus anything with `is_hidden=True`
3. Plus that property's `PropertyChecklistItem`s
4. Plus one-offs, at visit creation only

An item added to the standard list *after* a property has been reviewed
resolves in as **new and unreviewed** — detected by comparing the item's
`created_at` against the property's `last_reviewed_at`, so there's no state to
backfill and no migration when the reservoir grows.

**Unreviewed items appear on the cleaner's list but do not block submission.**
This matters operationally: without it, adding one item to the standard list at
2pm silently converts every in-flight cleaning that day into a blocked one, and
someone is stuck at 4:15 before a 5pm check-in. Unreviewed items show in the
dashboard's needs-attention zone until a manager keeps or hides them, at which
point they become mandatory like anything else.

### VisitChecklistItem

**The materialized checklist for one visit**, snapshotted from the resolved list
at visit creation. This is the one place copying is correct: the resolved view
changes over time, and a submitted visit's record must never change underneath
it. Once a visit exists, its checklist is frozen except for one-off additions
made before it starts.

`source` is `standard` / `property` / `oneoff`. **One-off items** — "check the
garage this time, last guest complained" — are rows with `source='oneoff'`
added to a single visit, covering the permanent-vs-just-this-once split without
a second model.

Completion fields: `is_completed`, `completed_at`, `note`, `skip_reason`.

**Mandatory enforcement:** submission is blocked server-side until every
`mandatory` item is completed and every `requires_photo` item has at least one
attached `VisitMedia`. The exception case is `skip_reason` — a mandatory item
can be passed only with a written reason, which surfaces in red on the visit
detail page. That keeps "mostly mandatory" honest without stranding a cleaner
in front of a locked room.

### The one thing to watch

A reservoir grows. If it reaches 120 items and a condo hides 60, the cleaner
still faces 60 checkboxes on a phone. There is a length past which people stop
reading and start speed-tapping, and that destroys exactly the data quality
this module exists to produce. Attribute scoping and sections hold it off;
median resolved checklist length is worth putting on the admin screen as a
number someone actually looks at.

### VisitMedia

`visit`, `checklist_item` (nullable — general photos vs. item evidence),
`file`, `media_type`, `caption`, `uploaded_at`.

### VisitIssue

What the cleaner reports that isn't a checklist item: broken AC, stained couch.
On submit, each issue **creates a real `Ticket`** — `property` set,
`assigned_role='maintenance'`, description and photos carried over, FK back to
the visit. This is the bridge between the two systems and the reason repairs
don't need a parallel life here.

This needs one new value in `Ticket.source`: `onsite`. Small migration, but
worth it — attributing these to `manual` would quietly corrupt the only field
that tells you where work comes from.

### VisitRule

Recurring generation for inspections and deep cleans: `property`, `visit_type`,
`interval_months`, `default_assignee`, `last_generated_at`, `is_active`.
Modeled on `TicketTemplate` per `CLAUDE.md`'s instruction to follow the
recurring path, not the dead reactive one.

## Booking import

Staff download the reservation export from Airbnb/VRBO and drop the file on the
dashboard. Parsing is pluggable — `.ics` (calendar export, the common case) and
`.csv` (reservation report, richer) both supported, source auto-detected from
file shape.

The import is **two-phase, never silent**:

1. **Preview.** Parse into an `ImportBatch`, diff against existing `Booking`
   rows by `(source, external_uid)`, and render: N new bookings, N changed
   dates, N disappeared from the feed. Nothing is written to `Booking` yet.
2. **Apply.** Staff confirm. New bookings create `Visit` rows in `unassigned`
   status. Changed dates move the linked visit and re-push the calendar event.
   Bookings absent from the feed are marked `cancelled` and their visits go to
   `cancelled` — which deletes the calendar event.

Cancellation detection compares `last_seen_at` against the batch timestamp, and
only within the date range the file actually covers, so a partial export can't
wipe out next month.

`ImportBatch` retains the raw file and the applied diff. When someone asks why
a cleaning vanished, that's the answer.

## Dashboard

Default view is **today**. Two zones:

**Needs attention**, at the top, `--status-critical`:
- Checkouts today with no visit scheduled
- Visits in `unassigned`
- **At risk** — not `submitted`, and now is past `ready_by` minus a per-visit-type
  buffer. This is the alarm that makes the whole module worth building.

**Today's board**, one row per property with activity today:

| Column | |
|---|---|
| Property | |
| Timeline | checkout time → visit window → check-in time, rendered as a bar |
| Status | derived, see below |
| Assignee | staff or contact, with a resend-link action |
| Ready by | countdown when unmet, actual submit time when met |

Property readiness is **derived, not stored** — a service function over today's
visits and bookings, so it can never drift out of sync with the visits it
summarizes:

- `occupied` — guest in residence, no visit today. Hidden by default.
- `dirty` — checkout passed, visit not started
- `in_progress` — cleaner marked start
- `ready` — visit submitted
- `at_risk` — any of the above that won't make `ready_by`

That directly answers the two questions you actually ask all day: *can this
guest check in early*, and *is anything going to be late.*

Clicking a `ready` property opens the visit detail: submit time, elapsed time,
every photo and video, per-item notes, skip reasons, supply requests,
signature, and any tickets it spawned.

## Cleaner-facing link

`/onsite/v/<token>/`, no login. Same shape as the vendor portal: UUID token +
expiry, and **reuse `vendorportal.models.AccessAttempt.is_rate_limited(ip)`**
rather than building new rate limiting.

Flow: land → property address, access notes, check-in deadline →
**Start** button (sets `started_at`; the checklist doesn't render until then,
which is what makes start-marking real rather than a formality) → ordered
checklist with inline photo capture → issues → supplies → signature pad →
submit. Mobile-first, `base.html`, one page, state saved per item as they go so
a dropped connection doesn't lose an hour of work.

Signature is a canvas pad, stored as a PNG plus typed name, timestamp, and IP.

## Google Calendar

One-directional push to one fixed calendar. Calendar ID goes in
`core/app_settings.py::SECRET_KEYS` as `GOOGLE_ONSITE_CALENDAR_ID`, so it gets
a masked input on `/admin-tools/` for free.

- Create visit → create event, store `google_event_id`
- Edit → patch event
- Cancel → delete event
- Assignee added as **attendee** when an email is on file

Wrapped in the house pattern from `CLAUDE.md`: `is_configured()` guard, broad
`except Exception`, log and set `google_sync_pending=True` rather than raising.
A calendar outage must never block a cleaning from being scheduled. A scheduler
job retries pending pushes.

## Wiring into what exists

- **URLs** — mounted under `/onsite/` in `proptasks/urls.py`, alongside
  `vendorportal`/`supplies`/`processes`, to stay clear of the root-mounted
  route-name collisions `CLAUDE.md` warns about.
- **Scheduler** (`proptasks/scheduler.py::start()`) — three jobs:
  `generate_scheduled_visits` (from `VisitRule`), `sync_onsite_calendar`
  (retry pending pushes), `expire_onsite_tokens`.
- **Procfile** — one idempotent `seed_checklist_templates` command chained into
  the `web:` line, using `get_or_create` on template name.
- **Supplies** — cleaner supply requests feed the existing `supplies` shortage
  report → daily digest flow. No parallel list.
- **UI** — bubble pickers for property, visit type, and assignee; inline-edit
  pencils for property checklist items; existing `--status-*` and `--role-*`
  CSS variables for the board.
- **Permissions** — visible to all staff, consistent with department dashboards
  being non-restrictive. Checklist template editing gated by `_is_admin(user)`.

## Two things to resolve before building

**Media storage is a hard prerequisite.** Railway's filesystem is ephemeral, so
if vendor portal photos currently write to a local `MEDIA_ROOT`, they are being
lost on every deploy and nobody has noticed because nobody goes back and looks.
Cleaning videos will make that failure loud and fast. Check where
`vendorportal` uploads actually land before writing any of this; if it's not
object storage, that's step zero.

**Property check-in/check-out times.** ICS exports usually carry dates, not
times, and `ready_by` is meaningless without a real check-in time. `Property`
needs `default_check_in_time` / `default_check_out_time`, or every deadline in
this module is a guess.
