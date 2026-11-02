# Installation guide

Target: one Ubuntu 24.04 server (2 vCPU, 2–4 GB RAM), PostgreSQL 16, Nginx, HTTPS.
Estimated time: 45–60 minutes. Commands assume a user with `sudo`.

---

## 1. Server packages

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-dev build-essential postgresql postgresql-client nginx certbot python3-certbot-nginx git
```

## 2. Database

```bash
sudo -u postgres psql -c "CREATE USER flotec WITH PASSWORD 'choose-a-long-password';"
```

```bash
sudo -u postgres psql -c "CREATE DATABASE flotec OWNER flotec;"
```

## 3. Application user and code

```bash
sudo adduser --system --group --home /srv/flotec flotec
```

```bash
sudo mkdir -p /srv/flotec/app /srv/flotec/media /srv/flotec/backups /srv/flotec/logs && sudo chown -R flotec:flotec /srv/flotec
```

Copy the project into `/srv/flotec/app` (git clone of your repository, or unzip the source archive), then:

```bash
cd /srv/flotec/app && sudo -u flotec python3 -m venv .venv && sudo -u flotec .venv/bin/pip install -r requirements.txt
```

## 4. Configuration (`.env`)

```bash
sudo -u flotec cp .env.example .env && sudo -u flotec chmod 600 .env
```

Edit `/srv/flotec/app/.env`:

| Variable | What to put |
|---|---|
| `DJANGO_SECRET_KEY` | output of `python3 -c "import secrets;print(secrets.token_urlsafe(50))"` |
| `DJANGO_ALLOWED_HOSTS`, `SITE_URL`, `DJANGO_CSRF_TRUSTED_ORIGINS` | your domain, e.g. `sales.ajayplastic.com` / `https://sales.ajayplastic.com` |
| `DATABASE_URL` | `postgresql://flotec:<password>@localhost:5432/flotec` |
| `FIELD_ENCRYPTION_KEY` | output of `.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` — **store a copy safely**; losing it means reconnecting mailboxes |
| `ANTHROPIC_API_KEY` | from console.anthropic.com → API keys (leave empty to run without AI). Needed for buyer/OEM/country research |
| `OPENROUTER_API_KEY` | optional, from openrouter.ai → Keys. Lets e-mail drafting, reply classification and report summaries run on OpenRouter models (System settings → *AI provider*) |
| `ZOHO_*`, `GOOGLE_*`, `MS_*` | OAuth apps — see `docs/EMAIL_SETUP.md` |
| `EMAIL_*` | SMTP account used only for internal notifications (escalations, daily report) |
| `MEDIA_ROOT`, `BACKUP_DIR`, `LOG_DIR` | `/srv/flotec/media`, `/srv/flotec/backups`, `/srv/flotec/logs` |

## 5. Initialise

```bash
cd /srv/flotec/app && sudo -u flotec .venv/bin/python manage.py migrate
```

```bash
sudo -u flotec .venv/bin/python manage.py setup_initial_data
```

```bash
sudo -u flotec .venv/bin/python manage.py createsuperuser
```

```bash
sudo -u flotec .venv/bin/python manage.py collectstatic --noinput
```

```bash
sudo -u flotec .venv/bin/python manage.py check --deploy
```

Load the company profile, the 43 products, their sizes, photos and the catalogue from the FLOTEC website
repository (needs `git` access to the private website repo and Node.js: `sudo apt install -y nodejs`):

```bash
sudo -u flotec git clone https://github.com/dhvanitsantoki/flotec-website- /srv/flotec/website
```

```bash
sudo -u flotec .venv/bin/python manage.py import_website_data /srv/flotec/website --certification "ISO 9001:2015" --fact "Established Indian manufacturer now expanding into export markets"
```

Owner confirmations recorded on 2 Nov 2026: ISO 9001:2015 is held; the company does **not export yet** and
is starting export sales, so the website's export statements are skipped (add `--confirm-exports` only
once they become true).

Products are imported as *not verified*; a manager reviews them and clicks **Verify all shown** on the
Products page. Other website claims (lead-free / food-grade, export countries) are not copied until confirmed.

`setup_initial_data` creates the ISO country list (by continent, with time zones), the product
categories, the 10 e-mail template types, the Day 0/3/7/14/30 follow-up sequence and the
scheduled tasks. It is safe to run again.

Check the AI key(s) with one live request (a synthetic test e-mail; free on `openrouter/free`):

```bash
sudo -u flotec .venv/bin/python manage.py ai_selftest --provider openrouter
```

```bash
sudo -u flotec .venv/bin/python manage.py ai_selftest --provider anthropic --model claude-haiku-5-5
```

`PASS` means the key, model and structured output work. A `FAILED` line names the problem
(rejected key, no credits, privacy settings, daily limit, network).

