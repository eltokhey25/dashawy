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
 * CORS with an allow-list. Same call signature as the `cors` package, so it is
 * a drop-in swap. A request with no Origin (curl, same-origin navigation,
 * mobile webviews) is allowed; a browser Origin that is not on the list gets
 * no CORS headers, which is what makes the browser block the response.
 */
function corsPolicy(options) {
  const opts = options || {};
  const allowed = opts.allowedOrigins || [];
  const methods = opts.methods || ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'];
  const allowedHeaders = opts.allowedHeaders || ['Content-Type', 'Authorization'];
  const maxAge = opts.maxAge != null ? opts.maxAge : 600;

  return function applyCors(req, res, next) {
    const origin = req.headers.origin;

    if (origin && allowed.indexOf(origin) === -1) {
      logger.warn('security', '[cors] blocked origin: ' + origin);
      // No CORS headers are emitted, so the browser refuses the response.
      // The request itself still runs — CORS is a browser policy, not authz.
      if (req.method === 'OPTIONS') return res.status(403).end();
      return next();
    }

    if (origin) {
      res.setHeader('Access-Control-Allow-Origin', origin);
      res.setHeader('Vary', 'Origin');
      res.setHeader('Access-Control-Allow-Methods', methods.join(', '));
      res.setHeader('Access-Control-Allow-Headers', allowedHeaders.join(', '));
      res.setHeader('Access-Control-Allow-Credentials', 'true');
      res.setHeader('Access-Control-Max-Age', String(maxAge));
    }

    if (req.method === 'OPTIONS') return res.status(204).end();
    return next();
  };
}

module.exports = { securityHeaders, corsPolicy, CSP, DEFAULT_HEADERS };
