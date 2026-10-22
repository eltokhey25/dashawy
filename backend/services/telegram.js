/**
 * @file   Telegram bot
 * @module services/telegram
 *
 * Long-polls the Telegram Bot API with the token from `TELEGRAM_BOT_TOKEN`
 * (loaded from `.env` by config.js). The bot has one job: consume
 * `/start <token>` links created by `POST /api/auth/telegram-link`, record the
 * caller's chat id on the matching account, and confirm in Arabic. Once linked,
 * the same chat receives the login codes issued by `/api/auth/request-otp`.
 *
 * There is deliberately no third-party Telegram package: Node 20 has a global
 * `fetch`, and the two API methods this feature needs (`getUpdates`,
 * `sendMessage`) do not justify another dependency to audit and pin.
 *
 * @author Eltokhey
 * @since  2026-10
 */
const db = require('../db');
const logger = require('../utils/logger');

const DEFAULT_API_BASE = 'https://api.telegram.org';

// Overridable so the bot can be exercised against a local mock during tests
// without ever touching a real token or sending a real message.
const API_BASE = (process.env.TELEGRAM_API_BASE || DEFAULT_API_BASE).replace(/\/+$/, '');

const LINK_SUCCESS_MESSAGE = 'تم ربط حسابك بنجاح! يمكنك الآن استلام أكواد الدخول هنا.';
const LINK_INVALID_MESSAGE = 'رابط الربط غير صالح أو انتهت صلاحيته. اطلب رابطاً جديداً من الموقع.';
const LINK_WELCOME_MESSAGE =
  'أهلاً بك في دشاوي. لربط حسابك، افتح صفحة الربط من الموقع ثم اضغط على الرابط.';

const POLL_TIMEOUT_SECONDS = 30;
const NETWORK_BACKOFF_MS = 5000;

let running = false;
let offset = 0;
let activeController = null;
let cachedBotUsername = process.env.TELEGRAM_BOT_USERNAME || null;

/**
 * @returns {string} the configured bot token, or an empty string.
 */
function getToken() {
  return String(process.env.TELEGRAM_BOT_TOKEN || '').trim();
}

/**
 * Whether a token is configured. The server stays up without one; the bot is
 * simply disabled, exactly like Web Push when its keys are absent.
 *
 * @returns {boolean}
 */
function isConfigured() {
  return Boolean(getToken());
}

/**
 * Whether the polling loop is currently running.
 *
 * @returns {boolean}
 */
function isRunning() {
  return running;
}

/**
 * Sends one Bot API request.
 *
 * @param {string} method   e.g. 'getUpdates'
 * @param {object} [payload]
 * @param {number} [timeoutMs]
 * @returns {Promise<any>} the `result` field of a successful response
 */
