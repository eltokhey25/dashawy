/**
 * @file   Structured, tagged application logger
 * @module utils/logger
 *
 * One entry point for every backend log line, so each carries a timestamp and a
 * subsystem tag and can be filtered by grep. Replaces ~40 bare `console.*` calls
 * whose output could not be attributed to a subsystem after the fact.
 *
 * Deliberately dependency-free: this is the only logging surface, and adding a
 * transport (CloudWatch, pino, a file sink) later means changing this file.
 *
 * @author Eltokhey
 * @since  2026-10
 */

// Where each subsystem's output goes.
const LEVELS = { info: 'log', warn: 'warn', error: 'error' };

/**
 * Formats one log line.
 *
 * @param {string} level  one of info | warn | error
 * @param {string} tag    subsystem name, e.g. 'db', 'push', 'auth'
 * @param {string} msg    human-readable message
 * @param {...unknown} args  extra values appended after the message
 * @returns {string}
 */
function format(level, tag, msg, args) {
  const stamp = new Date().toISOString();
  const levelLabel = level.toUpperCase().padEnd(5);
  const tagLabel = String(tag || 'app').padEnd(10);
  const tail = args && args.length ? ' ' + args.map((a) => safe(a)).join(' ') : '';
  return `[${stamp}] [${levelLabel}] [${tagLabel}] ${msg}${tail}`;
}

/**
 * Renders an argument without ever throwing.
 *
 * A logger that crashes the request it is logging is worse than no logger, and
 * `JSON.stringify` throws on a circular reference or a BigInt — both of which can
 * reach here from a database row or a request body.
 *
 * @param {unknown} value
 * @returns {string}
 */
function safe(value) {
  if (typeof value === 'string') return value;
  if (value instanceof Error) return value.stack || value.message;
  try {
    return JSON.stringify(value);
  } catch (err) {
    return String(value);
  }
}

const logger = {
  /**
   * Normal progress: boot, migrations, sweeps.
   *
   * @param {string} tag
   * @param {string} msg
   * @param {...unknown} args
   */
  info(tag, msg, ...args) {
    console.log(format('info', tag, msg, args));
  },

  /**
   * Something unexpected but survivable: a blocked CORS origin, a rate-limit hit.
   *
   * @param {string} tag
   * @param {string} msg
   * @param {...unknown} args
   */
  warn(tag, msg, ...args) {
    console.warn(format('warn', tag, msg, args));
  },

  /**
   * A failure that needs attention. The full error (stack included) is logged;
   * the HTTP layer separately decides what the client is allowed to see.
   *
   * @param {string} tag
   * @param {string} msg
   * @param {...unknown} args
   */
  error(tag, msg, ...args) {
    console.error(format('error', tag, msg, args));
  },
};

module.exports = logger;
