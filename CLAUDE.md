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
no-login completion portal, a supply-request digest, and a company-wide
owner dashboard with QuickBooks financials and weather.

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
`gunicorn` + WhiteNoise for static files.

## The 7 Django apps

- **`core`** (`core/models.py` 604 lines, `core/views.py` 1549 lines) — the
  hub: `Property`, `Contact`, `StaffProfile`, auth (Google Sign-On + password
  fallback), the Admin Tools screen, external integrations
  (`google_calendar.py`, `quickbooks.py`, `places.py`, `usps.py`), the
  DB-backed secrets system (`app_settings.py`).
- **`tickets`** (`tickets/models.py` 739 lines, `tickets/views.py` 2455
  lines — the largest file in the app) — `Ticket`, `TicketTemplate` (recurring
  rules), `TaskPackage`/`PackageRun` ("Functions", a named group of recurring
  steps), dashboards (main, Owner, 6 department sub-dashboards), the ticket
  list/detail/create/reassign/follow-up flows.
- **`processes`** — a generalized 17-step-type workflow engine (`ProcessTemplate`
  → `ProcessRun` → `ProcessRunStep`), a staff-facing builder UI, and a
  no-login external link for outside parties to complete assigned steps.
- **`vendorportal`** — the no-login, token-keyed page a vendor/contractor
  uses to mark a ticket done and upload photos. Rate-limited by IP
  (`AccessAttempt`).
- **`messaging`** — `send_followup_bulk` and friends: one abstraction for
  "email or SMS to one or more contacts," used by tickets and properties.
- **`supplies`** — shortage reports → daily digest → Amazon-search-link
  order list. Checkout stays manual, nothing auto-purchases.
- **`intake`** — the reactive-ticket-creation machinery. **As of 2026-08-03
  this no longer creates tickets automatically** (see "Intake is off" below)
  but the models/classifiers/webhook receiver are all still here, just
  unwired from the scheduler.

Root URL config (`proptasks/urls.py`) mounts `vendorportal`/`supplies`/
`processes` under their own prefixes and `core`/`intake`/`tickets` at the
root — so route names across those three can collide if you're not careful;
check `tickets/urls.py` and `core/urls.py` before naming a new route.

## Core domain model

```
Property ──< Ticket >── StaffProfile (assigned_staff)
    │            └──── Contact (assigned_contact, "vendor" path)
    │
    ├──< Contact (M2M, "which contacts belong to this property")
    ├──< ProcessRun (also attachable to Ticket or Contact — exactly one, DB-checked)
    └──< PropertyDocument, PropertyAttribute (tags), PropertySystemLocation
```

- **`Property`** — has a `property_type` (association/STR/LTR/snowbird/
  commercial/general) and `is_general` (a "not one specific address" bucket
  per type, used when a ticket can't be pinned to a real address). Structured
  address fields (`street`/`city`/`state`/`zip_code`) auto-derive the display
  `address`; `address_verified` comes from USPS if configured.
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
  email/quo/calendar/airbnb/vrbo/fake — only `manual` and `recurring` are
  live now), `assigned_role` (department, required) + `assigned_staff`
  (specific person, optional) + `assigned_contact` (vendor, mutually
  exclusive with `assigned_staff`), `due_date`, `priority`, `delayed`
  (bool). A ticket only shows up in dashboards once it has a `property` —
  see `ticket_form.html`'s required property picker.

## Established patterns worth reusing

- **Dual assignment FK** (`assigned_role` + `assigned_staff`, or
  `assigned_staff` xor `assigned_contact`) — used for Ticket assignment and
  copied verbatim for Process step assignees. If a new module needs "assign
  to a department, optionally a specific person," reuse this shape rather
  than inventing a new one.
- **Nullable-multi-target FK + CheckConstraint** — `FollowUpLog` (ticket-or-
  property) and `ProcessRun` (ticket-or-property-or-contact) both use several
  nullable FKs plus a `CheckConstraint` requiring exactly one set, instead of
  `GenericForeignKey`/contenttypes (never used anywhere in this codebase,
  despite being installed). Follow this precedent, not contenttypes.
- **External no-login token access** — `Ticket.completion_token` (vendor
  portal) and `ProcessRunExternalAccess` (process external link) both follow
  the same shape: a UUID token + expiry + `vendorportal.models.AccessAttempt.
  is_rate_limited(ip)` for brute-force protection. Reuse `AccessAttempt` for
  any new no-login surface rather than building new rate-limiting.
- **"Unconfigured = graceful degrade, never crash"** — every external
  integration (`google_calendar.py`, `quickbooks.py`, `places.py`, `usps.py`,
  Quo, Gmail) has an `is_configured()` guard and a broad `except Exception`
  around the actual API call that logs and returns `None`/`[]`/`False`
  rather than raising. UI shows a "Connect X" prompt instead of erroring.
  New integrations should follow this shape from the start.
- **DB-backed secret overrides** (`core/app_settings.py`) — add a key to
  `SECRET_KEYS` and it automatically gets a masked input on `/admin-tools/`,
  no template changes needed. `apply_overrides()` copies DB values onto
  `django.conf.settings` at boot and after every save.
