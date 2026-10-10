/**
 * @file   Application error types and Express error handler
 * @module errors
 *
 * Defines the AppError class used to signal expected failures with a status code, plus the
 * terminal error handler that hides 5xx detail in production and maps malformed bodies to 413.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Error helpers: AppError + async handler wrapper for Express 4.
const logger = require('./logger');

// Reusable error type with a HTTP status code.
//
// The optional `code` is a stable machine-readable tag ('profile_incomplete',
// …). The Arabic message is for people and may be reworded at any time; a client
// that has to branch on behaviour needs something it can rely on, and matching
// on a translated sentence is how a copy edit turns into a production bug.
class AppError extends Error {
  constructor(message, status = 400, code) {
    super(message);
    this.name = 'AppError';
    this.status = status;
    if (code) this.code = code;
  }
}

// Wraps an async route handler so rejected promises reach the error middleware.
function wrap(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

// Common shorthand constructors.
const badRequest = (msg) => new AppError(msg || 'Bad request', 400);
const unauthorized = (msg) => new AppError(msg || 'Unauthorised', 401);
const forbidden = (msg) => new AppError(msg || 'Forbidden', 403);
// (A notFound() shorthand was removed: it was exported but referenced nowhere,
// which made it look like a guard the app had and did not.)

// Global error-handling middleware for Express.
// eslint-disable-next-line no-unused-vars
function errorHandler(err, req, res, next) {
  if (err.type === 'entity.parse.failed' || err instanceof SyntaxError) {
    return res.status(400).json({ error: 'Invalid JSON body' });
  }

  // A body larger than the parser limit arrives here as an entity.too.large
  // error; answering 400 (instead of a generic 500) makes the cause clear
  // without echoing the parser's message.
  if (err.type === 'entity.too.large' || err.status === 413) {
    return res.status(413).json({ error: 'حجم الطلب أكبر من المسموح' });
  }

  const status = err.status || 500;

  // 4xx messages are written by us and are safe to return. 5xx messages are
  // raw driver/Node errors and can leak internals (SQL, file paths, library
  // versions), so in production they are replaced with a generic string.
  let message;
  if (status < 500) {
    message = err.message || 'Bad request';
  } else if (process.env.NODE_ENV === 'production') {
    message = 'Internal server error';
  } else {
    message = err.message || 'Internal server error';
  }

  if (status >= 500) {
    // The full error (stack included) stays in the server log, never in the
    // response body.
    logger.error('errors', '[error]', err);
  }

  // A multer limit (file too large / too many files) surfaces as a plain
  // Error with a 500 by default; give it its real 4xx status.
  //
  // Only an AppError's own tag is echoed. Multer and Node attach their own
  // `code` properties ('LIMIT_FILE_SIZE', 'ENOENT', …), and copying those into
  // the response would leak an internal vocabulary and let a client start
  // branching on driver-specific strings.
  const body = { error: message };
  if (err instanceof AppError && err.code) body.code = err.code;
  res.status(status).json(body);
}

// Translates a typed failure from a service into an AppError.
//
// Services throw plain Errors carrying a stable `code` (see pricing.js's
// walletError) rather than importing this module, which keeps the dependency
// pointing one way: routes -> services -> db. A route that forgets to map them
// turns every "already paid" into a 500, so both the technician and admin
// withdrawal routes funnel through here.
//
// An unrecognised code is returned UNTOUCHED on purpose: an unexpected internal
// error should still surface as a 500 and get logged, rather than being flattened
// into a 400 that hides it and tells the client nothing.
function serviceError(err, statusByCode) {
  if (err && statusByCode && statusByCode[err.code]) {
    return new AppError(err.message, statusByCode[err.code]);
  }
  return err;
}

module.exports = {
  AppError,
  wrap,
  badRequest,
  unauthorized,
  forbidden,
  errorHandler,
  serviceError,
};
