# Phase reports

Test totals at completion: **129 automated tests, all passing on SQLite and on PostgreSQL 16**
(`python manage.py test`). AI and mailbox providers are tested with fakes/mocked HTTP; nothing in
the test suite calls a real external service.

Verification levels used below:
* **Unit/integration tested** – automated tests against the real database.
* **Browser-verified** – exercised in headless Chromium against the running app (gunicorn + PostgreSQL).
* **Not yet live-verified** – depends on credentials not available during development (see "Pending").

---

## Phase 0 – Architecture & foundation
**Delivered:** architecture & cost document; Django 5.2 LTS project with 11 modular apps; company
profile (approved facts), system settings, execution modes, automation state, roles
(admin/manager/executive/viewer), audit log, notifications, Fernet token encryption, login throttling,
read-only viewer middleware, responsive Bootstrap UI with locally vendored assets.
**Verified:** smoke test renders every page for admin; viewer write-blocking; login lock-out after 5 failures.

## Phase 1 – Product database (12 tests)
**Delivered:** categories (the 11 from the brief), products with specs/standards/pressure/connections/
MOQ/packaging/samples/OEM flag, catalogue PDF & drawing uploads, images, sizes/SKUs (+CSV import),
manager verification (technical edits reset verification), documents library with "approved for
sending", versioned price lists (draft → approved → archived, approved lists immutable, CSV import,
"new version"), quantity breaks, price lookup by currency/date.
**Verified:** prices only from approved, in-date lists; quantity breaks; no currency conversion;
locked lists; size normalisation; only verified products are matched.
**Defects found & fixed:** inline "add size" saved sizes as *inactive* (unrendered checkbox) and the
image upload required a hidden field — found in the browser walkthrough; fixed + regression tests.

## Phase 2 – CRM (8 tests + import/UI tests)
**Delivered:** 243 countries (ISO list from the tz database) by continent with primary time zones;
companies with all fields from the brief; contacts with e-mail verification status and sources;
evidence/source records; pipeline New lead → Payment received (+ Closed) with history; activities
(calls, WhatsApp, meetings, notes); requirements; tasks; filters (country, continent, product,
OEM/export, lead quality, status, employee, follow-up due, quotation status, potential value);
Kanban; CSV import with de-duplication; Excel/CSV export.
**Verified:** duplicate detection (domain, name+country, e-mail; free-mail domains excluded);
agents only move deals forward; CSV import.
**Defect fixed:** HTML date inputs rendered `dd/mm/yyyy` under the en-GB locale and blocked form
submission — browser-found; all date widgets now ISO; regression test added.

## Phase 3 – Agent infrastructure & Global buyer research agent (14 + 10 tests)
**Delivered:** DB job queue (SKIP LOCKED on PostgreSQL), retries with back-off, stale-job recovery,
cron scheduler (default 02:00 nightly pipeline), Running/Paused/Stopped with immediate effect,
Claude wrapper (cost metering per call, monthly budget cap, server-side refusal fallback, structured
outputs, web search/fetch tool loop with `pause_turn`), research pipeline with deterministic
verification: website check, every e-mail/phone/name must appear on its cited page (obfuscated
addresses handled), MX checks, company-published contacts crawled from its own contact pages,
SSRF protection, daily limits, review queue.
**Verified:** fabricated e-mail/phone/name/LinkedIn are discarded and logged; unreachable sites go
to review; duplicates merge; budget cap blocks calls; pause/stop semantics; scheduler enqueues once.

## Phase 4 – OEM research agent & country research (included above)
**Delivered:** OEM-focused discovery, company deep-dive, evidence-based OEM score with breakdown,
A–E categorisation, country market reports with sources (flagging sources the search tool never
returned) and confidence, country pages, priority-country rotation for the nightly run.

