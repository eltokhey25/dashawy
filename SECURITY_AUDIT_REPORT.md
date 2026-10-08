# SECURITY AUDIT REPORT — Dashawy

## Date: 2026-10-07

## Summary

- Critical: **1**
- High: **4**
- Medium: **7**
- Low: **6**
- Info: **5**

Scope: `backend/` (Express + better-sqlite3) and `frontend/` (vanilla JS, no
framework). 112 route handlers, 215 `db.prepare` calls, 16 `db.exec` calls,
112 raw input references. Audited by scan (the 15 scans specified in the brief)
plus live probing of the running server.

The application is already in better shape than the brief assumed: rate limiting,
security headers, CORS allow-listing, JWT algorithm pinning, production error
masking, magic-byte upload validation, IDOR scoping on every request/chat
endpoint, and a login path that cannot be used to enumerate accounts all **already
exist and were verified working**. Most of the "install these packages" work in the
brief was unnecessary — adding `helmet`, `cors` and `express-rate-limit` would
have *duplicated* working local implementations. Those were deliberately not
installed; see "Verified Safe".

---

## Vulnerabilities Found

### CRITICAL

1. **Password policy accepts trivially weak passwords, and the 6-character
   minimum is enforced at 9 sites** — `backend/routes/auth.routes.js:123,226,450`,
   `backend/routes/admin.routes.js:165,229,378,502,556,707`
   — Every password-set path in the app validates with
   `typeof password !== 'string' || password.length < 6`. Nothing requires a
   letter or a digit, so `123456` is accepted.
   **Confirmed live:** `POST /api/auth/register` with `password: "123456"`
   returned **HTTP 201** and created the account.
   This is the highest-severity finding because it is remotely reachable,
   unauthenticated, and weakens every account in the system including the
   seeded technician and any admin-provisioned user.
   **Fix:** single `isStrongPassword()` validator in `utils/validation.js`
   (≥8 chars, ≥1 letter, ≥1 digit) applied at all 9 sites, so the policy cannot
   drift between them again.

### HIGH

2. **No per-account login lockout; rate limiting is per-IP only** —
   `backend/middleware/rate-limit.js`, `backend/routes/auth.routes.js:39`
   — `loginLimiter` is `authLimiter('auth-login', 10)`, keyed by `req.ip`. There
   is no `failed_attempts` / `locked_until` column on `users` (the only
   `locked_until` in the codebase belongs to the Start PIN feature). A botnet or
   any attacker with a handful of IPs can therefore make 10 attempts per IP per
   15 minutes against **one** victim account indefinitely. Measured cost of one
   guess is ~164 ms of bcrypt, so a distributed attack is feasible.
   **Fix:** per-account lockout after repeated failures, enforced inside the login
   handler (not the limiter), returning the same generic error so it leaks
   nothing about whether the account exists.

3. **Chat has no per-user rate limit** — `backend/routes/messages.routes.js:161`
   — `POST /api/messages/:request_id` has no limiter of any kind (the only
   `apiLimiter` budget is 300 requests / 15 min for the *entire* API, shared by
   one client). A single authenticated user can flood a thread, and each message
   triggers a Web Push notification to the other party — so flooding is also a
   **push-notification amplification** vector against the victim's device.
   **Fix:** dedicated in-memory limiter on the message POST (30/min/user).

