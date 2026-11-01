# User guide

## Roles

| Role | Can |
|---|---|
| **Admin / Owner** | everything: settings, users, mailboxes, execution mode, price approval |
| **Sales manager** | approve e-mails (including commercial/technical content), approve quotations, set discounts and manual prices, approve price lists, pause/stop automation, edit templates and follow-up schedules |
| **Sales executive** | CRM work, research runs, AI drafts, approve *plain* e-mails (no prices, terms, certifications or technical claims), prepare quotations |
| **Viewer** | read everything, change nothing |

## The daily routine (15–30 minutes)

1. **Dashboard** – "Needs a decision" lists e-mails and quotations waiting for you, follow-ups due,
   overdue tasks and agent errors.
2. **Approvals** – read each draft. The card shows *why* it needs approval, any sensitive topics
   detected and anything the agent asked you to fill in (`[TEAM TO CONFIRM: …]`). Approve, edit or reject.
   Approved e-mails are sent in the buyer's local business hours within the sending limits.
3. **Inbox** – replies are classified (interested, need quotation, need catalogue, technical question,
   negotiation, not interested, future opportunity, existing customer, complaint …) with a summary,
   extracted requirements and a drafted reply in the approval queue. Correct a classification with
   one click; the CRM updates accordingly. Mark handled when done.
4. **Quotations** – the agent drafts a quotation when a buyer asks for one. It uses only approved
   price lists; anything missing (a price in that currency, payment terms, lead time, freight,
   validity, MOQ issues) is listed under *Human input needed*. A manager approves, which generates
   the PDF, then *Prepare e-mail* → approve.
5. **Tasks** – escalations from the agents (hot leads, quotation requests, complaints) and CRM hygiene.

## Finding buyers

* **Research runs → New research run**: choose *Global buyer research* or *OEM customer research*,
  up to 10 countries, industries, company types, product focus and how many companies.
* Every company the agent reports is verified before it reaches the CRM: the website must respond,
  each e-mail/phone must be printed on the page it came from, e-mail domains must accept mail, and
  duplicates are merged. Anything the agent claimed but could not be verified is shown under
  *Verification details → Discarded* — it is never stored as a contact.
* Companies whose website could not be checked go to the **Review queue**.
* **OEM deep-dive** (company page) researches one company's brands, product range and OEM fit.
* **Country markets → AI market report**: demand evidence, import requirements, standards,
  competitors, with sources and a confidence level. Sources the search tool never returned are flagged.

## OEM score and categories

Points are given only for signals backed by a quote from a reachable source page:
own-brand valves (25), outsourcing evidence (15), imports plastic valves (15), manufactures irrigation
products (12), private-label need (10), distribution network (10), bulk volumes (8), product fit (10).

* **A** – high-potential OEM buyer (score ≥ 60 with OEM evidence)
* **B** – medium-potential OEM buyer (score ≥ 40 with OEM/manufacturing evidence)
* **C** – general distributor · **D** – importer · **E** – not relevant

The company page shows each signal, its quote and source.

## Outreach and follow-ups

* Draft for one company: company page → **Draft e-mail (AI)** → choose template and contact.
* Nightly (02:00): the pipeline drafts introductions for verified, uncontacted companies (A–D).
* The agent writes only from the company profile's approved facts, verified product data and the
  buyer's sourced research. Numbers that do not appear in those facts are flagged for review.
* After an introduction is sent, the contact enters the follow-up sequence (Day 3, 7, 14, final Day 30 —
  editable under *Follow-ups → Edit schedule*). Any reply, decline, unsubscribe or bounce stops it
  immediately and cancels unsent follow-ups. Auto-replies (out of office) do not.

## Execution modes and the kill switch (Automation page)

* **Mode 1 – Research only**: research and verified leads; no AI e-mails.
* **Mode 2 – Draft and approval** (recommended start): AI drafts, humans approve every send.
* **Mode 3 – Controlled automation**: introductions/follow-ups (and catalogue replies if ticked)
  from templates approved for auto-send are sent without a human, within daily/hourly limits and
  local business hours — *unless* the text mentions prices, discounts, quotations, payment terms,
  delivery commitments, exclusivity, legal terms, certifications, technical claims, capacity or
  purchase orders, which always go to a human.
* **Pause**: schedules stop; manual actions still work. **Stop now**: emergency stop — cancels queued
  jobs, blocks all AI calls and all sending. **Resume** to continue.

## Reports

Daily (07:30), weekly (Monday) and monthly reports are generated automatically and e-mailed to the
report recipients with a PDF. *Reports* lists them; each can be exported to Excel or PDF. The
optional AI summary is written only from the report's numbers.

## What the AI will never do

* invent companies, people, e-mail addresses, phone numbers or LinkedIn profiles;
* state prices, discounts, payment terms, delivery times, MOQs, certifications or technical claims
  that are not in the approved data — and never send such content without a manager;
* change an approved price list or approve a quotation or discount;
* e-mail an unverified, unsubscribed, bounced or declined address, or send a second introduction;
* follow up after the buyer has replied.
