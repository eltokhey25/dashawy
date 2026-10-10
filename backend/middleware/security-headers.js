/**
 * @file   Security response headers and CORS allow-list
 * @module security-headers
 *
 * Sets the CSP and the hardening headers on every response and replaces the previous wildcard
 * CORS with an explicit origin allow-list. Exports both the header and the CORS factories.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Security headers + CORS allow-list (dependency-free).
//
// Drop-in replacement for `helmet` + `cors`. When those packages are
// installed later, swap this file out in backend/app.js; nothing else refers
// to it.
//
// The CSP below is deliberately tight but still permits everything the site
// actually loads: Google Fonts CSS + woff2, Font Awesome CSS + webfonts, the
// inline <script>/<style> blocks every page ships, and blob:/data: images
// (avatar + portfolio previews are created client-side from a Blob URL).
const logger = require('../utils/logger');

const DEFAULT_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'X-DNS-Prefetch-Control': 'off',
  'X-Permitted-Cross-Domain-Policies': 'none',
  'X-XSS-Protection': '0',
  'Origin-Agent-Cluster': '?1',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'cross-origin',
  // No camera/microphone/geolocation/interest-cohort use anywhere in the app;
  // geolocation is only requested from the customer's own click handler, and
  // that still works because the policy is same-origin (not 'none').
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(self), payment=(), usb=()',
};

const CSP = [
  "default-src 'self'",
  // 'unsafe-inline' is required: every page has an inline <script> block.
  // A nonce-based policy would need a template change across the whole app.
  // accounts.google.com serves the Sign-In button and its popup, and
  // google.gstatic.com the iframe document it loads inside.
  "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com cdn.jsdelivr.net accounts.google.com",
  "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com",
  "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:",
  "img-src 'self' data: blob: https://lh3.googleusercontent.com",
  // The sign-in popup and its hidden credential iframe.
  'frame-src https://accounts.google.com',
  'child-src https://accounts.google.com',
  // Service worker, declared explicitly. Without it worker-src falls back to
  // child-src above, which is accounts.google.com only — so /sw.js was refused
  // and the PWA never installed. This permits same-origin workers and nothing
  // else: it is not a general loosening.
  "worker-src 'self'",
  "connect-src 'self' https://accounts.google.com",
  "media-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

/**
 * @param {object} [options]
 * @param {string[]} [options.allowedOrigins]  origins allowed to read the API
 * @param {boolean} [options.hsts]              send Strict-Transport-Security
 * @param {string}  [options.csp]                override the CSP string
 */
function securityHeaders(options) {
  const opts = options || {};
  const headers = Object.assign({}, DEFAULT_HEADERS, opts.headers || {});
  const csp = opts.csp || CSP;

  if (opts.hsts) {
    headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains';
  }

  return function applySecurityHeaders(req, res, next) {
    // Don't advertise the framework.
    res.removeHeader('X-Powered-By');
    Object.entries(headers).forEach(([k, v]) => res.setHeader(k, v));
    res.setHeader('Content-Security-Policy', csp);
    return next();
  };
}

/**
 * Canonical form of an origin, or null when the value cannot be one.
 *
 * Comparison is done on the normalised form so that `https://Example.com`,
 * `https://example.com:443` and `https://example.com/login` are all recognised
 * as the same origin — a browser is strict about this and sends the normalised
 * one, so an allow-list holding an un-normalised entry would silently never
 * match.
 *
 * @param {unknown} value
 * @returns {string|null}
 */