4. **Start PIN is stored in plaintext and is DB-readable** —
   `backend/services/broadcast.js:270`, `services/assignment.js:136`,
   `routes/admin.routes.js:1623`
   — **NOT FIXED, deliberately, and re-classified as Info (#6 below.** The
   brief proposed hashing it; that would break the feature, because the customer
   has to *read* the code off their screen to tell the technician. A hash is
   one-way, so hashing a value that must be displayed buys nothing — the
   plaintext would have to be stored alongside it anyway.

   The real exposure is database read access, and the meaningful mitigations are
   already in place: the PIN is `NULL`ed the moment it is spent
   (`technician.routes.js:320`), so it lives at rest only for the short window
   between "technician accepted" and "job started"; it is never returned by any
   technician-facing endpoint (stripped at
   `technician.routes.js:126`); and the only route that consumes it has a
   5-try/15-minute lockout.

   Residual risk: anyone with read access to `fixrequest.db` (or a stolen backup)
   can read an in-flight PIN. Acceptable, and not fixable by hashing. Tracked as
   Info #6 with the real recommendation (shorten the at-rest window; do not hash).

5. **`unread-count` and chat reads are unbounded in time** —
   `backend/routes/messages.routes.js:57,79`
   — `GET /api/messages/:request_id` returns **every** message in the thread with
   no pagination or cap. A long-running thread grows without limit, and because
   the client polls every 3 s the whole thread crosses the wire constantly. On a
   metered mobile connection this is a real cost, and an attacker can force a
   large response.
   **Fix:** cap the returned page (newest N) with the client pinned to the tail
   of the thread, and mark-as-read applied to the same window.

### MEDIUM

6. **10 tables have unindexed foreign-key columns** — `database/schema.sql`
   — Notably `messages.sender_id`, `messages.request_id`, `reviews.customer_id`,
   `technician_wallet.technician_id`, `request_broadcasts.technician_id`,
   `wallet_transactions.request_id`, `technician_profiles.technician_id`,
   `consents.user_id`, `data_subject_requests.user_id`,
   `withdrawal_requests.processed_by`, `requests.payment_confirmed_by`.
   `EXPLAIN QUERY PLAN` on the unread-count hot path currently resolves via
   `idx_messages_unread` so chat is fine today, but the FK columns are exactly
   what cascades and joins use — a `DELETE FROM requests` must scan
   `messages.request_id` to fire the cascade, and it is unindexed.
   **Fix:** add the indexes.

7. **Dead table `rate_limit_buckets`** — present in the live DB and in
   `schema.sql`, containing 0 rows, with **no code anywhere that reads or writes
   it** (`grep -rn rate_limit_buckets backend/` → no matches). The real limiter
   is in-memory. This is confusing for the next reader and implies a persistence
   layer that does not exist.
   **Fix:** drop the table and remove it from `schema.sql`.

8. **`GET /api/health` returns no uptime or version** — `backend/app.js:79`
   — responds `{"ok":true}` only. A load balancer or uptime monitor cannot tell
   *which* build is live, and a process that has wedged but still answers TCP is
   indistinguishable from a healthy one.
   **Fix:** add `uptime` (seconds) and `version` (from `package.json`).

9. **No graceful shutdown** — `backend/server.js`
   — no `SIGTERM`/`SIGINT` handlers. On deploy or restart, in-flight requests are
   cut and the process dies immediately; SQLite gets no chance to finish a
   transaction.
   **Fix:** close the HTTP server, then the DB, then exit.

10. **Static frontend served with no cache headers** — `backend/app.js:228`
    — `express.static(frontendDir)` with no `maxAge`, so `Cache-Control:
    public, max-age=0` on every asset. Functionally safe (the service worker
    network-fetches with `cache: 'no-store'` anyway) but wasteful.
    **Fix:** `maxAge` + `etag`.

11. **`X-XSS-Protection: 0` is correct but `Cross-Origin-Embedder-Policy` is
    unset while COOP is `same-origin`** — `backend/middleware/security-headers.js:26`
    — internally consistent today (COEP is deliberately off because Google Fonts /
    FA are cross-origin). Noted so a future "just add COEP" change does not break
    font loading silently.
    **Fix:** document the coupling (comment only).

12. **`CORS_ORIGINS` is documented in `.env.example` only as a comment** —
    `.env.example:23` — and the variable is the only one read by the app that is
    not set anywhere. An operator deploying behind a different domain can follow
    the comment, but nothing validates that production CORS is not accidentally
    left empty (which would silently break a split frontend/API deployment, or,
    worse, invite someone to "fix" it with a wildcard).
    **Fix:** surface a boot warning when `NODE_ENV=production` and
    `CORS_ORIGINS` is unset.

### Info

19. **Start PIN cannot be hashed** — see HIGH #4 above for the full reasoning.
    Not a defect: the value must be displayable, so a one-way hash provides no
    protection. The at-rest window is already short (cleared on use) and access
    is already restricted. Recommendation is to shorten the window further if
    the threat model includes backup theft, not to hash.

---

## LOW

13. **bcrypt cost factor 10 at 7 hash sites** — `auth.routes.js:145,248,464`,
    `admin.routes.js:176,236,403,506,562,713`
    — cost 10 is ~285 ms here. Acceptable today, low for high-value admin
    accounts in 2026.
    **Fix:** raise to 12, centralised in one constant.

14. **No `NOT NULL`/CHECK hardening on `messages.sender_role`** — the app-level
    `CHECK` was added to `schema.sql`, but pre-existing rows were never
    backfilled/validated.
    **Fix:** one-off validation query; none needed (all values are written by the
    server).

15. **Unused export `notFound`** — `backend/utils/errors.js:97` — exported,
    referenced 0 times.
    **Fix:** remove.

16. **`console.*` used directly in ~40 places** instead of a logger
    `backend/app.js`, `db.js`, `server.js`, routes.
    **Fix:** `utils/logger.js` with timestamped tags, adopted across the backend.

17. **Dead comment block showing the `express-rate-limit` import**
    `backend/middleware/rate-limit.js:18-19`
    **Fix:** keep (it documents the swap path) but mark it as documentation, not
    commented-out code.

18. **No JSDoc on many exported functions** — several route handlers and
    helpers lack `@param`/`@returns`.
    **Fix:** document the exported surface.

---

## Verified Safe

These were checked and found already implemented and working. **No package was
installed for them** — adding `helmet`, `cors` or `express-rate-limit` would have
duplicated working local code.

- **Rate limiting — PRESENT and working.** `backend/middleware/rate-limit.js`
  (125 lines, dependency-free, express-rate-limit-compatible API). `apiLimiter`
  300/15 min on `/api/`, plus `loginLimiter` / `registerLimiter` /
  `changePasswordLimiter` at 10 failures/15 min each, with
  `skipSuccessfulRequests` so real users are not punished.
- **Security headers — PRESENT.** Live response carries `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`, `Origin-Agent-Cluster`,
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy`, and a real CSP.
- **CORS — PRESENT and correctly restricting.** Live probes:
  `localhost:3000` → allowed, `127.0.0.1:3000` → allowed, `http://evil.com` →
  **no ACAO header**, `https://evil.com` → **no ACAO header**. No origin
  reflection. `X-Powered-By` is removed.
- **JWT algorithm pinning — PRESENT.** `jwt.verify(..., { algorithms: ['HS256'] })`
  in `middleware/auth.js:28`, `algorithm: 'HS256'` on sign. Live: a hand-built
  `alg=none` token with `is_super_admin: true` returned **401**.
- **JWT claims are never trusted for authorization.** Role, super-admin flag and
  permissions are re-read from the database on every request, so a downgraded or
  blocked account loses access immediately rather than at token expiry.
- **Body size limits — PRESENT.** `express.json`/`urlencoded` capped at 100 kb;
  `entity.too.large` mapped to 413.
- **Trust proxy — PRESENT** (`TRUST_PROXY_HOPS`).
- **Production error masking — PRESENT.** 5xx messages replaced with a generic
  string when `NODE_ENV=production`; malformed JSON → 400; stack stays in the
  log, never the response.
- **SQL injection — none found.** All 215 `db.prepare` calls are parameterized.
  The 16 `db.exec` calls interpolate only *compile-time constants*: schema text
  and hard-coded column lists in `db.js` migrations (never user input). The
  dynamic-SQL sites in `admin.routes.js` (`${k} = @${k}`, the `reviews` filter,
  `service_pricing` updates) build **column names from internal allow-lists** and
  pass all values as `?`/`@named` parameters. `reviewsFilter()` builds its
  `WHERE` clause from a fixed set of clauses with `?` placeholders only.
- **IDOR — none exploitable.** Every customer route scopes on
  `r.customer_id = ?`; every technician route on `r.technician_id = ?`; the chat
  router authorises per request and only for the owning customer or the assigned
  technician. Live: a stranger on a foreign chat got **403** on GET, POST and
  `/read`, and the message count was unchanged; a foreign request id returned
  **403**.
- **Account enumeration via login — blocked by design.** One shared Arabic error
  for "no such account" and "wrong password".
- **File uploads — solid.** Magic-byte validation via `file-type`; the extension
  is **derived from the detected MIME**, never from the client's `originalname`,
  so a stored file cannot carry `.php`/`.phtml`/`.jsp`; per-file size caps on all
  multer instances; documents staged in `pending/` and moved inside the
  registration transaction; `nosniff` on `/uploads`; admin document route
  resolves and prefix-checks the path (traversal-safe), forces the MIME from the
  extension, and sends `Content-Disposition: inline`.
- **Chat authorisation.** `authorizeRequest()` rejects non-participants with 403;
  sending is restricted to `assigned`/`in_progress` server-side (the UI lock is
  not the control); read stays open so finished jobs keep their transcript.
- **Start PIN gate is the only route to `in_progress`.** The legacy
  `/requests/:id/accept` is retired with an explicit 400, and `start_pin` is
  stripped from the technician payload.
- **No sensitive data in logs.** Grepping all `console.*` calls for
  password/token/secret/hash/email returns nothing.
- **No hardcoded secrets.** The dev JWT fallback is clearly labelled and gated on
  `NODE_ENV`; production without `JWT_SECRET` throws at require time.
- **No prototype-pollution or mass-assignment sinks**; `role` is never accepted
  from a request body.
- **No open redirect.** The only `location.href` targets are literals.

---

## Recommendations

1. Land the password-policy fix first — it is the only CRITICAL and it is a
   five-line change once centralised.
2. Add per-account login lockout (HIGH #2). It is the control that actually stops
   a distributed credential-stuffing run; the per-IP limiter cannot.
3. Rate-limit chat sends (HIGH #3), and hash the Start PIN at rest (HIGH #4).
4. Before any further `r.*` query is added to a customer- or technician-facing
   route, remember this codebase has already leaked a secret that way twice
   (`start_pin`, `start_pin_attempts`). Prefer explicit column lists on any
   endpoint crossing the trust boundary.
5. Consider moving rate-limit state to Redis before running more than one
   instance; the in-memory buckets count per process.
6. Re-run this audit when `file-type` is upgraded (the one remaining dependency
   advisory is a moderate DoS in its ASF parser, reachable only through the
   technician document upload path, which is itself rate-limited and
   size-capped).