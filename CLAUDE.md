# proptasks — architecture reference

This file exists so a fresh Claude session (or a human) can understand this
codebase well enough to design a new module that actually fits how the app
already works, instead of reinventing patterns that already exist here.

## What this app is

A property-management task-tracking webapp for a real business (associations/
HOAs, short-term rentals, long-term rentals, commercial properties). Staff
track maintenance/admin work as `Ticket`s — some created manually, some
generated on a recurring schedule — assign them to a department or a specific
person or an outside contractor, and follow the work through to completion.
It also runs multi-step guided workflows ("Processes"), a vendor-facing
no-login completion portal, a supply-request digest, on-site visit scheduling
for cleaners/inspectors, and a company-wide owner dashboard with QuickBooks
financials and weather.

Deployed on Railway (Postgres in production, SQLite locally), auto-deploys
on push to `main`. Production URL: `propermanagement-production.up.railway.app`.
Demo logins: `staff`/`staff12345` (regular staff), `admin`/`admin12345`
(superuser).

## Stack

Django 6.0.7, server-rendered templates (no SPA, no Node build), Bootstrap 5
+ a small custom design system on top, htmx included but lightly used,
vanilla JS for anything interactive (no framework). APScheduler runs
background jobs in-process (`proptasks/scheduler.py`) — no Celery/Redis.
SQLite locally, Postgres (`DATABASE_URL`) in production via `dj_database_url`.
`gunicorn` + WhiteNoise for static files. Media uploads (`FileField`/
`ImageField`) go to Cloudinary when `CLOUDINARY_CLOUD_NAME`/`_API_KEY`/
`_API_SECRET` are set, else plain local disk (`django.core.files.storage.
FileSystemStorage`) — see "Media storage" below before touching `STORAGES`.

## The 8 Django apps

- **`core`** (`core/models.py` ~615 lines, `core/views.py` ~1550 lines) — the
  hub: `Property`, `Contact`, `StaffProfile`, auth (Google Sign-On + password
  fallback), the Admin Tools screen, external integrations
  (`google_calendar.py`, `quickbooks.py`, `places.py`, `usps.py`), the
  DB-backed secrets system (`app_settings.py`).
- **`tickets`** (`tickets/models.py` ~740 lines, `tickets/views.py` ~2455
  lines — the largest file in the app) — `Ticket`, `TicketTemplate` (recurring
  rules), `TaskPackage`/`PackageRun` ("Functions", a named group of recurring
  steps), dashboards (main, Owner, 6 department sub-dashboards), the ticket
  list/detail/create/reassign/follow-up flows.
- **`processes`** — a generalized 17-step-type workflow engine (`ProcessTemplate`
  → `ProcessRun` → `ProcessRunStep`), a staff-facing builder UI, and a
  no-login external link for outside parties to complete assigned steps.
- **`onsite`** — scheduling and tracking on-site work (turnover cleans, deep
  cleans, inspections) at short-term rentals: booking-file import, a computed
  per-property checklist, a no-login cleaner link, issue→ticket bridging,
  Google Calendar push. See `ONSITE_DESIGN.md` for the full design.
