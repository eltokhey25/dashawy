/**
 * @file   Google ID token verification
 * @module services.google-identity
 *
 * Verifies a Google Sign-In ID token on the server: fetches Google's published
 * signing keys, checks the RS256 signature, then checks audience, issuer,
 * expiry and `email_verified`. Exports verifyGoogleIdToken(), which the auth
 * route treats as the only trustworthy source of an identity.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Server-side verification of a Google Sign-In ID token.
//
// The browser hands us an opaque string, so nothing about it can be believed:
// a JWT is just three base64 segments, and a caller can mint any of them. The
// only thing that makes an ID token trustworthy is checking, here on the
// server, that Google signed it. That is what this module does — it fetches
// Google's published JWKS and verifies the RSA signature before any claim is
// read. Decoding without verifying would let anybody POST `{"email":
// "victim@example.com"}` and be logged in as that account.
//
// Written against node:crypto and global fetch rather than google-auth-library
// or jose, because this project deliberately runs on a handful of hand-picked
// dependencies and neither library is otherwise used here.
const crypto = require('crypto');
const config = require('../config');
const { AppError } = require('../utils/errors');
const { isValidEmail, normalizeEmail } = require('../utils/validation');
const logger = require('../utils/logger');

// Google's rotating signing keys. Public by definition — they are what proves a
// token's signature, so publishing them is the whole point.
const JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs';

// Both spellings of the issuer appear in real tokens; Google documents the
// bare host as the legacy form but has not retired it.
const TRUSTED_ISSUERS = ['https://accounts.google.com', 'accounts.google.com'];

// How long a fetched key set is trusted. Google's rotation is rare, and an
// unknown `kid` forces an immediate refetch, so a long TTL costs nothing.
const JWKS_TTL_MS = 6 * 60 * 60 * 1000;
const JWKS_TIMEOUT_MS = 5000;

// Tolerance for clock drift between this server and Google's. A token is
// accepted for this long PAST its own `exp`, and one issued this far in the
// future is rejected. Servers drift from real time by seconds, and without the
// tolerance a correctly-signed token would be refused on a machine whose clock
// runs a little fast. 30s is far shorter than the token's own lifetime, so the
// grace period cannot meaningfully extend a leaked token's usefulness.
const CLOCK_SKEW_SECONDS = 30;

let cachedKeys = new Map();
let cachedAt = 0;
// A single in-flight fetch shared by concurrent logins, so a cold start under
// load does not open one HTTPS request per request.
let pendingFetch = null;

function authError(message) {
  // 401 throughout: a token that does not verify is a failed authentication,
  // whatever the reason, and a 400 would tell the caller to fix the request.
  return new AppError(message || 'تعذر التحقق من تسجيل الدخول بحساب جوجل', 401);
}

function decodeSegment(segment) {
  return JSON.parse(Buffer.from(segment, 'base64url').toString('utf8'));
}

// Google's JWK entries carry a full x5c certificate chain. Verifying against the
// leaf certificate lets node:crypto do the whole trust check (validity dates,
// key usage) instead of us hand-rolling RSA padding and hash handling.
function certificateToPem(x5cLeaf) {
  const lines = x5cLeaf.match(/.{1,64}/g) || [];
  return '-----BEGIN CERTIFICATE-----\n' + lines.join('\n') + '\n-----END CERTIFICATE-----\n';
}

async function fetchJwks() {
  let response;
  try {
    response = await fetch(JWKS_URL, {
      signal: AbortSignal.timeout(JWKS_TIMEOUT_MS),
      headers: { Accept: 'application/json' },
    });
  } catch (err) {
    logger.error('google', '[google] could not reach the JWKS endpoint:', err.message);
    throw authError('تعذر الاتصال بخدمة جوجل، حاول مرة أخرى');
  }
  if (!response.ok) {
    logger.error('google', '[google] JWKS endpoint returned ' + response.status);
    throw authError('تعذر الاتصال بخدمة جوجل، حاول مرة أخرى');
  }

  const body = await response.json();
  const keys = new Map();
  (body.keys || []).forEach((key) => {
    // Only signing keys are accepted. Dropping everything else up front means
    // an encryption key (alg RSA-OAEP, use enc) can never be used to verify a
    // token signature, whatever the token header claims.
    if (key.kty !== 'RSA' || key.alg !== 'RS256' || key.use !== 'sig') return;
    if (!key.kid || !Array.isArray(key.x5c) || !key.x5c.length) return;
    keys.set(key.kid, certificateToPem(key.x5c[0]));
  });

  if (!keys.size) {
    logger.error('google', '[google] JWKS endpoint returned no usable signing keys');
    throw authError('تعذر الاتصال بخدمة جوجل، حاول مرة أخرى');
  }

  cachedKeys = keys;
  cachedAt = Date.now();
  return keys;
}

async function getSigningKeys(forceRefresh) {
  const fresh = Date.now() - cachedAt < JWKS_TTL_MS && cachedKeys.size > 0;
  if (fresh && !forceRefresh) return cachedKeys;
  if (pendingFetch) return pendingFetch;

  pendingFetch = fetchJwks();
  try {
    return await pendingFetch;
  } finally {
    pendingFetch = null;
  }
}

function verifyRs256Signature(pem, signingInput, signature) {
  const verifier = crypto.createVerify('RSA-SHA256');
  verifier.update(signingInput);
  verifier.end();
  return verifier.verify(pem, signature);
}

/**
 * Verifies a Google ID token and returns its claims.
 *
 * @param {string} idToken  the `credential` string handed back by the Google button
 * @returns {Promise<{sub: string, email: string, name: string|null, picture: string|null}>}
 * @throws {AppError} 401 when the token is malformed, unsigned by Google, expired,
 *   issued for a different application, or carries an unverified email address.
 */
