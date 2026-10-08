# Dashawy — Security Report

**Scope:** full backend + frontend review of the Fix Request / دشاوي platform.
**Date:** 2026-09-30
**Server:** Express 4 (CommonJS) + better-sqlite3, served as a single origin.

---

## Vulnerabilities Found & Fixed

### Critical

1. **Hardcoded JWT signing secret**
   `backend/config.js` fell back to a literal string
   (`'dashawy-change-me-in-production'`) when `JWT_SECRET` was unset. Anyone
   with the source could mint a valid super-admin token.
   *Fix:* `getJwtSecret()` now throws on boot in production when the variable is
   missing or shorter than 32 chars; development gets an explicitly labelled
   throwaway secret plus a startup warning.

2. **No rate limiting anywhere**
   `POST /api/auth/login` accepted unlimited attempts, so passwords could be
   brute-forced; `/register` could be spammed to fill the database.
   *Fix:* dependency-free limiter (`backend/middleware/rate-limit.js`) with a
   general **300 req / 15 min** budget on `/api/*` and a strict **10 / 15 min**
   bucket on `/login`, `/register`, `/register-technician`, `/change-password`.
   Auth buckets count only *failed* attempts, so a legitimate mistype is not
   punished, and each route has its own bucket so one cannot lock out another.

3. **Wildcard CORS (`Access-Control-Allow-Origin: *`)**
   Any website could read API responses from a logged-in visitor.
   *Fix:* allow-list in `backend/middleware/security-headers.js`; the site is
   same-origin, and the production domain is added via `CORS_ORIGINS`.

### High

4. **No security headers / no CSP**
   The API was missing CSP, `X-Frame-Options`, `Referrer-Policy`, HSTS,
   `Permissions-Policy` and advertised `X-Powered-By: Express`.
   *Fix:* `securityHeaders()` sets all of them, removes `X-Powered-By`, and
   ships a CSP that permits only what the site actually loads (self, Font
   Awesome, Google Fonts, `blob:`/`data:` images, inline script/style). HSTS is
   emitted only when `NODE_ENV=production`, so local http development is not
   pinned to https.

5. **Internal errors leaked to clients**
   `errorHandler` returned `err.message` for 5xx, which can expose SQL text,
   file paths and library versions.
   *Fix:* 4xx messages still pass through (they are authored by us); 5xx
   messages become a generic `Internal server error` in production while the
   full error stays in the server log. Body-too-large now answers a proper
   **413** instead of a 500.

6. **JWT algorithm not pinned**
   `jwt.verify()` was called without `algorithms`, which leaves the classic
   `alg: none` / algorithm-downgrade class of attack open.
   *Fix:* `algorithms: ['HS256']` on verify and `algorithm: 'HS256'` on sign.
   Verified: `alg:none` and a HS512 token signed with a different secret are
   both rejected; a valid HS256 token still works.

### Medium

7. **Latent DOM XSS sink in the admin login page**
   `frontend/admin/login.html` built a form error with
   `err.innerHTML = '<i …>' + msg`. Every current call site passes a hardcoded
   Arabic literal, so it was **not** exploitable — but any future dynamic
   message (e.g. a server error string) would have become an XSS sink.
   *Fix:* rebuilt with `createElement` + `createTextNode`.

8. **`.env` documented but never loaded**
   A `.env` file had no effect: nothing read it, so operators could believe
   they had configured `JWT_SECRET` when they had not.
   *Fix:* `.env.example` written, the variables are now actually read
   (`MAX_UPLOAD_MB`, `TRUST_PROXY_HOPS`, `CORS_ORIGINS`, `NODE_ENV`), and the
   file documents that Node's own `--env-file=.env` flag is needed
   (`npm start` does not load it).

### Low

9. **Upload ceilings were hardcoded** — now driven by `MAX_UPLOAD_MB`.
10. **`trust proxy` was implicit** — now explicit via `TRUST_PROXY_HOPS` so the
    rate limiter keys on the real client IP rather than a spoofable header.
11. **`/uploads` serving** — `X-Content-Type-Options: nosniff` was already set;
    verified still in place after the rewrite.

### Verified as already safe (no change needed)

