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

Django serves the files in `frontend/` directly:

| URL | Page |
| --- | --- |
| `/` | Public homepage |
| `/auth.html` | Customer/technician sign-in and registration |
| `/customer/dashboard.html` | Customer dashboard |
| `/technician/dashboard.html` | Technician dashboard |
| `/admin` or `/admin/login.html` | Product admin login |
| `/admin/dashboard` | Product admin dashboard |
| `/django-admin/` | Django's built-in admin site |

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
| `POST` | `/api/technician/requests/<id>/accept` | Technician | Accept a request |
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
| `DJANGO_SECRET_KEY` | Development-only fallback | Django cryptographic signing key. Set a strong value in production. |
| `DJANGO_DEBUG` | `1` | `1`/`true` enables debug mode; use `0`/`false` in production. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated hostnames Django accepts. |
| `JWT_SECRET` | Falls back to `DJANGO_SECRET_KEY` | Signs and verifies JWT access tokens. Set a strong, stable value in production. |
| `JWT_EXPIRES_IN` | `7d` | Access-token lifetime (`30m`, `12h`, `7d`, etc.). |
| `JWT_REFRESH_EXPIRES_IN` | `7d` | Refresh-token lifetime. |

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
- **CORS** — permissive in development so split-port frontends can call the API.
  Tighten this to an explicit allow-list before production.
- **Secrets** — `.env` is git-ignored. Never commit real keys or credentials.
- **Database** — back up `database/fixrequest.db`; it holds accounts, identity
  documents, and payment records.

## Project layout

```text
dashawy_backend/    Django project: settings, URL configuration, WSGI/ASGI
core/               Django app: legacy schema models, API views, migrations,
                    permissions, serializers, and management commands
frontend/           HTML pages plus CSS, JS, images, and service worker
database/
  schema.sql        Canonical SQLite schema
  fixrequest.db     Local SQLite database (git-ignored)
uploads/            Uploaded documents and profile images (git-ignored)
manage.py           Django entry point
requirements.txt    Pinned Python dependencies
```

## Deployment

1. Set `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SECRET_KEY`, and
   `JWT_SECRET` in the deployment environment.
2. Run `python manage.py migrate --fake-initial` against the production
   database.
3. Run the app with a WSGI server pointing at
   `dashawy_backend.wsgi:application` (for example, gunicorn or uWSGI), behind
   HTTPS.
4. Serve `frontend/` and static assets through the reverse proxy. Django's
   development server handles them automatically, but production should not use
   `runserver`.
5. Back up the SQLite database regularly. For heavier write traffic, plan a
   move to a server database.