async function verifyGoogleIdToken(idToken) {
  if (!config.GOOGLE_ENABLED) {
    throw new AppError('تسجيل الدخول بحساب جوجل غير مفعّل حالياً', 503);
  }

  const parts = String(idToken || '').split('.');
  if (parts.length !== 3) throw authError();

  let header;
  let payload;
  try {
    header = decodeSegment(parts[0]);
    payload = decodeSegment(parts[1]);
  } catch (err) {
    throw authError();
  }
  if (!header || !payload) throw authError();

  // `alg` is pinned to RS256 BEFORE the signature is checked, and the algorithm
  // passed to node:crypto is derived from our own constant rather than from the
  // header. A token claiming `none`, HS256, or an RSA public key as its HMAC
  // secret is rejected here regardless of what it is signed with.
  if (header.alg !== 'RS256') throw authError();
  if (!header.kid) throw authError();

  let keys = await getSigningKeys(false);
  let pem = keys.get(header.kid);
  if (!pem) {
    // Unknown kid: the normal cause is that Google rotated its keys. Refetch
    // once before giving up, so a rotation does not log every user out.
    keys = await getSigningKeys(true);
    pem = keys.get(header.kid);
    if (!pem) throw authError();
  }

  let signature;
  try {
    signature = Buffer.from(parts[2], 'base64url');
  } catch (err) {
    throw authError();
  }
  if (!signature.length) throw authError();

  // The signed payload is the first two segments joined by a dot, exactly as
  // they arrived — re-encoding the decoded JSON would change the bytes and
  // break the signature.
  const valid = verifyRs256Signature(pem, parts[0] + '.' + parts[1], signature);
  if (!valid) throw authError();

  // Audience: the token must have been minted for THIS application. Without
  // this check a valid Google token issued to some other site could be replayed
  // here and would verify against Google's keys perfectly well.
  const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!audiences.includes(config.GOOGLE_CLIENT_ID)) throw authError();

  if (!TRUSTED_ISSUERS.includes(payload.iss)) throw authError();

  const nowSeconds = Math.floor(Date.now() / 1000);
  if (typeof payload.exp !== 'number' || payload.exp + CLOCK_SKEW_SECONDS < nowSeconds) {
    throw authError('انتهت صلاحية تسجيل الدخول بحساب جوجل، حاول مرة أخرى');
  }
  if (typeof payload.iat === 'number' && payload.iat - CLOCK_SKEW_SECONDS > nowSeconds) {
    throw authError();
  }

  // `sub` is the stable per-application account identifier. It is the key we
  // link on, so an absent one would leave the account unlinkable.
  if (typeof payload.sub !== 'string' || !payload.sub.trim()) throw authError();

  // Google sends email_verified as a boolean, but a string "true" has been seen
  // in the wild, so both spellings are accepted.
  const verified = payload.email_verified === true || payload.email_verified === 'true';
  if (!verified) throw authError('البريد الإلكتروني من جوجل غير مُوثّق');

  if (!isValidEmail(payload.email)) throw authError();
  const email = normalizeEmail(payload.email);

  return {
    sub: payload.sub,
    email,
    name: typeof payload.name === 'string' ? payload.name.trim() : '',
    picture: typeof payload.picture === 'string' ? payload.picture : '',
  };
}

module.exports = { verifyGoogleIdToken };
