# Security Policy

Dashawy (دشاوي) — technician service requests for home maintenance.

## Supported Versions

Only the version currently running on `main` receives security fixes.

| Version | Supported |
|---|---|
| current `main` | yes |
| older commits / forks | no |

---

## Reporting a Vulnerability

**Please report privately — do not open a public issue or pull request.**

Send the details directly to the maintainer (private channel) with:

1. **What** is affected (endpoint, page, file).
2. **How** to reproduce it — the exact request or steps.
3. **What** the impact is (data exposure, privilege escalation, account
   takeover, denial of service, …).
4. Any **suggested fix**, if you have one.

Please:

- use synthetic data only — no real customer records, documents, phone
  numbers or credentials;
- do not run automated scanners against the live deployment;
- give us a reasonable window to ship a fix before disclosing publicly.

**Expected response:** acknowledgement within **3 working days**, with a
remediation plan within **14 days**. Severity Critical (active data breach or
remote takeover) is triaged same-day.

Vulnerabilities already accepted as known limitations — no 2FA, no password
reset self-service, no account lockout — are documented in
[`SECURITY_REPORT.md`](./SECURITY_REPORT.md#known-limitations).

---

## Security Measures In Place

| Area | Measure |
|---|---|
| Authentication | bcrypt password hashing, signed JWTs, HS256 pinned on both sign and verify, every request re-reads `role` / `is_super_admin` / `blocked` from the database rather than trusting the token |
| Authorisation | Three-level RBAC — `requireAuth` → `requireRole` → `requirePermission` / `requireSuperAdmin`, applied per route |
| Brute force | 10 failed attempts / 15 min on login, register, register-technician and change-password (failures only, so a legitimate mistype is not punished); 300 requests / 15 min on the whole API |
| Secrets | `JWT_SECRET` is mandatory in production and must be ≥32 characters — the server refuses to boot otherwise; development gets an explicitly throwaway secret with a startup warning |
| Transport | HTTPS assumed at the proxy; HSTS (`max-age=31536000; includeSubDomains`) is emitted in production |
| Headers | CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, COOP, CORP, `Origin-Agent-Cluster`; `X-Powered-By` disabled |
| CORS | Explicit origin allow-list — the previous `Access-Control-Allow-Origin: *` is gone |
| XSS | Output escaped through the `Ui` helpers, no interpolated `innerHTML`, CSP as defence in depth |
| SQL injection | 100% parameterised queries; no user input is ever concatenated into SQL |
| Uploads | Magic-byte type detection, server-generated filenames, extension derived from the detected type, per-route size and file-count limits, rejected files deleted, `nosniff` on served files |
| Error handling | 5xx responses return a generic message in production; details stay in the server log |
| Dependencies | `npm run audit` in CI; only one moderate advisory remains open and documented |
| Secrets at rest | `.env` is git-ignored; `.env.example` holds placeholders only |

---

## Environment Setup

```bash
cp .env.example .env
node -e "console.log(require('crypto').randomBytes(48).toString('hex'))"   # → JWT_SECRET
node --env-file=.env backend/server.js
```

`npm start` does **not** load `.env` — use Node's built-in `--env-file` flag or
export the variables yourself. The app refuses to start with
`NODE_ENV=production` and a missing or weak `JWT_SECRET`.

| Variable | Required | Notes |
|---|---|---|
| `JWT_SECRET` | **in production** | ≥32 chars |
| `NODE_ENV` | recommended | `production` enables HSTS and hides 5xx messages |
| `CORS_ORIGINS` | in production | comma-separated allow-list of browser origins |
| `TRUST_PROXY_HOPS` | behind a proxy | must match the real proxy depth |
| `MAX_UPLOAD_MB` | no | per-file upload ceiling (default 5; profile images cap at 3) |
| `PORT` / `DATABASE_PATH` / `JWT_EXPIRES_IN` | no | sensible defaults |

---

## Hardening Checklist Before Going Live

- [ ] `JWT_SECRET` set to a fresh 48-byte random value.
- [ ] `NODE_ENV=production`.
- [ ] `CORS_ORIGINS` set to the real site origin (not `localhost`).
- [ ] `TRUST_PROXY_HOPS` matches the real proxy depth — a wrong value makes
      rate limiting key off the wrong IP.
- [ ] TLS terminated in front of the app; confirm `Strict-Transport-Security`
      is present.
- [ ] `node_modules` installed from `package-lock.json` with `npm ci`.
- [ ] The database file (`database/`) is **not** web-served.
- [ ] Default accounts and default admin passwords changed.
- [ ] Backups of the SQLite database taken and stored off-host.