- **Idempotent management commands, chained in `Procfile`** — every
  seed/backfill/cleanup command is written to be safely re-run on every
  deploy (`get_or_create`, or a no-op once its target state is reached).
  `Procfile`'s one `web:` line runs `migrate` then a long chain of these
  before `gunicorn` starts. A new module's first-deploy setup (seed data,
  data migrations, permission grants) should follow this pattern rather than
  a one-off script.
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
   `_is_admin(user)` in `core/views.py` and `tickets/views.py`).
2. `StaffProfile.role` → which department dashboard a ticket routes to.
   Not a permission — every ticket has one, everyone can see every
   department's dashboard.
3. `StaffProfile.is_company_admin` → sees the Owner Dashboard at `/` instead
   of the standard 6-department dashboard. Toggled per-staff from
   `/admin-tools/`. Currently only 2 accounts qualify.

## Ticket creation surfaces today

Only two ways a `Ticket` gets created now:
1. **Manual** — staff use "New ticket" (`ticket_create`), `source='manual'`.
2. **Recurring** — `TicketTemplate` rules (the "Functions" system,
   `TaskPackage`/`PackageRun` for grouped multi-step recurring work),
   generated by the `generate_recurring_tickets` scheduler job,
   `source='recurring'`.

Reactive/AI intake (phone calls via Quo, email via Gmail, shared calendar,
Airbnb/VRBO booking polling) was fully torn out of the scheduler on
2026-08-03 — see the `intake` app note above. If a new module wants
tickets to appear automatically from some new trigger, model it after the
**recurring** path (explicit rule → scheduled job → ticket), not the old
reactive-classifier path — that path is deliberately dead.

## UI/design conventions

- One `templates/base.html` — CSS custom properties for the whole brand
  palette (`--brand-primary`, `--surface-card`, `--status-good/warning/
  critical`, `--priority-urgent`, department-color ramp `--role-*`), Inter
  (UI) + IBM Plex Serif (page titles) fonts, all nav/scripts loaded here
  once (no per-page conditional script loading — see `.page-content`,
  `.calendar-days-scroll` for examples of global CSS classes added here).
- Every page is `{% extends 'base.html' %}` + `{% block content %}` — no
  other layout template.
- Mobile: `html`/`body` have `overflow-x: hidden` + `touch-action: pan-y
  pinch-zoom` sitewide (locks horizontal drag/bounce); `.page-content` adds
  generous `padding-bottom` so the last element on a page isn't flush
  against the viewport edge. Any new page should extend `base.html` and get
  both for free.
- No test suite exists. Verification throughout this project has been
  manual/browser-driven (Explore agents + live browser testing), not
  pytest/Django TestCase. Worth knowing before assuming CI would catch a
  regression.

## Where to look for a specific thing

| Need to understand... | Start here |
|---|---|
| How tickets get assigned/routed | `tickets/models.py::Ticket`, `tickets/views.py::ticket_set_status`/`ticket_reassign*` |
| Recurring ticket generation | `tickets/services/applicability.py`, `generate_recurring_tickets` command |
| The Owner Dashboard / company-wide view | `tickets/views.py::_owner_dashboard`, `tickets/templates/tickets/owner_dashboard.html` |
| Multi-step guided workflows | `processes/models.py` (`StepType` enum — 17 types), `processes/views.py` |
| OAuth-style external integration | `core/google_calendar.py` (the reference shape), `core/quickbooks.py` (a second real example of the same shape) |
| Adding a DB-backed secret/API key | `core/app_settings.py::SECRET_KEYS` |
| Adding a periodic background job | `proptasks/scheduler.py::start()` |
| Bubble-picker UI usage | `static/js/bubble-picker.js`, any `ticket_form.html`/`contact_form.html` for examples |
| No-login external access pattern | `vendorportal/models.py::AccessAttempt`, `Ticket.completion_token*`, `processes/models.py::ProcessRunExternalAccess` |
| Deploy-time setup | `Procfile` (the one `web:` line — read it top to bottom) |

## When designing a new module

Questions worth answering before writing code, based on precedent above:
- Does it need its own Django app, or does it belong inside `core`/`tickets`?
  (Rule of thumb in this codebase: a genuinely separate domain concept with
  its own models gets its own app — see `processes`, `supplies`; a UI
  surface on existing data stays in `core`/`tickets`.)
- Does it attach to `Ticket`/`Property`/`Contact`? If it can attach to more
  than one, use the nullable-multi-FK-plus-CheckConstraint pattern, not
  contenttypes.
- Does it need a specific-person-or-department assignee? Reuse the dual-FK
  pattern.
- Does it call an external API? Follow the `is_configured()` +
  broad-except-degrade-gracefully shape, add its keys to `SECRET_KEYS`, and
  if it's OAuth, mirror `google_calendar.py`/`quickbooks.py`.
- Does it need periodic background work? Add a job function + `add_job()`
  call in `scheduler.py`, not a new scheduling mechanism.
- Does it need first-deploy setup (seed data, permission grants, one-time
  migration)? Write an idempotent management command and chain it into
  `Procfile`.
