# Flotec Export & OEM Sales AI

AI-assisted international OEM and export sales department for **Ajay Plastic (brand: Flotec)**,
Ahmedabad, Gujarat, India — plastic valves, PVC/CPVC/UPVC valves, drip irrigation valves, pipes and fittings.

The application researches buyers worldwide, verifies every contact against its public source,
scores OEM potential, writes personalised e-mails, manages replies and follow-ups, prepares
quotations from approved price lists, and reports to management — with a human in the loop for
anything commercial, technical or legal.

| Agent | Module |
|---|---|
| 1 Global buyer research | `apps/research` (pipeline, verification) |
| 2 OEM customer research | `apps/research` (OEM prompts, deep-dive, scoring A–E) |
| 3 E-mail outreach | `apps/outreach/drafting.py`, `templates` |
| 4 E-mail management | `apps/mailbox` (providers, sync, classification) |
| 5 Follow-up | `apps/followups` |
| 6 Quotation & commercial | `apps/quotations` |
| 7 CRM & pipeline | `apps/crm` |
| 8 Management reports | `apps/reports` |
| Automation engine, kill switch, AI metering | `apps/agents`, `apps/core/policy.py` |

## Documentation

* [Architecture, stack, integrations, operating costs, phase plan](docs/ARCHITECTURE.md)
* [Installation guide](docs/INSTALL.md)
* [Mailbox (Zoho / Gmail / Outlook) setup](docs/EMAIL_SETUP.md)
* [Hosting on Vercel (serverless)](docs/VERCEL.md)
* [User guide](docs/USER_GUIDE.md)
* [Phase reports, test results, pending items](docs/PHASE_REPORTS.md)

## Quick start (local evaluation)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
printf "DJANGO_DEBUG=1\nSITE_URL=http://localhost:8000\n" > .env && .venv/bin/python manage.py migrate && .venv/bin/python manage.py setup_initial_data
```

```bash
.venv/bin/python manage.py createsuperuser && .venv/bin/python manage.py runserver
```

Background worker (second terminal): `.venv/bin/python manage.py run_worker` · Tests: `.venv/bin/python manage.py test`

The system starts in **Mode 2 (draft & approval)** with automation **paused**, and contains no
sample companies, products or prices — all business data must come from Ajay Plastic.
