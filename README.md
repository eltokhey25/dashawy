# Dashawy (Fix Request)

A service platform connecting customers with technicians in Alexandria: request
broadcast, first-accept wins, doorstep Start PIN handshake, two-sided cash
confirmation, mandatory customer reviews, per-request chat, and Web Push.

The application is powered by **Django 5.2** and serves both the REST API and
the existing HTML/CSS/JS frontend from a single process.

---

## Requirements

- Python **3.10+** (developed on 3.12)
- SQLite — a file database, no server to install
- `pip` and `venv`
- The system `libmagic` library used by `python-magic` for upload validation
  (for example `apt install libmagic1` on Debian/Ubuntu)

There is no build step and no bundler. The frontend is plain HTML, CSS, and
JavaScript served directly by Django.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set DJANGO_SECRET_KEY and JWT_SECRET.

python manage.py migrate --fake-initial
python manage.py runserver
```

Open <http://localhost:8000>.

`--fake-initial` works for both setups: it fakes the initial migration when the
legacy SQLite tables already exist, and applies it normally when the database is
new.

### Useful commands

```bash
python manage.py check                 # configuration check
python manage.py test core             # API and frontend-serving tests
python manage.py showmigrations        # migration status
python manage.py reset_admin_password  # recovery helper (see below)
```

## Frontend and admin

Django renders the pages in `core/templates/frontend/` and serves the static
assets from `frontend/`:

| URL | Page |
| --- | --- |
| `/` | Public homepage |
| `/order.html` | Guest order form (no account required) |
| `/track.html?token=…` | Private guest-order tracking and cancellation |
| `/auth.html` | Customer/technician sign-in and registration |
| `/customer/dashboard.html` | Customer dashboard |
| `/technician/dashboard.html` | Technician dashboard |
| `/admin` or `/admin/login.html` | Product admin login |
| `/admin/dashboard` | Product admin dashboard |
| `/django-admin/` | Django's built-in admin site |
| `/terms.html` | Terms of use |
| `/privacy.html` | Privacy policy |
| `/refund-cancellation.html` | Cancellation and refund policy |

The product admin pages use extensionless routes such as `/admin/dashboard`;
Django maps those to the matching `.html` files. Django's own admin is mounted
at `/django-admin/` so it does not conflict with the product admin.

## API overview

All API routes live under `/api/`. Authentication uses a Bearer JWT returned as
`token` by the login endpoint. Errors are returned as `{"error": "..."}`.

### Authentication

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/auth/login` | Public | Email or phone + password |
| `POST` | `/api/auth/register` | Public | Customer self-registration |
| `GET` | `/api/auth/me` | Bearer | Current user and profile |
| `POST` | `/api/auth/google` | Public | Sign in with a verified Google ID token |
| `GET` | `/api/auth/google/config` | Public | Whether Google Sign-In is configured |
| `POST` | `/api/auth/complete-profile` | Bearer | Add the Egyptian mobile number Google accounts lack |

### Public and guest

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/public/settings` | Public | Site name, contact details and social links |
| `GET` | `/api/public/config` | Public | Full public configuration, including `SITE_URL` |
| `GET` | `/api/public/stats` | Public | Real counters, hidden until the sample size is meaningful |
| `GET` | `/api/public/reviews` | Public | Published reviews (completed requests only) |
| `GET` | `/api/specialties` | Public | Service types with prices from the database |
| `GET` | `/api/alexandria-areas` | Public | Covered neighbourhoods |
| `POST` | `/api/consent` | Bearer | Record terms/privacy consent with a policy version |
| `POST` | `/api/guest/requests` | Public | Create an account-free order; returns a tracking token once |
| `GET` | `/api/guest/requests/<token>` | Token | Status of a guest order |
| `POST` | `/api/guest/requests/<token>/cancel` | Token | Cancel before work starts |
| `GET` | `/api/guest/requests/<token>/photo` | Token | The guest's own uploaded photo |
| `POST` | `/api/customer/guest-orders/link` | Customer | Link a guest order with token + matching phone |

### Customer

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/customer/requests` | Customer | List the customer's requests |
| `POST` | `/api/customer/requests` | Customer | Create a request |
| `GET` | `/api/customer/requests/<id>` | Customer | Request details |

### Technician

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/technician/requests` | Technician | Available/assigned requests |
| `POST` | `/api/technician/requests/<id>/start` | Technician | Start a job with the customer's arrival code |
| `POST` | `/api/technician/requests/<id>/accept` | Technician | Deprecated alias for `/start`; still requires the code |
| `POST` | `/api/technician/requests/<id>/reject` | Technician | Reject and reassign |

### Admin dashboard

Admin endpoints require `role='admin'` or `is_super_admin=True`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/admin/stats` | Dashboard counters and chart data |
| `GET` | `/api/admin/requests` | All requests; supports `status` and `specialty` filters |
| `GET` | `/api/admin/technicians` | Technicians with account details and availability |
| `POST` | `/api/admin/technicians/<id>/verify` | Toggle document verification |
| `GET` | `/api/admin/customers` | Customers with account details and request counts |
| `POST` | `/api/admin/requests/<id>/cancel` | Force-cancel a request |

## Environment variables

