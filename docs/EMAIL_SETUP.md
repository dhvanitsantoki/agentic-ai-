# Mailbox integration (OAuth 2.0)

The system never stores mailbox passwords. Each provider needs a one-time "OAuth app"
registration; then an admin clicks **Connect** on *Settings → Mailboxes* and signs in with the
company mailbox. Tokens are encrypted in the database with `FIELD_ENCRYPTION_KEY`.

The redirect URI to register is shown on the Mailboxes page and has the form:

```
https://<your-domain>/mailbox/oauth/callback/<provider>/
```

where `<provider>` is `zoho`, `gmail` or `outlook`. It must be HTTPS in production.

---

## Zoho Mail (recommended if the company already uses Zoho)

1. Sign in to the Zoho API console of **your data centre** — for accounts in India this is
   `https://api-console.zoho.in` (US: `.com`, EU: `.eu`).
2. *Add client* → **Server-based Applications**.
3. Homepage URL: your site. Authorized redirect URI: `https://<your-domain>/mailbox/oauth/callback/zoho/`.
4. Copy the Client ID and Client Secret into `.env`:
   `ZOHO_OAUTH_CLIENT_ID=…`, `ZOHO_OAUTH_CLIENT_SECRET=…`, `ZOHO_DC=in` (or `com`, `eu`, …).
5. Restart the services and click **Connect Zoho Mail**.

Scopes requested: `ZohoMail.messages.ALL, ZohoMail.accounts.READ, ZohoMail.folders.READ`.

## Gmail / Google Workspace

1. Google Cloud console → create a project → *APIs & Services* → enable **Gmail API**.
2. *OAuth consent screen*: for a Google Workspace domain choose **Internal** (no Google review needed).
   For a personal @gmail.com account the app must be "External"; while it is in *Testing* mode
   Google expires refresh tokens after 7 days, so publishing/verification is needed for long-term use.
3. *Credentials* → *Create OAuth client ID* → **Web application**; add the redirect URI
   `https://<your-domain>/mailbox/oauth/callback/gmail/`.
4. `.env`: `GOOGLE_OAUTH_CLIENT_ID=…`, `GOOGLE_OAUTH_CLIENT_SECRET=…`.

Scope requested: `gmail.modify` (read, send, label — not delete).

## Microsoft 365 / Outlook

1. Microsoft Entra admin centre → *App registrations* → **New registration**.
   Supported accounts: *this organisation only* (single tenant) is recommended.
2. Redirect URI (Web): `https://<your-domain>/mailbox/oauth/callback/outlook/`.
3. *Certificates & secrets* → new client secret. *API permissions* → Microsoft Graph →
   delegated: `Mail.ReadWrite`, `Mail.Send`, `offline_access`, `User.Read` → grant admin consent.
4. `.env`: `MS_OAUTH_CLIENT_ID=…`, `MS_OAUTH_CLIENT_SECRET=…`, `MS_OAUTH_TENANT=<tenant-id>` (or `common`).

---

## What the integration does

| Feature | How |
|---|---|
| Read e-mails | Inbox polled every 10 minutes (Gmail history API, Graph inbox query, Zoho folder listing) |
| Send approved e-mails | Gmail `messages.send` (raw MIME), Graph draft → send, Zoho `messages` API |
| Replies stay in the thread | `In-Reply-To`/`References` headers and the provider's thread / reply endpoints |
| Attach catalogues | only documents marked *Approved for sending*; quotation PDFs after manager approval |
| Track message IDs | provider id, thread/conversation id and RFC `Message-ID` stored on every sent e-mail |
| Bounces | delivery-failure messages are detected; the address is marked *bounced*, suppressed, and its follow-ups stop |
| Unsubscribe | every introduction/follow-up carries a personal unsubscribe link (+ `List-Unsubscribe` one-click header where the provider allows custom headers); e-mails asking to be removed are honoured automatically |

Delivery confirmation: none of the three APIs reports successful delivery, so reports show
"sent" and "bounced" (and "not bounced" = sent minus bounces).

## Deliverability checklist (do this before sending)

* Publish **SPF**, **DKIM** and **DMARC** DNS records for the sending domain (your mail provider's
  admin console shows the exact values).
* Start with low limits (e.g. 20/day per mailbox, 5/hour) for 2–3 weeks and increase slowly.
* Send only to verified business addresses (the policy enforces this) and keep e-mails short and personal.
* Keep the unsubscribe footer (the settings form refuses a footer without `{unsubscribe_url}`).
* Check the Mailboxes page for errors and the Outbound e-mails page for bounces weekly.