- **`vendorportal`** — the no-login, token-keyed page a vendor/contractor
  uses to mark a ticket done and upload photos. Rate-limited by IP
  (`AccessAttempt` — reused by `onsite`'s cleaner link too).
- **`messaging`** — `send_followup_bulk` and friends: one abstraction for
  "email or SMS to one or more contacts," used by tickets and properties.
- **`supplies`** — shortage reports → daily digest → Amazon-search-link
  order list. Checkout stays manual, nothing auto-purchases.
- **`intake`** — the reactive-ticket-creation machinery. **As of 2026-08-03
  this no longer creates tickets automatically** (see "Intake is off" below)
  but the models/classifiers/webhook receiver are all still here, just
  unwired from the scheduler.

Root URL config (`proptasks/urls.py`) mounts `vendorportal`/`supplies`/
`processes`/`onsite` under their own prefixes and `core`/`intake`/`tickets` at
the root — so route names across those three can collide if you're not
careful; check `tickets/urls.py` and `core/urls.py` before naming a new
route. A new app with its own models should mount under its own prefix.

## Media storage — a real incident, read before touching `STORAGES`

`django-cloudinary-storage==0.3.0`'s own `collectstatic` management command
override **must never be enabled** — its `copy_file` silently no-ops unless
static files are *also* routed through Cloudinary (they aren't; only
media/`default` uses it), which breaks WhiteNoise-based static collection
outright and crash-looped production on 2026-08-04. The fix: `'cloudinary_storage'`
is **not** in `INSTALLED_APPS` — only `'cloudinary'` (the base SDK) is. The
storage backend class (`cloudinary_storage.storage.MediaCloudinaryStorage`)
doesn't need app registration; it's referenced by dotted path in
`STORAGES['default']['BACKEND']` in `settings.py`, conditional on all three
`CLOUDINARY_*` env vars being set. If a future change ever needs
`cloudinary_storage` back in `INSTALLED_APPS` (e.g. to also serve *static*
files from Cloudinary), re-verify `collectstatic` end-to-end first — don't
assume it's safe.

Related: any new model field using `ImageField` (not just `FileField`)
requires `Pillow` — it's in `requirements.txt`, but this was missed once and
crash-looped production (`fields.E210`) until caught, because Pillow was
already present in the local dev venv from an unrelated earlier install and
masked the gap. Always check a genuinely clean install (or at least diff
`requirements.txt` against every third-party import) before assuming
`manage.py check` passing locally means the deploy will succeed.

## Core domain model

```
Property ──< Ticket >── StaffProfile (assigned_staff)
    │            └──── Contact (assigned_contact, "vendor" path)
    │
    ├──< Contact (M2M, "which contacts belong to this property")
    ├──< ProcessRun (also attachable to Ticket or Contact — exactly one, DB-checked)
    ├──< onsite.Visit (assigned_staff xor assigned_contact, DB-checked)
    └──< PropertyDocument, PropertyAttribute (tags), PropertySystemLocation
```

- **`Property`** — has a `property_type` (association/STR/LTR/snowbird/
  commercial/general) and `is_general` (a "not one specific address" bucket
  per type, used when a ticket can't be pinned to a real address). Structured
  address fields (`street`/`city`/`state`/`zip_code`) auto-derive the display
  `address`; `address_verified` comes from USPS if configured.
  `default_check_in_time`/`default_check_out_time` (nullable, STR-relevant)
  feed the `onsite` module's turnover-deadline math.
- **`Contact`** — one flexible model for guests/tenants/owners/board members/
  association members/on-site staff/leads/vendors/staff/other
  (`contact_type`, plus `secondary_types` for e.g. Owner+Board Member at
  once). M2M to `Property`. `source` tracks manual vs. imported.
- **`StaffProfile`** — one per `User`. `role` (admin/property_manager/
  maintenance/cleaner/contractor/accounting) is the **department/queue**
  concept — separate from `User.is_superuser` (Django-admin access) and from
  `is_company_admin` (Owner Dashboard access). Three independent permission
  axes; don't conflate them.
- **`Ticket`** — the center of the app. Key fields: `status` (open →
  assigned/in_progress/blocked/upcoming/vendor_complete → completed/verified,
  or skipped/not_applicable/deferred/cancelled), `source` (manual/recurring/
  onsite/email/quo/calendar/airbnb/vrbo/fake — only `manual`, `recurring`,
  and `onsite` are live now), `assigned_role` (department, required) +
  `assigned_staff` (specific person, optional) + `assigned_contact` (vendor,
  mutually exclusive with `assigned_staff`, enforced by both a `CheckConstraint`
  and form-level `clean()`), `due_date`, `priority`, `delayed` (bool). A
  ticket only shows up in dashboards once it has a `property` — see
  `ticket_form.html`'s required property picker.

## Established patterns worth reusing

- **Dual assignment FK** (`assigned_role` + `assigned_staff`, or
  `assigned_staff` xor `assigned_contact`) — used for `Ticket` assignment,
  copied verbatim (with a real DB `CheckConstraint`, not just form-level
  validation) for `Process` step assignees and `onsite.Visit`. If a new
  module needs "assign to a department, optionally a specific person," reuse
  this shape rather than inventing a new one.
- **Nullable-multi-target FK + CheckConstraint** — `FollowUpLog` (ticket-or-
  property) and `ProcessRun` (ticket-or-property-or-contact) both use several
  nullable FKs plus a `CheckConstraint` requiring exactly one set, instead of
  `GenericForeignKey`/contenttypes (never used anywhere in this codebase,
  despite being installed). Follow this precedent, not contenttypes.
- **Computed-not-copied resolution, materialized once at creation** —
  `onsite`'s checklist model is the reference example: a standard reservoir
  plus per-property overrides/additions is *resolved live* every time
  (`onsite/services/checklist.py::resolve_checklist`), never stored — but
  once a `Visit` is created, the resolved list is snapshotted once into
  `VisitChecklistItem` rows so it can't drift under a record that's already
  in progress or submitted. Worth copying for any "a live template applies
  to many targets, but one instance's history must be frozen" need.
- **External no-login token access** — `Ticket.completion_token` (vendor
  portal), `ProcessRunExternalAccess` (process external link), and
  `onsite.Visit.access_token` (cleaner link) all follow the same shape: a
  UUID token + expiry + `vendorportal.models.AccessAttempt.
  is_rate_limited(ip)` for brute-force protection. Reuse `AccessAttempt` for
  any new no-login surface rather than building new rate-limiting.
- **"Unconfigured = graceful degrade, never crash"** — every external
  integration (`google_calendar.py`, `quickbooks.py`, `places.py`, `usps.py`,
  Quo, Gmail, `onsite/google_calendar_push.py`, Cloudinary storage) has an
  `is_configured()` guard and a broad `except Exception` around the actual
  API call that logs and returns `None`/`[]`/`False` rather than raising. UI
  shows a "Connect X" prompt (or just silently no-ops) instead of erroring.
  New integrations should follow this shape from the start — and be
  verified locally in *both* the configured and unconfigured state, not
  just whichever one you happen to be testing.
- **DB-backed secret overrides** (`core/app_settings.py`) — add a key to
  `SECRET_KEYS` and it automatically gets a masked input on `/admin-tools/`,
  no template changes needed. `apply_overrides()` copies DB values onto
  `django.conf.settings` at boot and after every save. Exception: settings
  baked into a dict/registry at process-boot time (like `STORAGES`, built
  from `CLOUDINARY_*`) only take full effect on a restart even when changed
  via this DB-override path — see the comment on those `SECRET_KEYS` entries.
- **Idempotent management commands, chained in `Procfile`** — every
  seed/backfill/cleanup command is written to be safely re-run on every
  deploy (`get_or_create`, or a no-op once its target state is reached).
  `Procfile`'s one `web:` line runs `migrate` then a long chain of these
  before `collectstatic` and `gunicorn` start. A new module's first-deploy
  setup (seed data, data migrations, permission grants) should follow this
  pattern rather than a one-off script.
- **APScheduler jobs** (`proptasks/scheduler.py`) — periodic work is a
  `_run_command('some_management_command')` wrapper registered with
  `_scheduler.add_job(..., 'interval', minutes=settings.SOME_INTERVAL)` in
  `start()`. No Celery — if a new module needs periodic background work,
  this is the existing mechanism.
- **Bubble-picker UI, not `<select>`** — almost every choice field in this
  app (property type, department, status, priority, assignee...) is a
  "Duolingo-style" bubble-lock picker (`static/js/bubble-picker.js`,
  `data-mode="single"` or `"multi"`), often with a parent→child drilldown for
  large option sets (property picker: type category → specific property).
  Plain `<select>` is the exception, not the default, in this codebase.
- **Inline-edit pencil pattern** (`static/js/inline-edit.js`) — click-to-
  reveal-editable-field, used throughout list views (ticket list, property
  recurring-tasks) instead of a separate edit page for small field changes.

## Permissions (three independent axes — don't conflate)

1. `User.is_superuser` → Django admin + `/admin-tools/` (gated by
   `_is_admin(user)` in `core/views.py`, reused by `onsite/views.py` for its
   checklist-promotion admin screen).
2. `StaffProfile.role` → which department dashboard a ticket routes to.
   Not a permission — every ticket has one, everyone can see every
   department's dashboard.
3. `StaffProfile.is_company_admin` → sees the Owner Dashboard at `/` instead
   of the standard 6-department dashboard. Toggled per-staff from
   `/admin-tools/`. Currently only 2 accounts qualify.

## Ticket creation surfaces today

Three ways a `Ticket` gets created now:
1. **Manual** — staff use "New ticket" (`ticket_create`), `source='manual'`.
2. **Recurring** — `TicketTemplate` rules (the "Functions" system,
   `TaskPackage`/`PackageRun` for grouped multi-step recurring work),
   generated by the `generate_recurring_tickets` scheduler job,
   `source='recurring'`.
3. **On-site visit issue** — a cleaner/inspector flags something outside the
   checklist during a visit; `onsite.services.checklist.submit_visit`
   creates a real `Ticket` (`source='onsite'`, `assigned_role='maintenance'`)
   on submit.

Reactive/AI intake (phone calls via Quo, email via Gmail, shared calendar,
Airbnb/VRBO booking polling) was fully torn out of the scheduler on
2026-08-03 — see the `intake` app note above. If a new module wants
tickets to appear automatically from some new trigger, model it after the
**recurring** or **on-site-issue** paths (explicit rule/human action →
ticket), not the old reactive-classifier path — that path is deliberately
dead.

## UI/design conventions

- One `templates/base.html` — CSS custom properties for the whole brand
  palette (`--brand-primary`, `--surface-card`, `--status-good/warning/
  critical`, `--priority-urgent`, department-color ramp `--role-*`), Inter
  (UI) + IBM Plex Serif (page titles) fonts, all nav/scripts loaded here
  once (no per-page conditional script loading — see `.page-content`,
  `.calendar-days-scroll` for examples of global CSS classes added here).
- Every internal page is `{% extends 'base.html' %}` + `{% block content %}`
  — no other layout template. External no-login pages (vendor portal,
  process external link, `onsite`'s cleaner link) extend
  `vendorportal/templates/vendorportal/base.html` instead — a separate,
  simpler shell with no staff nav, meant to be opened cold from a text
  message link.
- Mobile: `html`/`body` have `overflow-x: hidden` + `touch-action: pan-y
  pinch-zoom` sitewide (locks horizontal drag/bounce); `.page-content` adds
  generous `padding-bottom` so the last element on a page isn't flush
  against the viewport edge. Any new page should extend `base.html` and get
  both for free.
- No automated test suite exists. Verification throughout this project has
  been manual/browser-driven (direct browser interaction, raw HTTP against
  the real dev DB), not pytest/Django TestCase. Worth knowing before
  assuming CI would catch a regression — and worth remembering that
  `manage.py check` passing locally does **not** guarantee a clean deploy
  (see "Media storage" above for two real incidents this caused).

## Where to look for a specific thing

| Need to understand... | Start here |
|---|---|
| How tickets get assigned/routed | `tickets/models.py::Ticket`, `tickets/views.py::ticket_set_status`/`ticket_reassign*` |
| Recurring ticket generation | `tickets/services/applicability.py`, `generate_recurring_tickets` command |
| The Owner Dashboard / company-wide view | `tickets/views.py::_owner_dashboard`, `tickets/templates/tickets/owner_dashboard.html` |
| Multi-step guided workflows | `processes/models.py` (`StepType` enum — 17 types), `processes/views.py` |
| On-site visit scheduling/checklists | `onsite/models.py`, `onsite/services/checklist.py`, `ONSITE_DESIGN.md` |
| OAuth-style external integration | `core/google_calendar.py` (the reference shape), `core/quickbooks.py`/`onsite/google_calendar_push.py` (further examples of the same shape) |
| Adding a DB-backed secret/API key | `core/app_settings.py::SECRET_KEYS` |
| Adding a periodic background job | `proptasks/scheduler.py::start()` |
| Bubble-picker UI usage | `static/js/bubble-picker.js`, any `ticket_form.html`/`contact_form.html` for examples |
| No-login external access pattern | `vendorportal/models.py::AccessAttempt`, `Ticket.completion_token*`, `onsite.Visit.access_token*` |
| Media/file storage config | `proptasks/settings.py` `STORAGES`/`CLOUDINARY_*` — read the comment before changing |
| Deploy-time setup | `Procfile` (the one `web:` line — read it top to bottom) |

## When designing a new module

Questions worth answering before writing code, based on precedent above:
- Does it need its own Django app, or does it belong inside `core`/`tickets`?
  (Rule of thumb in this codebase: a genuinely separate domain concept with
  its own models gets its own app — see `processes`, `supplies`, `onsite`; a
  UI surface on existing data stays in `core`/`tickets`.)
- Does it attach to `Ticket`/`Property`/`Contact`? If it can attach to more
  than one, use the nullable-multi-FK-plus-CheckConstraint pattern, not
  contenttypes.
- Does it need a specific-person-or-department assignee? Reuse the dual-FK
  pattern (with a DB `CheckConstraint`, per `onsite.Visit`/`Ticket`).
- Is there a "template applies broadly, one instance must freeze" need? See
  `onsite`'s checklist resolution/snapshot pattern before inventing a copy-
  on-write scheme.
- Does it call an external API? Follow the `is_configured()` +
  broad-except-degrade-gracefully shape, add its keys to `SECRET_KEYS`, and
  if it's OAuth, mirror `google_calendar.py`/`quickbooks.py`. Verify both
  the configured and unconfigured paths locally before shipping.
- Does it need periodic background work? Add a job function + `add_job()`
  call in `scheduler.py`, not a new scheduling mechanism.
- Does it need first-deploy setup (seed data, permission grants, one-time
  migration)? Write an idempotent management command and chain it into
  `Procfile`.
- Does it add a new `ImageField`, or touch `INSTALLED_APPS`/`STORAGES`? Read
  the "Media storage" section above first — both have already caused a
  production crash-loop once.
