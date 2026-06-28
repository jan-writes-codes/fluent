# Deploying The Green Pencil

This project runs as **the same code deployed twice** — a **test** system and a
**production** system — each pointed at its **own database** via environment
variables. Nothing is copied between them: "test vs prod" is purely which env
vars the process starts with.

```
                same code / same image
                /                      \
        TEST instance              PROD instance
   DJANGO_DEBUG=false          DJANGO_DEBUG=false
   own test database           own prod database
   seeded with demo data       EMPTY → built in the GUI
   test.thegreenpencil.at      thegreenpencil.at
```

## What makes it deployable

| Piece | Why |
| ----- | --- |
| **gunicorn** | Production WSGI app server. Replaces `runserver` (which is dev-only). |
| **whitenoise** | Serves static files (logo, fonts, JS) when `DEBUG=false`, since Django stops serving them itself. |
| **dj-database-url** | Reads `DATABASE_URL` so each environment uses a different database with no code change. |
| **psycopg[binary]** | PostgreSQL driver. Only used when `DATABASE_URL` points at Postgres. |

## Environment variables

Set these per environment (e.g. in a `.env` file, systemd unit, or your PaaS
dashboard — `.env` files are gitignored):

| Variable | Required when | Example |
| -------- | ------------- | ------- |
| `DJANGO_DEBUG` | always | `false` in test **and** prod |
| `DJANGO_SECRET_KEY` | `DEBUG=false` | a long random string — **different per environment** |
| `DJANGO_ALLOWED_HOSTS` | `DEBUG=false` | `thegreenpencil.at,www.thegreenpencil.at` |
| `DATABASE_URL` | to use a non-default DB | `postgres://user:pass@host:5432/greenpencil_prod` |
| `STRIPE_SECRET_KEY` | to enable self-checkout | `sk_live_…` (or `sk_test_…`) |
| `STRIPE_PUBLISHABLE_KEY` | with Stripe | `pk_live_…` (or `pk_test_…`) |
| `STRIPE_WEBHOOK_SECRET` | with Stripe webhook | `whsec_…` |
| `RESEND_API_KEY` | to send real e-mail | `re_…` |
| `DEFAULT_FROM_EMAIL` | with e-mail | `The Green Pencil <hallo@thegreenpencil.at>` |
| `EMAIL_REPLY_TO` | recommended | `davit@thegreenpencil.at` |
| `TUTOR_NOTIFY_EMAIL` | to alert the tutor | `davit@thegreenpencil.at` |
| `EMAIL_ASYNC` | optional | `true` (send off the request thread; default) |
| `SITE_URL` | optional | `https://thegreenpencil.at` (links in e-mails) |

### Stripe credit top-ups (optional)

If `STRIPE_SECRET_KEY` is unset, the app runs unchanged and students buy credits
the existing way (they message the tutor, who adds the credits). Setting the
Stripe keys turns on self-service Stripe Checkout in the credit-top-up panel.

Point a Stripe webhook at `https://<host>/api/stripe/webhook/` for the
`checkout.session.completed` event and put its signing secret in
`STRIPE_WEBHOOK_SECRET`. The webhook is the source of truth; the app also
re-confirms the session when the student returns, so credits are granted exactly
once even if the webhook is slow or not yet configured.

### Transactional e-mail (Resend, optional)