## Phase 5 – E-mail integration & outreach (27 tests incl. policy, sending, unsubscribe)
**Delivered:** OAuth 2.0 connect/reconnect/disconnect for Zoho Mail (data-centre aware), Gmail and
Microsoft Graph; encrypted tokens with auto-refresh; send with attachments, threading, message-ID
tracking; 10 template types; personalised drafting grounded in approved facts + sourced research
with signature added by code; unsupported-number detection; central policy (modes, mandatory human
approval topics, verified recipients, suppression, duplicate introductions, follow-up stop, caps,
recipient business hours); approval queue (manager required for sensitive content); edit resets
approval; compose/reply manually; suppression list; unsubscribe page with RFC 8058 one-click.
**Verified:** 15 policy cases; send → CRM/follow-up enrolment; suppression at send time; approved
attachments only; auth errors mark the mailbox; provider request shapes (mocked HTTP) for all three.

## Phase 6 – Reply management (10 tests)
**Delivered:** inbox sync with de-duplication; thread matching (In-Reply-To/References, provider
thread); deterministic bounce, auto-reply and unsubscribe handling; AI classification into the 10
categories (+ urgency, requirements, questions, sample/catalogue/quotation intent); CRM stage updates;
follow-ups stopped and unsent follow-ups cancelled on any reply; escalation tasks + notifications +
e-mail; reply drafts (catalogue attachments only from approved documents); new leads from genuine
unknown-sender enquiries; manual re-classification; prompt-injection guidance in all prompts.

## Phase 7 – Follow-up automation (9 tests)
**Delivered:** editable sequences (default Day 3/7/14/30), one active sequence per contact, one
e-mail per step (DB constraint), next step scheduled after the previous one is sent (min. 2-day gap),
stop rules (reply, decline, unsubscribe, bounce, stage progressed, do-not-contact), pause/resume/stop
per contact, threading with the introduction.

## Phase 8 – Quotations (11 tests)
**Delivered:** quotations from requirements or manually; product matching to verified sizes; prices
from approved lists only; MOQ/packaging from product data; company default terms used only if set;
missing-data list → *Needs human input*; manager-only approval, discounts and manual prices; PDF
(ReportLab) on approval; locked after approval; revisions; quotation e-mail with PDF (still through
policy); expiry job; accepted → *Order confirmed*.
**Defect fixed:** creating a revision mutated the original object in memory — fixed + test.

## Phase 9 – Reports & dashboard (10 tests)
**Delivered:** metrics computed from records (new companies, verified buyers, sent, bounced, replies,
interested, quotations requested/sent, follow-ups, meetings, orders, pipeline value by currency,
country/product/employee breakdowns, agent errors, pending approvals, AI cost); daily/weekly/monthly
snapshots on schedule; AI narrative written only from the numbers; e-mail with PDF; Excel/PDF
export; dashboard with charts; CRM hygiene job (overdue next actions, stale opportunities).

## Phase 10 – Security, deployment, documentation (security tests + live checks)
**Delivered/verified:** `check --deploy` clean (except optional HSTS preload); open-redirect
protection; upload type allow-lists; uploads served only to signed-in users; inbound e-mail HTML
escaped; tests isolated from real media; systemd units, Nginx config, Docker Compose, backup
command with rotation + restore script (backup restored into a scratch PostgreSQL database and
row counts checked); full browser walkthrough with zero console errors (profile → product →
approved price list → company/contact → quotation → approval → PDF; pause/stop/resume; reports;
mobile layout); background worker executed the scheduled jobs, the nightly pipeline and a backup.

---

## Pending / to do at go-live

| Item | Why pending | What to do |
|---|---|---|
| Live Claude API calls | No API key in the development environment; request shapes follow the official SDK docs and are covered by fake-client tests | Set `ANTHROPIC_API_KEY`, run one research run (1 country, 3 companies) and one draft; check *Automation → Agent log* |
| Live mailbox connections | Need the company's OAuth apps and a public HTTPS URL | Follow `docs/EMAIL_SETUP.md`, connect, send one test e-mail to an internal address, reply to it, check the Inbox |
| Real product, price and company data | Must come from Ajay Plastic (no data was invented) | Enter products/sizes/prices/catalogues and the company profile |
| WhatsApp | The brief says "if integrated"; no WhatsApp Business API account | WhatsApp numbers are stored and WhatsApp conversations can be logged; API integration can be added later |
| Two-factor login | Not in scope of phase plan | Use strong passwords; can be added (e.g. TOTP) later |
| Delivery receipts | Providers do not expose them | Bounce-based reporting is used |