## 6. Services

```bash
sudo cp deploy/flotec-web.service deploy/flotec-worker.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now flotec-web flotec-worker
```

The **worker** runs the scheduler and every background job (research, mailbox sync, sending,
follow-ups, reports, backups). Without it nothing automatic happens.

## 7. Nginx + HTTPS

```bash
sudo cp deploy/nginx-flotec.conf /etc/nginx/sites-available/flotec && sudo ln -s /etc/nginx/sites-available/flotec /etc/nginx/sites-enabled/
```

Edit `server_name` in that file, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

```bash
sudo certbot --nginx -d sales.ajayplastic.com
```

## 8. First-time setup in the application (owner)

1. **Settings → Company profile** – about text, website, signature, and *only verified* facts,
   capabilities, certifications and OEM services. The agents cannot write outreach until this is complete.
2. **Products** – Products → *Import CSV* (products, then sizes; templates downloadable on each import
   page) or add them one by one; upload images and catalogue PDFs; a manager clicks **Verify product data**.
3. **Price lists** – create a list per currency/Incoterm, add prices (or CSV import), **Approve**.
4. **Catalogues & files** – upload the catalogue, tick *Approved for sending*.
5. **Settings → Mailboxes** – connect the Zoho / Gmail / Outlook mailbox (OAuth).
6. **Settings → Users** – create accounts for the sales team with the right roles.
7. **Settings → System settings** – sending limits, local-hour window, research limits, AI models,
   monthly AI budget, escalation and report e-mail addresses.
8. **Country markets** – open the target countries and tick *Include in the nightly research rotation*.
9. **Two-step login** – user menu → *Two-step login* for every user; then tick *Require two-step login*
   in System settings.
10. **Automation** – keep **Mode 2** at first; click **Resume** (automation starts *paused*).

## 9. Backups and restore

* Automatic: the *Database backup* schedule (01:30 daily) writes `db-*.sql.gz` and `media-*.tar.gz`
  into `BACKUP_DIR` and keeps the newest `BACKUP_KEEP` sets.
* Copy `BACKUP_DIR` off the server daily (e.g. `rclone` to Backblaze B2 / S3 / Google Drive).
* Manual backup: `sudo -u flotec .venv/bin/python manage.py backup`
* Restore (stops the services, replaces the database, restores files):

```bash
sudo /srv/flotec/app/deploy/restore.sh /srv/flotec/backups/db-YYYYmmdd-HHMMSS.sql.gz /srv/flotec/backups/media-YYYYmmdd-HHMMSS.tar.gz
```

## 10. Updating

```bash
cd /srv/flotec/app && sudo -u flotec git pull && sudo -u flotec .venv/bin/pip install -r requirements.txt
```

```bash
sudo -u flotec .venv/bin/python manage.py migrate && sudo -u flotec .venv/bin/python manage.py collectstatic --noinput
```

```bash
sudo systemctl restart flotec-web flotec-worker
```

## 11. Docker alternative

Put `.env` in the project root (add `POSTGRES_PASSWORD=...`), then:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

```bash
docker compose -f deploy/docker-compose.yml exec web python manage.py createsuperuser
```

Put Nginx/HTTPS in front of `127.0.0.1:8000` as in step 7.

## 12. Running locally for evaluation

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
printf "DJANGO_DEBUG=1\nSITE_URL=http://localhost:8000\n" > .env && .venv/bin/python manage.py migrate && .venv/bin/python manage.py setup_initial_data
```

```bash
.venv/bin/python manage.py createsuperuser && .venv/bin/python manage.py runserver
```

In a second terminal: `.venv/bin/python manage.py run_worker`. Run the test suite with
`.venv/bin/python manage.py test`.

## 13. Troubleshooting

| Symptom | Check |
|---|---|
| Nothing happens automatically | `systemctl status flotec-worker`; Automation page shows "no job has run yet"; automation state is *Paused* on a fresh install |
| "AI not configured" badge | `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` in `.env`, then restart both services; the Automation page shows which provider each task uses and whether its key is set |
| "OpenRouter ... privacy settings" | free models need the free-endpoint option enabled at openrouter.ai/settings/privacy, or choose other models in System settings |
| "AI paused: OpenRouter daily limit reached" | the free daily request limit is used up; jobs wait and continue automatically after the reset (or add OpenRouter credits for a higher limit) |
| AI jobs fail with "budget reached" | raise the monthly budget in System settings |
| Mailbox shows *Error* | token revoked or expired → **Reconnect** on the Mailboxes page |
| E-mails stay *Approved* | outside the recipient's local business hours, daily/hourly limit reached, automation stopped, or no mailbox connected — the e-mail page shows the reason |
| Logs | `journalctl -u flotec-web -u flotec-worker`, and `LOG_DIR/app.log` |
