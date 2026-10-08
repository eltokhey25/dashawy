/**
 * @file   Dependency-free request rate limiting
 * @module rate-limit
 *
 * Fixed-window per-client counter exposed through an express-rate-limit-compatible API. Serves
 * one general budget for the whole API and tighter buckets for the auth endpoints, counting
 * only failed attempts.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Rate limiting (dependency-free).
//
// Drop-in replacement for `express-rate-limit` with the same options and
// response shape, so the app can switch to the real package later without
// touching any route file:
//
//   const rateLimit = require('express-rate-limit');
//   const apiLimiter = rateLimit({ windowMs, max, standardHeaders, legacyHeaders, message });
//
// Why hand-rolled: this audit runs under a hard "do not modify node_modules"
// constraint, so no new dependency could be installed. The implementation is a
// fixed-window counter per client key with lazy expiry — the same algorithm
// express-rate-limit v7 uses by default, minus the shared store.
//
// Production note: the counters live in this process's memory. With more than
// one instance (or behind a load balancer) each instance counts separately, so
// a real deployment should swap in `express-rate-limit` with a Redis store.
const logger = require('../utils/logger');

const buckets = new Map();

// Keep the map from growing without bound when clients rotate IPs.
function sweep(now) {
  for (const [key, b] of buckets) {
    if (b.resetAt <= now) buckets.delete(key);
  }
}

let lastSweep = Date.now();

function clientKey(req) {
  // Behind a proxy, req.ip already honours trust proxy. Fall back to the raw
  // socket address so the limiter never keys on an empty value.
  return req.ip || (req.socket && req.socket.remoteAddress) || 'unknown';
}

/**
 * Key on the authenticated user when there is one, falling back to the caller's
 * IP for anonymous traffic.
 *
 * Some surfaces must be limited per ACCOUNT rather than per ADDRESS: a shared
 * office NAT, a mobile carrier CGNAT or a botnet would otherwise let one real
 * user through many times their intended budget (or let an attacker with many
 * addresses through entirely). Used by the chat limiter.
 *
 * @param {import('express').Request} req
 * @returns {string}
 */
function userKey(req) {
  const uid = req.user && req.user.id;
  return uid != null ? 'user:' + uid : 'ip:' + clientKey(req);
}

/**
 * @param {object} options
 * @param {number} options.windowMs   window length in ms
 * @param {number} options.max        requests allowed per window per client
 * @param {boolean} [options.standardHeaders=true]  emit RateLimit-* headers
 * @param {boolean} [options.legacyHeaders=false]   emit X-RateLimit-* headers
 * @param {object|string} options.message          429 body
 * @param {string} [options.name]                   bucket namespace
 * @param {boolean} [options.skipSuccessful=false]  don't count 2xx/3xx
 * @param {Function} [options.keyBy]               custom bucket key (req) => string;
 *                                               defaults to the caller IP. Use userKey
 *                                               for per-account limits.
 */
function rateLimit(options) {
  const opts = options || {};
  const windowMs = opts.windowMs || 15 * 60 * 1000;
  const max = opts.max != null ? opts.max : 100;
  const standardHeaders = opts.standardHeaders !== false;
  const legacyHeaders = opts.legacyHeaders === true;
  const message = opts.message || { error: 'Too many requests' };
  const name = opts.name || 'default';
  const skipSuccessful = opts.skipSuccessful === true;
  const keyBy = typeof opts.keyBy === 'function' ? opts.keyBy : clientKey;

  return function limiter(req, res, next) {
    const now = Date.now();
    if (now - lastSweep > windowMs) {
      sweep(now);
      lastSweep = now;
    }

    const key = name + '|' + keyBy(req);
    let bucket = buckets.get(key);
    if (!bucket || bucket.resetAt <= now) {
      bucket = { count: 0, resetAt: now + windowMs };
      buckets.set(key, bucket);
    }
    bucket.count += 1;

    const remaining = Math.max(0, max - bucket.count);
    const resetSeconds = Math.ceil((bucket.resetAt - now) / 1000);

    if (standardHeaders) {
      res.setHeader('RateLimit-Limit', String(max));
      res.setHeader('RateLimit-Remaining', String(remaining));
      res.setHeader('RateLimit-Reset', String(resetSeconds));
      res.setHeader('RateLimit-Policy', `${max};w=${Math.round(windowMs / 1000)}`);
    }
    if (legacyHeaders) {
      res.setHeader('X-RateLimit-Limit', String(max));
      res.setHeader('X-RateLimit-Remaining', String(remaining));
      res.setHeader('X-RateLimit-Reset', String(resetSeconds));
    }

    if (bucket.count > max) {
      res.setHeader('Retry-After', String(resetSeconds));
      if (req.path && req.path.startsWith('/api')) {
        logger.warn(
          'ratelimit',
          `[ratelimit] ${name} blocked ${keyBy(req)} (${max}/${windowMs}ms)`
        );
      }
      return res.status(429).json(typeof message === 'string' ? { error: message } : message);
    }

    // When configured, a successful response refunds the request so only
    // failed attempts count (used for the login endpoint).
    if (skipSuccessful) {
      res.on('finish', () => {
        if (res.statusCode < 400) {
          bucket.count = Math.max(0, bucket.count - 1);
        }
      });
    }

    return next();
  };
}

// Test helper: forget every counter.
function reset() {
  buckets.clear();
}

module.exports = rateLimit;
module.exports.rateLimit = rateLimit;
module.exports.reset = reset;
module.exports.userKey = userKey;
