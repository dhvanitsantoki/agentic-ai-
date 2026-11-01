# Flotec Export & OEM Sales AI — Architecture

System for **Ajay Plastic (brand: Flotec), Ahmedabad, Gujarat, India**: an AI-assisted
international OEM & export sales department that researches buyers, verifies contacts,
prepares personalised outreach, manages replies and follow-ups, prepares quotations from
approved pricing, and reports to management — with human approval wherever a commercial,
legal or technical commitment is involved.

---

## 1. Design principles

| Principle | How it is enforced in code |
|---|---|
| **No fabricated data** | AI research output is never written to the CRM directly. Every company needs a source URL; every e-mail address and phone number is re-fetched from its stated public source page and must literally appear there (`research/verification.py`). Addresses that fail are stored as *unverified* and are blocked from outreach by the send policy. Product, price, MOQ, lead-time and certification data come only from records an admin has entered and marked verified/approved. |
| **Human approval for commitments** | A single policy module (`core/policy.py`) decides every send. Prices, discounts, quotations, payment terms, exclusivity, legal/contract language, certifications, technical claims and purchase orders always require a human approval, in every execution mode. |
| **Three execution modes** | Mode 1 *Research only*, Mode 2 *Draft & approve*, Mode 3 *Controlled automation* (only pre-approved e-mail kinds, within daily/hourly caps and the recipient's local business hours). |
| **Kill switch** | Automation state *Running / Paused / Stopped* is checked by the worker before every job and by the scheduler before every enqueue. *Stop* also cancels queued jobs. |
| **Modular agents** | Each agent is its own Django app/module with its own service layer and job handlers; agents communicate through the database and the job queue (not through direct calls), so each can be changed, disabled or tested independently. |
| **Low cost** | One small VPS, PostgreSQL, no Redis/Celery: a DB-backed job queue and cron scheduler. AI spend is metered per call and stopped by a monthly budget cap. |
| **Compliance** | Unsubscribe link in every outreach e-mail, suppression list (unsubscribes, bounces, declines), one initial e-mail per company, follow-ups stop on any reply, no sending outside recipient business hours, rate caps per mailbox. |

---

## 2. Technology stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Best ecosystem for AI, scraping, PDF/Excel, e-mail APIs |
| Web framework | **Django 5.2 LTS** (supported to April 2028) | Built-in auth, permissions, ORM, migrations, admin, CSRF/XSS protection |
| Database | **PostgreSQL 16** (production), SQLite (development/tests) | Reliable, free, `SELECT … FOR UPDATE SKIP LOCKED` for the job queue |
| Background jobs | Built-in DB job queue + cron scheduler (`manage.py run_worker`) using `croniter` | No Redis/Celery to run or pay for; pause/resume/stop is a DB flag |
| AI model | **Anthropic Claude API** — default `claude-opus-5-5`, configurable per agent in Settings (e.g. `claude-sonnet-5-5` / `claude-haiku-5-5` to cut cost) | Server-side web search & web fetch tools for research with source URLs; structured outputs for reliable JSON |
| E-mail | Gmail API, Microsoft Graph, Zoho Mail API — **OAuth 2.0 only**, tokens encrypted at rest (Fernet) | Official APIs, no passwords stored |
| UI | Server-rendered Django templates, Bootstrap 5, HTMX, Chart.js (all vendored locally) | Fast, responsive, no JS build step, works on mobile |
| Documents | ReportLab (PDF quotations & reports), openpyxl (Excel exports) | Pure-Python, no external services |
| App server | Gunicorn behind Nginx, Let's Encrypt TLS, WhiteNoise for static files | Standard, cheap, robust |
| Ops | systemd units (web, worker), daily `manage.py backup` (DB dump + media archive, rotation), optional Docker Compose | Simple to run on one VPS |

---

## 3. Module / agent map

```
                         ┌──────────────────── Management dashboard (reports app) ───────────────────┐
                         │ KPIs · funnel · country/product/employee performance · approvals · errors │
                         └───────────────────────────────────────────────────────────────────────────┘
   Scheduler (cron rows) ──► Job queue (agents.Job) ──► Worker ──► job handlers (one per agent action)
                                     ▲    checks: automation state, execution mode, AI budget
                                     │
 ┌─────────────┐  findings  ┌───────────────┐ verified leads ┌──────────┐  drafts  ┌──────────────┐
 │ 1 Buyer     │──────────► │ Verification  │──────────────► │ 7 CRM &  │────────► │ 3 Outreach   │
 │   research  │            │ (source page, │                │ pipeline │          │   (templates,│
 │ 2 OEM       │            │  MX, website, │                │          │◄──────── │  personalise)│
 │   research  │            │  dedupe,score)│                └────▲─────┘  events  └──────┬───────┘
 └─────────────┘            └───────────────┘                     │                       │ policy.py
 Country market research ─► geo.CountryMarketReport               │              approval queue / auto-send
                                                                  │                       ▼
 ┌──────────────┐ classify/extract ┌──────────────┐  stop rules  ┌─┴───────────┐   ┌──────────────┐
 │ 4 Mailbox    │◄──── inbound ────│ Gmail/Graph/ │◄──── send ───│ Outbound    │◄──│ 5 Follow-up  │
 │   management │                  │ Zoho adapters│              │ e-mail      │   │   sequences  │
 └──────┬───────┘                  └──────────────┘              └─────────────┘   └──────────────┘
        │ quotation request
        ▼
 ┌──────────────┐  approved price lists only   ┌──────────────┐
 │ 6 Quotation  │────────────────────────────► │ Products &   │
 │   agent      │  missing data → human input  │ price lists  │
 └──────────────┘                              └──────────────┘
 8 Report agent: daily/weekly/monthly snapshots, Excel/PDF export, e-mail to management
```

| App | Responsibility |
|---|---|
| `core` | Company profile (approved facts used by the AI), system settings, execution mode, automation state, roles, audit log, notifications, encryption, **send policy** |
| `products` | Categories, products, sizes/variants, documents (catalogue, drawing, images), versioned price lists with approval |
| `geo` | Continents, countries (ISO list + primary time zone), country market reports with sources & confidence |
| `crm` | Companies, contacts, sources/evidence, pipeline stages + history, activities, requirements, tasks, CSV import, filters, Kanban |
| `agents` | Claude client wrapper (cost metering, budget cap, refusal fallback), job queue, worker, scheduler, agent-run log, nightly pipeline |
| `research` | Agents 1 & 2 + country research: web research runs, verification, de-duplication, OEM scoring, review queue |
| `mailbox` | OAuth connections, provider adapters, inbound sync, agent 4 classification/extraction, bounce & unsubscribe handling, reply drafts |
| `outreach` | Agent 3: 10 template types, personalised drafts, sensitive-content checks, approval queue, sending, unsubscribe page, suppression list |
| `followups` | Agent 5: configurable sequences (default Day 0/3/7/14/30), enrolments, stop rules, local-hour windows, escalation |
| `quotations` | Agent 6: quotations from approved prices, missing-data detection, manager approval, PDF, sending |
| `reports` | Agent 8: metrics, snapshots, dashboard, Excel/PDF export |

---

## 4. Key data flows

**Research → CRM (Agents 1/2).** A research run (manual or nightly) asks Claude, with the
web-search and web-fetch tools, for real companies matching a country/segment/product focus
and to return them through a strict `submit_companies` tool, each with source URLs and
quoted evidence. The verification step then: normalises the domain, checks the website
responds, re-downloads each contact's source page and keeps an e-mail/phone only if it
literally appears there, crawls the company's own contact page for additional published
addresses, checks MX records, removes duplicates (domain, name+country, e-mail), computes the
OEM score from evidence-backed signals and assigns category A–E. Only then is a CRM record
created (stage *Research completed* or *Verified*).

**Outreach (Agent 3).** For a verified contact the agent picks the template type, writes a
short personalised e-mail using only (a) facts from the research evidence and (b) the
admin-approved company facts, and runs a sensitive-content check. `policy.py` decides:
blocked / needs approval / may auto-send (Mode 3 only).

**Inbound (Agent 4).** Each mailbox is polled every 10 minutes. Messages are matched to
threads (`In-Reply-To`, provider thread id) or to contacts by address; bounces and
unsubscribe requests are handled deterministically first; the rest are classified into the
10 categories, requirements are extracted, the CRM stage is advanced, follow-ups are stopped,
a reply is drafted for approval, and important messages are escalated to the owner.

**Follow-ups (Agent 5).** Hourly: due enrolments are checked against stop rules (any reply,
decline, unsubscribe, bounce, stage advanced), deferred to the recipient's local business
hours, then a follow-up is drafted and handled by the same policy.

**Quotation (Agent 6).** Extracted requirements are matched to verified product variants;
unit prices come only from an *approved*, in-date price list in the requested currency.
Missing price, payment terms, lead time, Incoterm or freight puts the quotation in
*Needs input*. Only a manager/admin can approve; discounts are manager-only; the PDF is
generated on approval and sending always requires approval.

---

## 5. Execution modes & mandatory approvals

| | Mode 1 Research only | Mode 2 Draft & approve | Mode 3 Controlled automation |
|---|---|---|---|
| Research, verification, CRM updates | ✔ | ✔ | ✔ |
| AI drafts e-mails | ✘ | ✔ | ✔ |
| AI sends without a human | ✘ | ✘ | Only the e-mail kinds an admin ticks (e.g. initial intro, follow-ups), within caps & local hours, and only when no sensitive topic is detected |
| Price, discount, quotation, payment terms, exclusivity, legal, certification, technical claim, PO, contract | Human | Human | **Always human** |

---

## 6. Integration requirements (what the owner must provide)

| Integration | Needed | Where |
|---|---|---|
| Anthropic API key | Yes, for AI features (the CRM, products, quotations and reports work without it) | console.anthropic.com → API keys; set `ANTHROPIC_API_KEY` |
| Zoho Mail | If the company uses Zoho | api-console.zoho.in (India DC) → *Server-based application*; scopes `ZohoMail.messages.ALL, ZohoMail.accounts.READ, ZohoMail.folders.READ` |
| Gmail / Google Workspace | If used | Google Cloud console → OAuth client (Web); Gmail API enabled; scope `gmail.modify`. Workspace: set consent screen to *Internal* (no Google verification needed) |
| Microsoft 365 / Outlook | If used | Entra ID app registration (Web); delegated `Mail.ReadWrite`, `Mail.Send`, `offline_access`, `User.Read` |
| Domain DNS | Strongly recommended | SPF, DKIM and DMARC records for the sending domain (deliverability) |
| Verified company data | Yes | Company profile, approved facts/certifications, products, sizes, MOQs, catalogues, approved price lists, default commercial terms — entered by the owner |
| Server | Yes | 2 vCPU / 2–4 GB RAM Ubuntu 24.04 VPS, domain name, HTTPS |

---

## 7. Estimated operating costs (monthly)

Infrastructure (fixed):

| Item | Estimate |
|---|---|
| VPS 2 vCPU / 4 GB (e.g. Hetzner, DigitalOcean Bangalore, AWS Lightsail Mumbai) | US$ 5 – 24 |
| Off-site backups (Backblaze B2 / S3, a few GB) | ≈ US$ 1 |
| Domain + TLS (Let's Encrypt is free) | ≈ ₹ 1,000 / year |
| E-mail APIs (Gmail API / Graph / Zoho Mail API) | No extra charge — uses existing mailboxes |

AI usage (variable, Claude API list prices at the time of writing: Opus 5.5 $4 / $20 per
million input/output tokens, Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50; web search is
billed per search — check current pricing at platform.claude.com):

| Activity | Typical tokens per item | ≈ cost per item (Opus 5.5) |
|---|---|---|
| Research a company (web search + fetch + structured output) | 15–40 k in, 2–4 k out, 2–4 searches | US$ 0.10 – 0.25 |
| Personalised outreach / follow-up draft | 3–5 k in, 0.6 k out | US$ 0.02 – 0.03 |
| Classify + extract an inbound e-mail, draft reply | 3–6 k in, 0.8 k out | US$ 0.02 – 0.04 |
| Country market report | 40–80 k in, 4 k out, 5–8 searches | US$ 0.30 – 0.50 |

| Volume profile | Research/day | Drafts/day | Inbound/day | ≈ AI cost / month (Opus 5.5) | with Sonnet 5.5 |
|---|---|---|---|---|---|
| Light | 10 | 20 | 10 | US$ 50 – 100 | ≈ half |
| Standard | 30 | 60 | 30 | US$ 150 – 300 | ≈ half |

A **monthly AI budget cap** (Settings) stops all AI calls once the metered spend reaches the
cap; per-day research limits bound the largest cost driver. Every AI call's tokens and cost
are logged and shown on the dashboard.

---

## 8. Security

* Django auth with PBKDF2/argon password hashing, password validators, login throttling,
  session timeout, CSRF on every form, secure/HTTP-only cookies and HSTS in production.
* Roles: **Admin** (settings, users, mailboxes, mode, price approval), **Manager** (approvals,
  quotations, discounts, research runs), **Sales executive** (CRM, drafts), **Viewer** (read-only).
* OAuth tokens encrypted with Fernet (`FIELD_ENCRYPTION_KEY`); secrets only in environment
  variables, never in source code or the database in plain text.
* All AI-generated and human actions written to an audit log.
* Inbound e-mail HTML is never rendered unsanitised (plain-text view).
* Daily backups with rotation; documented restore procedure.

---

## 9. Phase plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | Architecture, project scaffold, core settings, roles, audit, policy skeleton | App boots, login works, tests pass |
| 1 | Product database: categories, products, variants, documents, price lists + approval | CRUD + price lookup tests |
| 2 | CRM: countries, companies, contacts, pipeline, activities, tasks, filters, Kanban, CSV import | CRUD, stage history, filter tests |
| 3 | Agent infrastructure + Global buyer research agent + verification + dedupe | Mocked-AI pipeline tests, verification tests |
| 4 | OEM research agent, scoring A–E, country market research | Scoring & categorisation tests |
| 5 | Mailbox OAuth (Gmail, Outlook, Zoho), templates, outreach drafts, approval queue, sending, unsubscribe | Provider tests with mocked HTTP, policy tests |
| 6 | Inbound sync, classification, extraction, bounce/unsubscribe, reply drafts, escalation | Classification pipeline tests |
| 7 | Follow-up sequences, stop rules, local-hour windows | Scheduling & stop-rule tests |
| 8 | Quotations from approved prices, approvals, PDF | Pricing/approval/PDF tests |
| 9 | Reports, dashboard, Excel/PDF exports | Metrics tests |
| 10 | Security hardening, backup/restore, deployment files, install guide, end-to-end browser check | Full test suite green, deploy check |

Progress, test results and pending items for each phase are recorded in `docs/PHASE_REPORTS.md`.