async function apiCall(method, payload, timeoutMs) {
  const token = getToken();
  if (!token) throw new Error('TELEGRAM_BOT_TOKEN is not set');

  const controller = new AbortController();
  activeController = controller;
  const timer = setTimeout(() => controller.abort(), timeoutMs || 15000);

  try {
    const res = await fetch(`${API_BASE}/bot${token}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
      signal: controller.signal,
    });

    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }

    if (!res.ok || !data.ok) {
      const err = new Error(data.description || `Telegram API HTTP ${res.status}`);
      err.status = res.status;
      err.telegram = data;
      throw err;
    }
    return data.result;
  } finally {
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

/**
 * Sends a plain-text message to a chat. Plain text on purpose: no parse_mode
 * means a code containing `_` or `*` can never break the message.
 *
 * @param {number|string} chatId
 * @param {string} text
 * @returns {Promise<object>}
 */
function sendMessage(chatId, text) {
  return apiCall('sendMessage', {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  });
}

/**
 * Sends a login code to a linked chat.
 *
 * A chat the bot cannot reach (the user blocked it, or the account was deleted)
 * is surfaced to the caller as a `telegram_blocked` error so request-otp can
 * clear the stale link and tell the browser to re-link.
 *
 * @param {number|string} chatId
 * @param {string} otp
 * @returns {Promise<void>}
 */
async function sendOtp(chatId, otp) {
  const text =
    'كود الدخول الخاص بك: ' +
    otp +
    '\nالكود صالح لمدة 10 دقائق. لو مش إنت اللي طلبته، تجاهل الرسالة.';

  try {
    await sendMessage(chatId, text);
  } catch (err) {
    // 400 "chat not found" and 403 "bot was blocked" both mean this chat id is
    // no longer usable, which is a linking problem rather than a Telegram
    // outage. Everything else stays a delivery failure.
    const description = String((err.telegram && err.telegram.description) || err.message || '');
    if (
      err.status === 403 ||
      /chat not found|blocked by the user|user is deactivated/i.test(description)
    ) {
      const blocked = new Error('Telegram chat is no longer reachable');
      blocked.code = 'telegram_blocked';
      throw blocked;
    }
    throw err;
  }
}

/**
 * Stores a chat id on a user account.
 *
 * The update is a small transaction because a chat can only belong to one
 * account: any other account still holding this chat id is unlinked first, so
 * a second person using the same Telegram account does not hit the partial
 * UNIQUE index and leave the first account pointing at a chat they no longer
 * control.
 *
 * @param {number|string} userId
 * @param {number|string} chatId
 * @returns {boolean} true when the row was updated
 */
function linkChatIdToUser(userId, chatId) {
  const id = Number(userId);
  const chat = String(chatId == null ? '' : chatId).trim();

  if (!Number.isInteger(id) || id <= 0) throw new Error('linkChatIdToUser: invalid user id');
  if (!chat) throw new Error('linkChatIdToUser: invalid chat id');

  const run = db.transaction(() => {
    db.prepare(
      'UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ? AND id <> ?'
    ).run(chat, id);
    const result = db.prepare('UPDATE users SET telegram_chat_id = ? WHERE id = ?').run(chat, id);
    if (result.changes !== 1) throw new Error('linkChatIdToUser: user not found');
  });

  run();
  return true;
}

/**
 * Handles one update from getUpdates.
 *
 * The token lookup, the link and the delete run in one transaction. If two
 * /start messages race with the same token, the loser finds no row and gets the
 * invalid-link answer, and the chat is linked exactly once.
 *
 * @param {object} update
 * @returns {Promise<void>}
 */
async function handleUpdate(update) {
  const message = update && (update.message || update.edited_message);
  if (!message || !message.chat || message.chat.id == null) return;

  const text = typeof message.text === 'string' ? message.text.trim() : '';
  const match = text.match(/^\/start(?:@[A-Za-z0-9_]+)?(?:\s+(\S+))?/i);
  if (!match) return;

  const chatId = message.chat.id;
  const token = match[1];

  if (!token) {
    await sendMessage(chatId, LINK_WELCOME_MESSAGE);
    return;
  }

  const claimed = db.transaction(() => {
    const row = db
      .prepare(
        `SELECT user_id FROM telegram_link_tokens
          WHERE token = ? AND expires_at > datetime('now')`
      )
      .get(token);

    if (!row) {
      // Housekeeping: an expired token can never become valid again.
      db.prepare(
        "DELETE FROM telegram_link_tokens WHERE token = ? AND expires_at <= datetime('now')"
      ).run(token);
      return null;
    }

    // Order matters and is the one the feature spec asks for: read the user id,
    // link the chat, then delete the token so the link cannot be replayed.
    linkChatIdToUser(row.user_id, chatId);
    db.prepare('DELETE FROM telegram_link_tokens WHERE token = ?').run(token);
    return { userId: row.user_id };
  })();

  if (!claimed) {
    await sendMessage(chatId, LINK_INVALID_MESSAGE);
    return;
  }

  logger.info('telegram', `[telegram] chat ${chatId} linked to user ${claimed.userId}.`);
  await sendMessage(chatId, LINK_SUCCESS_MESSAGE);
}

/**
 * Calls Telegram's getMe. Also caches the username for the linking page.
 *
 * @returns {Promise<{id:number, username?:string, first_name?:string}>}
 */
async function fetchBotUsername() {
  const me = await apiCall('getMe', {}, 15000);
  if (me && me.username) cachedBotUsername = me.username;
  return cachedBotUsername;
}

/**
 * @returns {string|null} the bot's @username without the leading @, if known
 */
function getBotUsername() {
  return cachedBotUsername
    ? String(cachedBotUsername).replace(/^@/, '')
    : process.env.TELEGRAM_BOT_USERNAME
      ? String(process.env.TELEGRAM_BOT_USERNAME).replace(/^@/, '')
      : null;
}

/**
 * @param {number} ms
 * @returns {Promise<void>}
 */
function sleep(ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    // A backoff must not keep a shutting-down process alive.
    if (typeof timer.unref === 'function') timer.unref();
  });
}

/**
 * The long-poll loop. `getUpdates` itself waits up to 30 seconds for a message,
 * so this is one request per half minute while idle rather than a busy loop.
 *
 * @returns {Promise<void>}
 */
async function pollLoop() {
  while (running) {
    try {
      const updates = await apiCall(
        'getUpdates',
        {
          offset: offset || undefined,
          timeout: POLL_TIMEOUT_SECONDS,
          allowed_updates: ['message'],
        },
        (POLL_TIMEOUT_SECONDS + 10) * 1000
      );

      if (!Array.isArray(updates)) continue;

      for (const update of updates) {
        if (update && typeof update.update_id === 'number' && update.update_id >= offset) {
          offset = update.update_id + 1;
        }
        try {
          await handleUpdate(update);
        } catch (err) {
          logger.error(
            'telegram',
            `[telegram] failed to handle update ${update && update.update_id}:`,
            err.message
          );
        }
      }
    } catch (err) {
      if (!running) break;

      // 401 is not transient: a wrong or revoked token will fail every poll, so
      // stop instead of hammering the API and flooding the logs.
      if (err.status === 401) {
        logger.error(
          'telegram',
          '[telegram] TELEGRAM_BOT_TOKEN was rejected by Telegram; bot stopped.'
        );
        running = false;
        break;
      }

      logger.warn('telegram', `[telegram] polling failed: ${err.message}`);
      await sleep(NETWORK_BACKOFF_MS);
    }
  }
}

/**
 * Starts the bot. Safe to call more than once; an already-running bot is left
 * alone. Without a token it logs a warning and returns false, so the HTTP
 * server still boots in an installation that has not configured Telegram.
 *
 * @returns {boolean} whether polling was started
 */
function start() {
  if (running) return true;

  if (!isConfigured()) {
    logger.warn('telegram', '[telegram] TELEGRAM_BOT_TOKEN is not set — Telegram bot disabled.');
    return false;
  }

  running = true;
  logger.info('telegram', '[telegram] bot listening — long-polling getUpdates for /start links.');

  // Identity is logged in the background; a getMe failure must not stop the
  // poll loop (Telegram may be briefly unreachable) and must not reject.
  fetchBotUsername()
    .then((username) => {
      if (username) logger.info('telegram', `[telegram] authenticated as @${username}.`);
    })
    .catch((err) => {
      logger.warn('telegram', `[telegram] getMe failed: ${err.message}`);
    });

  pollLoop().catch((err) => {
    logger.error('telegram', '[telegram] polling loop crashed:', err.message);
    running = false;
  });

  return true;
}

/**
 * Stops polling and aborts an in-flight getUpdates. Called from the server's
 * graceful shutdown so SIGTERM does not wait out the 30-second long poll.
 *
 * @returns {void}
 */
function stop() {
  running = false;
  if (activeController) {
    try {
      activeController.abort();
    } catch (e) {
      /* already aborted */
    }
    activeController = null;
  }
}

module.exports = {
  start,
  stop,
  isConfigured,
  isRunning,
  linkChatIdToUser,
  sendMessage,
  sendOtp,
  fetchBotUsername,
  getBotUsername,
  handleUpdate,
  LINK_SUCCESS_MESSAGE,
};
