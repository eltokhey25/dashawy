# Dashawy (Fix Request)

A service platform connecting customers with technicians in Alexandria: request
broadcast, first-accept wins, doorstep Start PIN handshake, two-sided cash
confirmation, mandatory customer reviews, per-request chat, and Web Push.

---

## Requirements

- Node.js **18+** (developed on 20.x)
- SQLite — created automatically on first boot, no server to install

No build step. No bundler. The frontend is plain HTML/CSS/JS served by the same
Express process that serves the API.

## Running it

```bash
npm install
cp .env.example .env      # then fill in JWT_SECRET
node --env-file=.env backend/server.js
```

Open <http://localhost:3000>.

> `npm start` does **not** load `.env` (Node's `--env-file` flag is required), so
> use the explicit command above. Production additionally requires `JWT_SECRET`
> — the server refuses to boot without it when `NODE_ENV=production`.

## Tests / checks

```bash
npm run format:check   # prettier
npm run audit          # dependency advisories
npm run seed           # optional demo data
```

## Deploying

1. **Environment.** Copy `.env.example` to your secret store and set, at minimum:
   - `JWT_SECRET` — 32+ random characters. Generate with
     `node -e "console.log(require('crypto').randomBytes(48).toString('hex'))"`
   - `NODE_ENV=production`
   - `CORS_ORIGINS` — comma-separated origins, if the frontend and API are on
     different domains
2. **TLS.** Terminate HTTPS at nginx/Cloudflare. The app sets
   `Strict-Transport-Security` automatically when `NODE_ENV=production`.
3. **Proxy.** Set `TRUST_PROXY_HOPS` to the number of trusted hops in front of
   the app (1 behind a single nginx/Cloudflare hop). Rate limiting keys on
   `req.ip`, so this must be correct or every client shares one bucket.
4. **Run under a supervisor** (systemd, pm2, Docker) so the process restarts on
   crash. It handles `SIGTERM`/`SIGINT` gracefully: stops accepting, lets
   in-flight requests finish, closes SQLite, exits.
5. **Back up `database/fixrequest.db`** — it holds accounts, identity documents
   and money movements.
6. **Scaling beyond one instance:** the rate limiter keeps counters in process
   memory, so each instance counts independently. Move them to Redis (swap
   `backend/middleware/rate-limit.js` for `express-rate-limit` with a Redis store)
   before running more than one.

### First admin account

Admins are created by seeding, not self-registered — see `database/seed.js`.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `JWT_SECRET` | **yes in production** | dev-only fallback | Signs and verifies session tokens. Must be 32+ chars. |
| `NODE_ENV` | recommended | — | `production` enables HSTS, masks 5xx messages, and enforces the secret. |
| `PORT` | no | `3000` | HTTP port. |
| `JWT_EXPIRES_IN` | no | `7d` | Session token lifetime. |
| `DATABASE_PATH` | no | `./database/fixrequest.db` | SQLite file location. |
| `CORS_ORIGINS` | no | localhost only | Comma-separated allowed origins. |
| `TRUST_PROXY_HOPS` | no | `1` | Trusted proxy hops, for correct client IPs. |
| `BROADCAST_RADIUS_KM` | no | `10` | Deployment default radius; the admin setting wins once it exists. |
| `BROADCAST_TIMEOUT_HOURS` | no | `24` | How long an offer stays open. |
| `QUEUE_RECHECK_MINUTES` | no | `5` | Waiting-queue re-offer interval. |
| `QUEUE_MAX_HOURS` | no | `24` | When a queued request is cancelled. |
| `CASH_CONFIRM_TIMEOUT_HOURS` | no | `24` | How long a technician's cash report waits for the customer. |
| `MAX_UPLOAD_MB` | no | `5` | Per-file upload ceiling (profile images use 3). |
| `VAPID_PUBLIC_KEY` | no | — | Web Push public key. Without these three, push is simply off. |
| `VAPID_PRIVATE_KEY` | no | — | Web Push private key. **Keep it out of the repo.** |
| `VAPID_SUBJECT` | no | — | `mailto:` or `https:` contact for push. |

---

## Security notes

Read `SECURITY_AUDIT_REPORT.md` for the full audit (what was found, what was
fixed, what was verified already-safe). The controls that matter:

**Passwords** — minimum 8 characters with at least one letter and one digit,
enforced by a single shared validator (`isStrongPassword`) applied at every site
that sets a password. Hashed with bcrypt at cost 12.

**Brute force** — two independent brakes. A per-IP limiter (10 failures per 15
minutes, successes refunded) and a **per-account** lockout
(`users.failed_login_attempts` / `login_locked_until`): 5 failures locks the
account for 15 minutes. The per-account half is what stops a botnet, which would
otherwise get a fresh per-IP budget against one victim forever. Both answer with
the same generic Arabic error, and a lockout is only ever recorded for an
account that exists — so the endpoint cannot be used to enumerate addresses or to
lock a real user out.

**Sessions** — HS256 pinned on both sign and verify, so `alg: none` and algorithm
downgrades are rejected. Role, super-admin status and permissions are re-read
from the database on every request, so a downgraded or blocked account loses
access immediately rather than at token expiry.

**Uploads** — validated by magic bytes, not by the client-supplied MIME type, and
the stored extension is **derived from the detected type**, so a file cannot
arrive named `shell.php`. Documents stage in `pending/` and are moved inside the
registration transaction. `/uploads` is served with `nosniff`, and identity
documents are never publicly reachable — only an admin route streams them, with
a path-traversal check.

**SQL** — every query is parameterized. The few dynamically built SQL fragments
(administrator filters, `SET` column lists) construct **column and clause names
from internal allow-lists** only; all values are bound parameters. The 16
`db.exec` calls interpolate compile-time constants in schema migrations.

**Chat** — only the owning customer and the assigned technician can read or write
a thread. Sending is refused server-side unless the job is `assigned` or
`in_progress`; the UI lock is convenience, not the control. Sends are rate
limited to 30/minute **per user**, because every message also fans out to a Web
Push notification and flooding would be an amplification attack on the other
party's device.

**Start PIN** — 4 digits, shown only to the customer, cleared from the database
the moment it is spent, and never included in any technician-facing payload. The
legacy PIN-less start route is retired, so the handshake cannot be bypassed. It
is stored in plaintext **by necessity**: the customer has to read it, so a
one-way hash would provide no protection.

**Secrets** — no credentials in source. `.env` is git-ignored; `.env.example`
carries placeholders only.

## Project layout

```
backend/
  app.js            middleware chain, routing table, error handler
  server.js         bootstrap, workers, graceful shutdown
  config.js         env parsing + JWT secret policy
  db.js             schema init and migrations
  middleware/       auth, rate limiting, security headers, uploads
  routes/           auth, customer, technician, admin, public, chat, push
  services/         broadcast, assignment, pricing, payments, queue, push
  utils/            errors, validation, logger, geo
frontend/
  *.html            pages (customer, technician, admin)
  js/               api, auth, ui, push, force-review
  css/style.css
  sw.js             service worker (offline shell + push)
database/
  schema.sql        canonical schema
  fixrequest.db     SQLite database
uploads/            identity documents and portfolio images
```

## Known limitations

- Rate-limit counters are in-process; multi-instance needs Redis.
- Chat is polled every 3 seconds. At scale this wants a WebSocket or SSE layer.
- CSP uses `'unsafe-inline'` for scripts and styles, which every page currently
  requires. A nonce-based policy would need templating across the frontend.