Django loads `.env` from the project root. Copy `.env.example` and set the
values for your environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | None (required in production) | Django cryptographic signing key. Startup fails when debug is off and this is unset. |
| `DJANGO_DEBUG` | `False` | Exactly `True` enables debug mode; use `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated hostnames Django accepts. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:8000` | Comma-separated exact browser origins allowed to call the API. Wildcards are disabled. |
| `CSRF_TRUSTED_ORIGINS` | Empty | Comma-separated HTTPS origins trusted for CSRF-protected forms. |
| `JWT_SECRET` | Falls back to `DJANGO_SECRET_KEY` | Signs and verifies JWT access tokens. Set a strong, stable value in production. |
| `JWT_EXPIRES_IN` | `7d` | Access-token lifetime (`30m`, `12h`, `7d`, etc.). |
| `JWT_REFRESH_EXPIRES_IN` | `7d` | Refresh-token lifetime. |
| `SITE_URL` | `PYTHONANYWHERE_DOMAIN` or `http://localhost:8000` | Canonical origin used by canonical tags, Open Graph, the sitemap, robots.txt, the manifest and guest tracking links. |
| `BUSINESS_PHONE` | Settings table | Public phone number (falls back to the admin-editable `settings` row). |
| `BUSINESS_WHATSAPP` | Settings table / phone | WhatsApp number in international digits, used to build `wa.me` links. |
| `BUSINESS_EMAIL` | Settings table | Public support e-mail. |
| `PRIVACY_EMAIL` | `BUSINESS_EMAIL` | Data-protection contact shown on the legal pages. |
| `SOCIAL_FACEBOOK` / `SOCIAL_INSTAGRAM` / `SOCIAL_TIKTOK` | Empty | Social profile URLs. Empty values hide the icon instead of rendering a dead link. |
| `COMMISSION_RATE` | `20` (or settings table) | Platform commission percentage applied to new requests. |
| `GOOGLE_CLIENT_ID` | Empty | OAuth 2.0 Web client ID for Google Sign-In. When empty the button is hidden and the endpoint returns 503. |

Generate a production Django key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

## First admin / password recovery

Admin accounts are not self-registered. If you already have an admin account,
sign in at `/admin/login.html`. To reset its password from the command line:

```bash
python manage.py reset_admin_password \
  --email admin@example.com \
  --password 'NewPass123'
```

The command writes a bcrypt hash compatible with the existing `users` table and
clears any failed-login lock. When `DJANGO_DEBUG=0`, `--password` is required.

Django's built-in admin at `/django-admin/` uses Django's separate auth tables.
Create a Django superuser only if you need that site:

```bash
python manage.py createsuperuser
```

## Security notes

- **Passwords** — legacy bcrypt hashes are verified directly, so existing
  accounts continue to work without a reset. New passwords are hashed with
  bcrypt at the same cost.
- **Sessions** — JWTs use HS256. Role, super-admin status, permissions, and the
  blocked flag are re-read from the database on each request, so account changes
  take effect immediately.
- **Authorization** — customer, technician, and admin endpoints use explicit
  role checks. Admin endpoints accept either the admin role or the super-admin
  flag.
- **CORS / CSRF** — CORS accepts only explicit `CORS_ALLOWED_ORIGINS`; there is
  no wildcard fallback. `CsrfViewMiddleware` stays enabled, and
  `CSRF_TRUSTED_ORIGINS` must list any HTTPS origin used for cookie-authenticated
  forms.
- **Brute force** — `django-axes` locks an identifier/IP after five failed
  sign-ins for 15 minutes. JWTs are still revalidated against the database on
  every request.
- **Uploads** — technician documents are validated by magic bytes (JPEG, PNG,
  or PDF only) and stored under the private `uploads/technicians/` tree. Only
  `uploads/technicians/profiles/` and `uploads/technicians/portfolio/` are served
  publicly; identity documents are streamed by the admin-only
  `/api/admin/technicians/<id>/document/<type>` endpoint.
- **Client IPs** — rate limiting uses `REMOTE_ADDR`, or the right-most
  `X-Forwarded-For` entry when the direct peer is a private/loopback proxy
  (PythonAnywhere). A client cannot spoof the value when connecting directly.
- **Secrets** — `.env` is git-ignored. Never commit real keys or credentials.
- **Database** — back up `database/fixrequest.db`; it holds accounts, identity
  documents, and payment records. Run `scripts/backup.sh` from a daily scheduled
  task; it uses SQLite's online backup API, verifies integrity, gzips the file,
  and keeps 14 days.

## Project layout

```text
dashawy_backend/    Django project: settings, URL configuration, WSGI/ASGI
core/               Django app: legacy schema models, API views, migrations,
                    permissions, serializers, and management commands
core/templates/frontend/
                    Django-rendered pages and partials (never served statically)
frontend/           Static assets only: CSS, JS, images, and service worker
database/
  schema.sql        Canonical SQLite schema
  fixrequest.db     Local SQLite database (git-ignored)
uploads/            Uploaded documents and profile images (git-ignored)
manage.py           Django entry point
requirements.txt    Pinned Python dependencies
```

## Deployment

1. Set `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SECRET_KEY`,
   `JWT_SECRET`, `SITE_URL`, `CORS_ALLOWED_ORIGINS`, and
   `CSRF_TRUSTED_ORIGINS` in the deployment environment. The new public
   variables (`BUSINESS_*`, `SOCIAL_*`, `COMMISSION_RATE`, `GOOGLE_CLIENT_ID`)
   are documented above.
2. Back up the database, then run `python manage.py migrate` (this applies
   `core.0002`, which only adds guest-order columns and the rate-limit table).
3. Run the app with a WSGI server pointing at
   `dashawy_backend.wsgi:application` (for example, gunicorn or uWSGI), behind
   HTTPS.
4. Django renders the HTML pages itself, so no separate frontend web server is
   required; static assets are still served from `frontend/`.
5. Schedule `scripts/backup.sh` (for example, a PythonAnywhere daily task) and
   download a copy of `backups/` off the host regularly.
6. After a domain change, update `SITE_URL`, the Google Cloud Console
   authorised JavaScript origins, and the accounts in Search Console / Bing.
7. Back up the SQLite database regularly. For heavier write traffic, plan a
   move to a server database.
