# Hosting on Vercel

The system runs on Vercel as one Python (Django) function plus a cron job; a VPS installation
(`docs/INSTALL.md`) remains the alternative without platform limits.

| Part | On Vercel |
|---|---|
| Web app | `config/wsgi.py` as a Vercel Function (region `bom1`, Mumbai), up to 800 s per request |
| Background worker | **Vercel Cron** calls `/cron/tick/` every minute (needs the **Pro** plan); each call schedules due tasks and runs queued jobs for up to 4 minutes, at most 2 jobs at a time |
| Nightly pipeline | research runs are queued as separate jobs; drafts, follow-ups and the daily report run 60 minutes later |
| Database | PostgreSQL from the Vercel Marketplace (**Neon**), connected to the project (`DATABASE_URL`) |
| Uploaded files | stored in the database (`MEDIA_STORAGE=database`), served only to signed-in users |
| Static files | served by WhiteNoise from the source folders |
| Backups | the scheduled file backup is disabled; use Neon's point-in-time restore |
| Build | `python manage.py vercel_build` (from `pyproject.toml`): migrations, initial data, first admin account |

Limits to know:
* Vercel accepts and returns at most ~4.5 MB per request: files larger than ~4.4 MB cannot be uploaded
  or opened in the browser (they can still be e-mailed). Upload compressed catalogues (< 4 MB).
* Requests from buyers (unsubscribe links) and OAuth callbacks must reach the app, so **Vercel
  Authentication must not protect the production domain** (Deployment Protection -> "Only Preview Deployments" or off).
  The app has its own sign-in and two-step login.

## Steps (owner, in the Vercel dashboard)

1. **Plan**: Settings -> Billing -> upgrade the team to **Pro** (per-minute cron and 800 s functions).
2. **Project**: Add New -> Project -> import the GitHub repository `agentic-ai-` (allow Vercel access to it
   if asked) -> project name `flotec-sales` -> Framework preset: Django (auto-detected).
3. **Environment Variables** (before *Deploy*): paste the block from the setup sheet - `DJANGO_SECRET_KEY`,
   `FIELD_ENCRYPTION_KEY`, `CRON_SECRET`, `OWNER_INITIAL_PASSWORD` - and add `OPENROUTER_API_KEY` /
   `ANTHROPIC_API_KEY`. Then **Deploy** (the first build reports that the database is missing - expected).
4. **Database**: project -> Storage -> Create Database -> **Neon** (Free) -> region Mumbai/Singapore ->
   connect to `flotec-sales` (all environments). This adds `DATABASE_URL`.
5. **Deployment Protection**: project -> Settings -> Deployment Protection -> Vercel Authentication ->
   "Only Preview Deployments" (or Disabled) -> Save.
6. **Redeploy**: Deployments -> latest -> Redeploy. The build creates the tables and the `owner` account.
7. Open `https://flotec-sales.vercel.app` -> sign in as `owner` with `OWNER_INITIAL_PASSWORD` ->
   change the password (user menu) -> set up two-step login.
8. **Data**: Products -> Import CSV -> *Website data (ZIP)* -> upload `flotec-website-data.zip`
   (prepared with `manage.py import_website_data <website repo> --export-bundle website-data.zip`).
9. **AI check**: Automation -> *Test AI*. Then System settings, mailboxes, Resume (see `docs/INSTALL.md` step 8).

## Troubleshooting

| Symptom | Check |
|---|---|
| Build: "DJANGO_SECRET_KEY must be set" | environment variables missing for *Production*; add them and redeploy |
| Build fails on `crons` / `maxDuration` | the team is still on Hobby - upgrade to Pro |
| Automation page: "no job has run yet" | `CRON_SECRET` set? Pro plan? project -> Settings -> Cron Jobs shows `/cron/tick/` |
| `Bad Request (400)` on a custom domain | add the domain to `DJANGO_ALLOWED_HOSTS` and set `SITE_URL=https://<domain>`, redeploy |
| Buyers see a Vercel login on unsubscribe links | Deployment Protection still covers production (step 5) |
| Logs | project -> Logs (runtime) and Deployments -> Build Logs |