| Area | Finding |
|---|---|
| **SQL injection** | 157 `db.prepare()` calls, all parameterised. The only two dynamic-SQL sites interpolate either hardcoded clause literals (`reviewsFilter`) or keys from a fixed `updates` object the handler fills itself — never `req.body` keys. Values are always bound (`?` / `@named`). |
| **Route authorisation** | All **73** routes accounted for. `admin.routes.js` guards every route (`requireRole('admin')` at the router + per-route `requirePermission`/`requireSuperAdmin`); `customer` and `technician` routers guard with `router.use(requireRole(...))`. The only unguarded ones are intentionally public: `/auth/register`, `/auth/login`, and the two `/api/public/technicians` reads. |
| **File uploads** | Magic-byte validation, server-side extension rewrite from the *detected* type, non-user-controlled filenames, per-route `fileSize` + file-count limits, and cleanup of rejected files. |
| **Secrets in logs** | 7 `console.*` calls; none prints a token, password, hash or secret. |
| **`Ui.el({html})` sink** | The generic `innerHTML` sink in `ui.js` has **zero** callers. |
| **Password storage** | bcrypt everywhere; `password_hash` is never selected into a response. |

---

## Files Modified

| File | Change |
|---|---|
| `backend/middleware/rate-limit.js` | **new** — dependency-free limiter (express-rate-limit-compatible API) |
| `backend/middleware/security-headers.js` | **new** — security headers + CSP + CORS allow-list (helmet/cors-compatible API) |
| `backend/app.js` | `trust proxy`, `x-powered-by` off, 100 kb body caps, headers, CORS, `apiLimiter` |
| `backend/config.js` | `getJwtSecret()`, `IS_PRODUCTION`, `ALLOWED_ORIGINS`, `CORS_ORIGINS` |
| `backend/middleware/auth.js` | pin `algorithms: ['HS256']`; documented DB-re-read of role/flags |
| `backend/routes/auth.routes.js` | strict limiter on the 4 auth endpoints; `algorithm: 'HS256'` on sign |
| `backend/utils/errors.js` | no 5xx leakage in production; 413 handling |
| `backend/middleware/upload.js` | limits driven by `MAX_UPLOAD_MB` |
| `frontend/admin/login.html` | XSS sink rebuilt with DOM nodes |
| `package.json` | `audit` / `audit:fix` scripts |
| `.env.example` | **new** — documented variables |
| `SECURITY.md`, `SECURITY_REPORT.md` | **new** — this report and the policy |

**Not installed:** `helmet`, `express-rate-limit`, `cors`, `dotenv` were *not*
added, because the audit runs under a hard "do not modify `node_modules`"
constraint. The two new middleware files are drop-in swappable: replace them
with the real packages in `backend/app.js` and nothing else changes.

---

## Environment Variables Required

| Variable | Required | Default | Notes |
|---|---|---|---|
| `JWT_SECRET` | **yes in production** | dev-only throwaway | ≥32 chars; boot fails otherwise |
| `NODE_ENV` | recommended | `development` | `production` enables HSTS and hides 5xx messages |
| `PORT` | no | `3000` | |
| `CORS_ORIGINS` | in production | localhost only | comma-separated allow-list |
| `TRUST_PROXY_HOPS` | behind a proxy | `1` | must match the real proxy depth |
| `DATABASE_PATH` | no | `./database/fixrequest.db` | |
| `JWT_EXPIRES_IN` | no | `7d` | |
| `MAX_UPLOAD_MB` | no | `5` | profile images stay capped at 3 MB |

Load with Node's built-in flag — `npm start` does **not** read `.env`:
```bash
node --env-file=.env backend/server.js
```

---

## Known Limitations

- **No 2FA / MFA** on any account, including the super admin.
- **No password reset self-service.** Recovery is an out-of-band call to the
  admin, who resets it from `/admin/admins`; the user is then forced to change
  it on first login.
- **Rate-limit counters are in-process.** Behind a load balancer each instance
  counts independently. Move to `express-rate-limit` with a Redis store before
  scaling horizontally.
- **No account lockout** — a blocked IP just waits out the 15-minute window.
- **No CSRF tokens.** Safe today because auth is a `Bearer` header (not a
  cookie), so cross-site requests cannot be authenticated. Revisit if the
  session ever moves to cookies.
- **CSP still allows `'unsafe-inline'`** for scripts and styles because every
  page ships an inline `<script>`. Removing it needs a nonce/template pass.
- **SQLite, single-writer.** Fine for this deployment; not a horizontal-scale
  story, and all writes are synchronous on the event loop.
- **`file-type@16` advisory** (moderate, DoS in the ASF parser). Fixing needs
  a semver-major bump to v21, which is ESM-only and would break the
  CommonJS `require` in `upload.js` — deferred, not silently ignored.
- **No audit log** of admin actions (who changed a permission, deleted a
  customer). High-value next step.
- **CSP was verified by inspection, not in a real browser** — no headless
  browser is available in this environment.

---

## How to Report a Vulnerability

Report privately to the maintainer — do **not** open a public issue. Include:
the affected endpoint, the request that reproduces it, the impact, and any
suggested fix. Please do not run automated scanners against the live
deployment. Expect an acknowledgement within 3 working days.

**Do not include real customer data, documents or credentials in a report** —
use synthetic records.
