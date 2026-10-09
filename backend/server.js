/**
 * @file   HTTP entry point
 * @module server
 *
 * Boots the app: loads the Express wiring, warms the database connection, starts listening
 * and launches the background workers. Everything else is delegated to app.js so the
 * process stays a thin entry point.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Server entry point.
const app = require('./app');
const config = require('./config');
const db = require('./db');
const broadcast = require('./services/broadcast');
const queue = require('./services/queue');
const logger = require('./utils/logger');

// ---------------------------------------------------------------------------
// TEMPORARY admin account recovery — REMOVE AFTER USE
// ---------------------------------------------------------------------------
// Set these two in the deployment's variables (Railway → Service → Variables) and
// redeploy; the account is then created or repaired on boot, with no shell access
// needed on the box:
//
//   RESET_ADMIN_EMAIL    the address to own (defaults to Eltokhey@salahha.com)
//   RESET_ADMIN_PASSWORD the password to set
//
// Delete both variables (and this block) the moment you are back in. The gate is
// the reason the block is safe to leave in place while you work: with no
// variables set nothing runs, so once you remove them the next deploy cannot
// silently undo a password you changed through the UI afterwards.
//
// The password is never written here and never logged — it only ever exists in
// the deployment's variable store, and these logs are readable in the Railway
// dashboard by anyone with project access.
//
// The password variable alone is the gate, because the email falls back to the
// owner account below. An address is not a secret; the password is.
const RESET_ADMIN_EMAIL = process.env.RESET_ADMIN_EMAIL || 'Eltokhey@salahha.com';

if (process.env.RESET_ADMIN_PASSWORD) {
  try {
    const bcrypt = require('bcryptjs');
    // Shared with every route that writes an email. Required here rather than
    // inlined because the login endpoint looks accounts up through it too
    // (auth.routes.js:426), and users.email is a plain UNIQUE column — SQLite
    // compares TEXT case-sensitively, so storing "Eltokhey@salahha.com" while
    // login normalises to lowercase would create an account that can never
    // sign in.
    const { isValidEmail, normalizeEmail, passwordPolicyError } = require('./utils/validation');

    const targetEmail = normalizeEmail(RESET_ADMIN_EMAIL);
    if (!isValidEmail(targetEmail)) {
      throw new Error(`RESET_ADMIN_EMAIL is not a valid address: ${RESET_ADMIN_EMAIL}`);
    }

    // bcrypt will happily hash "a", and the value here is typed by hand into the
    // Railway dashboard, so the same policy the routes enforce is checked first —
    // otherwise a typo silently installs a password the app would have refused.
    const pwProblem = passwordPolicyError(process.env.RESET_ADMIN_PASSWORD);
    if (pwProblem) throw new Error(`RESET_ADMIN_PASSWORD rejected: ${pwProblem}`);

    const hash = bcrypt.hashSync(process.env.RESET_ADMIN_PASSWORD, 12);

    const existing = db
      .prepare('SELECT id, role, is_super_admin FROM users WHERE email = ?')
      .get(targetEmail);

    const run = db.transaction(() => {
      if (existing) {
        db.prepare(
          `UPDATE users
              SET password_hash = ?, role = 'admin', is_super_admin = 1, blocked = 0,
                  must_change_password = 0
            WHERE id = ?`
        ).run(hash, existing.id);
        return { id: existing.id, action: 'updated' };
      }
      const inserted = db
        .prepare(
          `INSERT INTO users (email, password_hash, role, is_super_admin, blocked, must_change_password)
           VALUES (?, ?, 'admin', 1, 0, 0)`
        )
        .run(targetEmail, hash);
      return { id: inserted.lastInsertRowid, action: 'created' };
    });

    const { id, action } = run();

    // The email is safe to print; the password is not.
    logger.warn(
      'server',
      `[FORCE-RESET] Admin ${action} for: ${targetEmail} (id ${id}). REMOVE RESET_ADMIN_EMAIL + RESET_ADMIN_PASSWORD NOW.`
    );
  } catch (err) {
    // A failure here must not stop the server from booting, or a bad value
    // would trade "I can't log in" for "the whole app is down".
    logger.error('server', `[FORCE-RESET] failed:`, err.message);
  }
}

db.prepare('SELECT 1').get();

const server = app.listen(config.PORT, () => {
  logger.info('server', `Fix Request server running on http://localhost:${config.PORT}`);
  logger.info(
    'broadcast',
    `[broadcast] offers expire after ${broadcast.timeoutHours()}h (BROADCAST_TIMEOUT_HOURS)`
  );

  // The waiting queue is started from inside the listen callback on purpose: it is
  // the one worker that writes to requests, and starting it before the HTTP server
  // is accepting would let its first pass race the first request.
  queue.start();
});

// Periodic expiry sweep for the broadcast window, so an offer nobody answers is
// closed even when no technician happens to poll during the window.
//
// The interval is a third of the window, capped at MAX_SWEEP_INTERVAL_MS. The cap
// matters because the window is now measured in hours: without it a 24h window
// would schedule a sweep every 8h, and a request that timed out could sit
// misleadingly "pending" for hours after its offers had actually lapsed. The cap
// costs one cheap indexed pass per 15 minutes.
const MAX_SWEEP_INTERVAL_MS = 15 * 60 * 1000;
const sweepIntervalMs = Math.min(
  MAX_SWEEP_INTERVAL_MS,
  Math.max(5000, Math.round((broadcast.timeoutSeconds() * 1000) / 3))
);
const sweepTimer = setInterval(() => {
  try {
    broadcast.sweepIfDue(true);
  } catch (err) {
    // A sweep that threw would otherwise kill the interval and silently stop expiry
    // for the rest of the process' life. sweepIfDue already swallows its own errors;
    // this is the belt to that braces.
    logger.info('server', `[broadcast] expiry sweep failed:`, err.message);
  }
}, sweepIntervalMs);
// unref() matters more than it looks. Without it this timer keeps the event loop
// alive forever, so the process would ignore SIGTERM and any test that requires
// server.js would hang instead of exiting.
sweepTimer.unref();

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
// Without this the process dies on the first SIGTERM, cutting in-flight requests
// mid-response and closing the SQLite handle while a transaction may still be
// open. `better-sqlite3` writes are synchronous, so an open transaction is only
// left behind by a throw inside one — but a WAL checkpoint or a busy handler can
// still be mid-flight, and a clean close flushes it.
//
// server.close() stops accepting new connections and waits for in-flight
// requests to finish; the hard timer is the backstop for a socket that never
// closes, because an orchestrator's SIGKILL is worse than a truncated request.
let shuttingDown = false;

function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info('server', `${signal} received — shutting down gracefully.`);

  // Stop the workers writing to requests before the server stops accepting, so
  // nothing new starts mid-shutdown.
  clearInterval(sweepTimer);
  if (typeof queue.stop === 'function') {
    try {
      queue.stop();
    } catch (err) {
      logger.warn('server', 'queue.stop() failed during shutdown:', err.message);
    }
  }

  const forced = setTimeout(() => {
    logger.warn('server', 'in-flight requests did not finish in 10s — forcing exit.');
    process.exit(1);
  }, 10000);
  forced.unref();

  server.close(() => {
    clearTimeout(forced);
    try {
      db.close();
      logger.info('server', 'database closed cleanly.');
    } catch (err) {
      logger.warn('server', 'db.close() failed:', err.message);
    }
    process.exit(0);
  });
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

// A crash that is not handled leaves the process in an undefined state; log it
// explicitly so the cause is visible in the logs rather than inferred from an
// unexplained exit code.
process.on('unhandledRejection', (reason) => {
  logger.error('server', 'unhandled promise rejection:', reason);
});
process.on('uncaughtException', (err) => {
  logger.error('server', 'uncaught exception:', err);
  shutdown('uncaughtException');
});