Booking confirmations and tutor alerts go out through [Resend](https://resend.com)
via `django-anymail`. **If `RESEND_API_KEY` is unset the app runs unchanged** and
e-mail is printed to the console instead of sent — nothing breaks.

To turn it on:

1. **Create a Resend account** and add `thegreenpencil.at` as a sending domain.
   Choose the **EU region** (data residency) when creating it.
2. **Add the DNS records Resend shows you** to `thegreenpencil.at` — the DKIM
   `CNAME`(s) and the SPF `TXT`. Sending from a subdomain (e.g.
   `send.thegreenpencil.at`) keeps the root domain's reputation insulated.
3. **Add a DMARC record** so mailbox providers trust the domain:
   `_dmarc.thegreenpencil.at  TXT  "v=DMARC1; p=quarantine; rua=mailto:dmarc@thegreenpencil.at"`
4. **Set the env vars**: `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL` (on the verified
   domain), `EMAIL_REPLY_TO` (the tutor's inbox), and `TUTOR_NOTIFY_EMAIL`.
5. (Optional) Point a Resend **webhook** at Anymail's tracking URL to record
   bounces/complaints — wire `anymail.urls` if/when you want delivery events.

**Async delivery.** By default (`EMAIL_ASYNC=true`) mail is sent on a background
thread so a booking never blocks on the ESP. A thread is enough for a single
studio; for durable, restart-surviving retries swap `core.emails.queue_email`
for a queue (Django Q2 over the existing Postgres needs no Redis, just one extra
worker process) — the call sites don't change.

`DATABASE_URL` formats:
- SQLite: `sqlite:////absolute/path/to/db.sqlite3`
- Postgres: `postgres://user:pass@host:5432/dbname`

If `DATABASE_URL` is unset it falls back to a local `db.sqlite3` (handy for dev).

> ⚠️ With `DJANGO_DEBUG=false` the app forces HTTPS (`SECURE_SSL_REDIRECT`, HSTS).
> Each environment must sit behind TLS (nginx/Caddy or your PaaS), or browsers
> will hit a redirect loop.

## Database choice

- **PostgreSQL** for the real production system — proper backups, concurrent-safe.
  Create one database per environment (`greenpencil_test`, `greenpencil_prod`).
- **SQLite** is fine for the test system (or even prod at this scale: one tutor,
  ~80 students). Just give each environment its own file.

## Bringing up an environment

Common steps (run from the project root, in the environment's venv):

```bash
pip install -r requirements.txt
python manage.py migrate                # create the schema in THIS env's database
python manage.py collectstatic --noinput
```

Then diverge:

### Test — load demo data so you can click around

```bash
python manage.py seed     # demo students/tutor/admin (all password: "password")
```

### Production — start EMPTY, then build it in the GUI

```bash
python manage.py createadmin            # creates ONE admin (prompts for email/pw)
# ...or non-interactively:
python manage.py createadmin --email you@thegreenpencil.at --password 'choose-a-strong-one' --name "Your Name"
```

**Do not run `seed` in production.** Skipping it is what keeps prod clean. After
`createadmin`, open `https://thegreenpencil.at/login/`, log in as that admin (you
land in the admin view at `/app/`), and create the tutor and real students there.

## Running the server

Replace `runserver` with gunicorn:

```bash
gunicorn fluent.wsgi --bind 0.0.0.0:8000 --workers 3
```

On a PaaS, use that as the **start command**, and
`python manage.py migrate && python manage.py collectstatic --noinput` as the
**release/build step**.

## Hosting shapes

**A) One small VPS (most control)**
- nginx terminates TLS and proxies to gunicorn.
- Two **systemd services**, each running `gunicorn fluent.wsgi` with its own
  `.env` file (different `DATABASE_URL`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`)
  on different ports/sockets.
- Two databases; nginx routes `test.thegreenpencil.at` vs the apex domain.
- Put `MEDIA_ROOT` (lesson-file uploads + base64 photos) on a backed-up disk.

**B) A PaaS — Render / Railway / Fly.io (least ops)**
- Create **two services** from the same repo, each with its own env group and a
  managed Postgres database.
- Start command `gunicorn fluent.wsgi`; release step runs `migrate` +
  `collectstatic`.

For a single-tutor business, **(B)** is the lower-maintenance choice.

## Quick checklist per environment

- [ ] `DJANGO_SECRET_KEY` set (unique), `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS` set
- [ ] `DATABASE_URL` points at this environment's own database
- [ ] TLS/HTTPS in front of gunicorn
- [ ] `migrate` + `collectstatic` run
- [ ] test → `seed`; prod → `createadmin` only
- [ ] `MEDIA_ROOT` on persistent, backed-up storage