function normalizeOrigin(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  // Browsers send the literal string "null" for an opaque origin (a sandboxed
  // iframe, file://, some redirect chains). It identifies no host, so it can
  // never be same-origin and must never be allow-listed.
  if (trimmed.toLowerCase() === 'null') return null;
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch (e) {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return parsed.origin.toLowerCase();
}

/**
 * First value of a header that may have been repeated or comma-joined.
 *
 * @param {string|string[]|undefined} value
 * @returns {string}
 */
function firstHeaderValue(value) {
  if (!value) return '';
  const raw = Array.isArray(value) ? value[0] : value;
  const comma = String(raw).indexOf(',');
  return (comma === -1 ? String(raw) : String(raw).slice(0, comma)).trim();
}

/**
 * The origin the browser is actually talking to, taken from the request the
 * proxy handed us. Normalised, so it can be compared against an Origin header
 * with a single string comparison.
 *
 * The protocol comes from `req.secure`, which reads X-Forwarded-Proto — but
 * only because app.js turns on `trust proxy` first. Without that the request
 * would look like plain http behind Railway and every https Origin would be
 * rejected as "not same-origin", which is the bug this whole check exists to
 * avoid.
 *
 * The host comes from the raw `Host` header and NOT from `req.hostname`:
 * that getter prefers X-Forwarded-Host whenever the proxy is trusted, so a
 * client that can inject that header (any proxy that forwards unknown
 * X-Forwarded-* headers, or a preflight that our own CORS layer approves)
 * could nominate an arbitrary origin and have it reflected straight back —
 * i.e. the allow-list would be bypassed by the very request it was meant to
 * stop. Railway forwards the browser's real Host (the public hostname), so the
 * raw header is correct here and carries no client-controlled risk.
 *
 * If an edge ever rewrites Host instead of forwarding it, the rejection is now
 * logged with its reason, and the fix is to list the domain in CORS_ORIGINS.
 *
 * @param {import('express').Request} req
 * @returns {string|null} the normalised self-origin, or null if Host is absent
 */
function selfOrigin(req) {
  const proto = req.secure ? 'https' : 'http';
  const host = firstHeaderValue(req.headers.host);
  if (!host) return null;
  return normalizeOrigin(proto + '://' + host);
}

// Repeated blocks from one origin are collapsed: a misconfigured allow-list
// otherwise produces one identical WARN per page load, per asset and per poll,
// which buries every other line in the log.
const BLOCK_LOG_WINDOW_MS = 5 * 60 * 1000;
const BLOCK_LOG_MAX_TRACKED = 200;
const blockLogState = new Map();

/**
 * Rate-limits the blocked-origin warning per origin.
 *
 * @param {string} key
 * @returns {{log: boolean, suppressed: number}}
 */
function shouldLogBlock(key) {
  const now = Date.now();
  if (blockLogState.size > BLOCK_LOG_MAX_TRACKED) blockLogState.clear();
  const prev = blockLogState.get(key);
  if (!prev || now - prev.at >= BLOCK_LOG_WINDOW_MS) {
    const suppressed = prev ? prev.suppressed : 0;
    blockLogState.set(key, { at: now, suppressed: 0 });
    return { log: true, suppressed };
  }
  prev.suppressed += 1;
  return { log: false, suppressed: 0 };
}

/**
 * CORS with same-origin always allowed plus an explicit allow-list.
 *
 * Same call signature as the `cors` package, so it is a drop-in swap. Three
 * cases, in order:
 *
 *   1. No Origin header (curl, same-origin navigation, a service worker,
 *      a native app) — allowed, and no CORS headers are needed.
 *   2. The Origin equals the request's own origin — allowed and reflected. This
 *      is the normal case for the site: the frontend is served by this very
 *      process, so listing the deployment's public hostname in CORS_ORIGINS used
 *      to be the only way it could work, and forgetting that broke production.
 *   3. The Origin is in `allowedOrigins` — allowed and reflected.
 *
 * Anything else gets no CORS headers at all, which is what makes the browser
 * block the response. Origins are never reflected on a guess: there is no `*`,
 * and no code path echoes an Origin that was not matched against the request's
 * own host or the allow-list.
 *
 * @param {object} [options]
 * @param {string[]} [options.allowedOrigins]   extra origins (CORS_ORIGINS)
 * @param {boolean}  [options.allowSameOrigin=true]  always allow the request's own origin
 * @param {string}   [options.allowlistSource]  where the allow-list came from, for logs
 * @param {string[]} [options.methods]
 * @param {string[]} [options.allowedHeaders]
 * @param {number}   [options.maxAge=600]
 */
function corsPolicy(options) {
  const opts = options || {};
  const allowlist = new Set();
  (opts.allowedOrigins || []).forEach((o) => {
    const normalized = normalizeOrigin(o);
    if (normalized) allowlist.add(normalized);
  });
  const allowSameOrigin = opts.allowSameOrigin !== false;
  const source = opts.allowlistSource || 'the configured allow-list';
  const methods = opts.methods || ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'];
  const allowedHeaders = opts.allowedHeaders || ['Content-Type', 'Authorization'];
  const maxAge = opts.maxAge != null ? opts.maxAge : 600;

  return function applyCors(req, res, next) {
    // The response differs per Origin, allowed or not, so every response has to
    // say so — otherwise a shared cache can hand one origin's response,
    // headers included, to another.
    res.vary('Origin');

    const origin = req.headers.origin;

    if (!origin) {
      if (req.method === 'OPTIONS') return res.status(204).end();
      return next();
    }

    const normalized = normalizeOrigin(origin);
    const self = allowSameOrigin ? selfOrigin(req) : null;
    const isSelf = normalized !== null && self !== null && normalized === self;
    const isListed = normalized !== null && allowlist.has(normalized);

    if (!isSelf && !isListed) {
      const seen = self || 'unknown (no Host header)';
      let reason;
      if (normalized === null) {
        reason = 'the Origin header is not a usable http(s) origin (an opaque "null" origin?)';
      } else if (!allowSameOrigin) {
        reason = `same-origin requests are disabled and ${normalized} is not in ${source}`;
      } else {
        reason =
          `${normalized} is not this site (request origin ${seen}) ` + `and is not in ${source}`;
      }

      const gate = shouldLogBlock(origin);
      if (gate.log) {
        const more = gate.suppressed
          ? ` (${gate.suppressed} further attempt(s) from this origin suppressed for ${BLOCK_LOG_WINDOW_MS / 1000}s)`
          : '';
        logger.warn(
          'security',
          `[cors] blocked ${origin} on ${req.method} ${req.originalUrl || req.url} — ${reason}.${more}`
        );
      }

      // No CORS headers are emitted, so the browser refuses the response.
      // The request itself still runs — CORS is a browser policy, not authz.
      if (req.method === 'OPTIONS') return res.status(403).end();
      return next();
    }

    res.setHeader('Access-Control-Allow-Origin', normalized);
    res.setHeader('Access-Control-Allow-Methods', methods.join(', '));
    res.setHeader('Access-Control-Allow-Headers', allowedHeaders.join(', '));
    res.setHeader('Access-Control-Allow-Credentials', 'true');
    res.setHeader('Access-Control-Max-Age', String(maxAge));

    if (req.method === 'OPTIONS') return res.status(204).end();
    return next();
  };
}

module.exports = { securityHeaders, corsPolicy, CSP, DEFAULT_HEADERS, normalizeOrigin };
