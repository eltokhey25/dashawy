/**
 * @file   One-time interactive admin creation script
 * @module scripts/create-admin
 *
 * Creates a single administrator account by asking for its details at the terminal. Runs against
 * the application's own database module, so the pending schema and migrations are applied exactly
 * as they are at server boot — but no HTTP server is started and no queue or sweep worker is
 * launched.
 *
 * This exists for the case the API cannot cover: an admin has to exist before anyone can log in
 * as one, and the /api/admin/admins route is itself behind requireSuperAdmin. When the last super
 * admin is gone, the console is the only way back in.
 *
 * Usage (from the repository root or anywhere else — every path is resolved from __dirname):
 *   node scripts/create-admin.js
 *
 * Inside Railway, from a machine with a real terminal:
 *   railway run node scripts/create-admin.js
 *   railway ssh v2   # then: cd /app && node scripts/create-admin.js
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Interactive admin creation. Deliberately one-shot: it creates exactly one account and is not
// meant to stay in the repository.
//
// The password is read from the terminal, typed twice, with the echo switched off. It is never
// accepted as an argv value or an environment variable, never printed, and never logged — a
// password passed as `node create-admin.js --password=...` or ADMIN_PASSWORD=... ends up in the
// shell history, in `ps`, and in Railway's variable list, which is the opposite of what this
// script is for. The guard below refuses to run while such a variable is still set.
const path = require('path');
const readline = require('readline');
const bcrypt = require('bcryptjs');

// Resolved from __dirname rather than from process.cwd(), so the script behaves identically
// whether it is run from /app, from the repository root, or through an absolute path.
// Requiring backend/config also enforces the production JWT_SECRET policy, exactly as it does
// for the server — this script is not a way to boot an app that could not boot.
const config = require(path.join(__dirname, '..', 'backend', 'config'));
const db = require(path.join(__dirname, '..', 'backend', 'db'));
const { normalizeEmail, isValidEmail, passwordPolicyError } = require(
  path.join(__dirname, '..', 'backend', 'utils', 'validation')
);

// 12 rounds, matching backend/routes/admin.routes.js (the admin the UI creates) and
// backend/routes/auth.routes.js (normal registration). The seed uses 10, so this script does not
// follow the seed: a console-created admin gets the same cost as one created through the app.
const BCRYPT_ROUNDS = 12;

// Bcrypt silently truncates anything past 72 bytes, so a longer password is a lie about its own
// strength. passwordPolicyError only enforces the app's own 8..200 range and says nothing about
// this limit, so it is checked separately rather than assumed.
const BCRYPT_MAX_BYTES = 72;

// Leftover variables from the sibling reset scripts. Their presence means a real password is
// sitting in the environment (or in Railway's variable list) where the script cannot see it,
// which is the mistake this script exists to avoid.
const LEAKED_PASSWORD_VARS = ['RESET_PASSWORD', 'ADMIN_PASSWORD', 'CREATE_ADMIN_PASSWORD'];

// ---------------------------------------------------------------------------
// Terminal
// ---------------------------------------------------------------------------

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: true,
});

// Lines are matched to questions through this queue instead of through rl.question()'s own
// callback.
//
// readline emits a 'line' event per line, and a question registered with rl.question() only
// catches the line that arrives while it is waiting. Anything typed or piped in faster than the
// prompts — a pasted block, a redirected input file under a pty — arrives when no question is
// registered and is silently dropped, which leaves the script waiting on an answer that was
// already given. Buffering the lines here makes input arrive when it arrives rather than when it
// was asked for.
const waiting = [];
const queued = [];

rl.on('line', (answer) => {
  if (waiting.length) waiting.shift()(answer);
  else queued.push(answer);
});

// stdin closing with a question still unanswered means there is nothing left to read. Without
// this the process would sit on a prompt forever.
rl.on('close', () => {
  if (waiting.length) {
    console.log('');
    console.log('انتهى الإدخال قبل اكتمال الطلب. لم يُنشأ أي حساب.');
    finish(1);
  }
});

/**
 * Asks one question and resolves with the answer.
 *
 * With `hidden`, the characters typed are not echoed. readline writes each keystroke through its
 * private `_writeToOutput`, so it is swapped out for the duration of the question: the prompt
 * itself still gets through, everything after it is dropped.
 *
 * @param {string} question
 * @param {boolean} [hidden=false]
 * @returns {Promise<string>}
 */
function ask(question, hidden) {
  const originalWrite = rl._writeToOutput;
  if (hidden) {
    rl._writeToOutput = function maskedWrite(chunk) {
      if (chunk.indexOf(question) !== -1) originalWrite.call(this, chunk);
    };
  }

  return new Promise((resolve) => {
    const deliver = (answer) => {
      rl._writeToOutput = originalWrite;
      if (hidden) process.stdout.write('\n');
      resolve(answer);
    };
    if (queued.length) deliver(queued.shift());
    else waiting.push(deliver);
  });
}

function line(char) {
  return char.repeat(64);
}

/**
 * Asks until the answer passes `validate`.
 *
 * @param {object} options
 * @param {string} options.question
 * @param {(v: string) => string|null} options.validate  returns an error message, or null when fine
 * @param {boolean} [options.hidden=false]
 * @param {number} [options.attempts=5]  a create script is not a login form: after this many
 *   tries something is wrong with the terminal, and looping forever would hide that.
 * @returns {Promise<string>}
 */
async function askUntilValid({ question, validate, hidden, attempts }) {
  const max = attempts || 5;
  for (let i = 1; i <= max; i++) {
    const answer = (await ask(question, hidden)).trim();
    const problem = validate(answer);
    if (!problem) return answer;
    console.log('  ! ' + problem);
    if (i === max) throw new Error('أُلغيت العملية بعد محاولات متكررة.');
  }
  throw new Error('unreachable');
}

// ---------------------------------------------------------------------------
// Exit paths
// ---------------------------------------------------------------------------

function finish(code) {
  try {
    rl.close();
  } catch (err) {
    /* the interface may already be closed */
  }
  try {
    db.close();
  } catch (err) {
    /* closing is best effort; the process exits either way */
  }
  process.exit(code);
}

rl.on('SIGINT', () => {
  console.log('\nتم الإلغاء. لم يُنشأ أي حساب.');
  finish(130);
});

// ---------------------------------------------------------------------------
// Preflight
// ---------------------------------------------------------------------------

function preflight() {
  const leaked = LEAKED_PASSWORD_VARS.filter((name) => process.env[name]);
  if (leaked.length) {
    console.error(
      'رفض التشغيل: ' +
        leaked.join('، ') +
        ' مُعتمد في البيئة. ' +
        'كلمة المرور يجب أن تُكتب على الطرفية فقط، لا كمتغير بيئة.'
    );
    console.error('أزلها من متغيرات Railway ثم أعد المحاولة.');
    finish(1);
  }

  // No TTY means no prompt can be answered: reading stdin would either block forever or read
  // EOF and loop on empty input. Say so plainly instead of hanging in a deploy log.
  if (!process.stdin.isTTY) {
    console.error('هذا البرنامج يقرأ من الطرفية ولا يعمل بدونها.');
    console.error('شغّله من جهاز فيه طرفية حقيقية، مثل:');
    console.error('  railway run node scripts/create-admin.js');
    console.error('  railway ssh v2   ثم داخل الخادم:  cd /app && node scripts/create-admin.js');
    finish(1);
  }

  // The same probe backend/server.js runs before app.listen(). Requiring backend/db has already
  // run initSchema() and every pending migration, so by this point the table is guaranteed to
  // have the current columns and the unique indexes this insert depends on.
  db.prepare('SELECT 1').get();
}

/**
 * The full name of an admin, but only if the schema actually has a column for it.
 *
 * Today it does not: an admin is identified by email alone, has no row in `customers` or
 * `technicians`, and admin.routes.js inserts the same five columns. The question is therefore not
 * asked. It is still looked up rather than assumed, so if a future migration adds a NOT NULL name
 * column the script asks for it instead of failing at the INSERT with a bare constraint error.
 *
 * @returns {string|null} the column name, or null when no name is required
 */
function requiredNameColumn() {
  return (
    db
      .prepare('PRAGMA table_info(users)')
      .all()
      .find((c) => c.notnull === 1 && c.dflt_value === null && /name/i.test(c.name)) || null
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  preflight();

  console.log(line('='));
  console.log('إنشاء حساب مدير جديد (مرة واحدة)');
  console.log(line('='));
  console.log('قاعدة البيانات : ' + config.DATABASE_PATH);
  console.log('البيئة        : ' + (config.IS_PRODUCTION ? 'production' : 'development'));
  console.log('');

  // --- email ------------------------------------------------------------
  const email = await askUntilValid({
    question: 'البريد الإلكتروني للمدير: ',
    validate: (v) => {
      if (!v) return 'البريد الإلكتروني مطلوب.';
      if (!isValidEmail(v)) return 'صيغة البريد الإلكتروني غير صحيحة.';
      return null;
    },
  });
  const normalizedEmail = normalizeEmail(email);

  // Checked with the same normalisation the login lookup uses
  // (auth.routes.js compares against normalizeEmail(input)), so a differently-cased address
  // cannot slip past this check and then collide with the UNIQUE index on insert.
  const existing = db
    .prepare(`SELECT id, email, role, is_super_admin, blocked FROM users WHERE email = ?`)
    .get(normalizedEmail);

  if (existing) {
    console.log('');
    console.log('هذا البريد الإلكتروني مسجّل بالفعل — لم يتغيّر أي شيء.');
    console.log('  المعرف : ' + existing.id);
    console.log('  البريد : ' + existing.email);
    console.log('  الدور  : ' + existing.role);
    console.log('  مدير عام: ' + (existing.is_super_admin ? 'نعم' : 'لا'));
    console.log('  محظور : ' + (existing.blocked ? 'نعم' : 'لا'));
    console.log('');
    console.log(
      'لن يتم استبدال كلمة المرور أو أي بيانات أخرى. ' +
        'إن كنت تبحث عن حساب جديد فاستخدم بريدًا آخر.'
    );
    finish(1);
  }

  // --- full name (only if the schema wants one) -------------------------
  const nameColumn = requiredNameColumn();
  let name = null;
  if (nameColumn) {
    name = await askUntilValid({
      question: 'الاسم الكامل: ',
      validate: (v) => {
        if (!v) return 'الاسم مطلوب.';
        if (v.length > 100) return 'الاسم طويل جدًا.';
        return null;
      },
    });
  }

  // --- super admin ------------------------------------------------------
  // backend/middleware/auth.js lets a super admin past every permission check, while an admin
  // with is_super_admin = 0 and permissions = '[]' can log in and then reach nothing — including
  // /admin/dashboard. That is why this defaults to yes rather than to the column default.
  const superAnswer = (await ask('هل يكون مديرًا عامًا (صلاحية كاملة)؟ [Y/n]: '))
    .trim()
    .toLowerCase();
  const isSuperAdmin =
    superAnswer === '' || superAnswer === 'y' || superAnswer === 'yes' || superAnswer === 'ن';

  // --- password ---------------------------------------------------------
  let password = await askUntilValid({
    question: 'كلمة المرور (لا تظهر أثناء الكتابة): ',
    hidden: true,
    validate: (v) => {
      const problem = passwordPolicyError(v);
      if (problem) return problem;
      if (Buffer.byteLength(v, 'utf8') > BCRYPT_MAX_BYTES) {
        return 'كلمة المرور طويلة جدًا (الحد الأقصى ' + BCRYPT_MAX_BYTES + ' بايت).';
      }
      return null;
    },
  });

  const confirm = await ask('أعد كتابة كلمة المرور: ', true);
  if (confirm !== password) {
    // Deliberately does not echo either value, and says nothing about which one was wrong.
    console.log('');
    console.log('كلمتا المرور غير متطابقتين. لم يُنشأ أي حساب.');
    password = '';
    confirm.replace(/./g, '');
    finish(1);
  }

  // The confirmation has served its purpose. Nothing downstream needs it in memory.
  confirm.replace(/./g, '');

  // --- create -----------------------------------------------------------
  const hash = bcrypt.hashSync(password, BCRYPT_ROUNDS);
  password = '';

  // Mirrors the INSERT in backend/routes/admin.routes.js exactly — same columns, same order,
  // same bcrypt cost. phone is left NULL (admins log in by email; see the users table comment)
  // and google_sub NULL, which keeps it out of both partial UNIQUE indexes.
  const columns = [
    'email',
    'password_hash',
    'role',
    'is_super_admin',
    'must_change_password',
    'permissions',
  ];
  const values = [
    normalizedEmail,
    hash,
    config.LEVELS.admin,
    isSuperAdmin ? 1 : 0,
    0,
    JSON.stringify([]),
  ];

  if (nameColumn) {
    columns.push(nameColumn);
    values.push(name);
  }

  const result = db
    .prepare(
      `INSERT INTO users (${columns.join(', ')}) VALUES (${columns.map(() => '?').join(', ')})`
    )
    .run(...values);

  const created = db
    .prepare(
      `SELECT id, email, role, is_super_admin, blocked, must_change_password, permissions
       FROM users WHERE id = ?`
    )
    .get(result.lastInsertRowid);

  console.log('');
  console.log(line('='));
  console.log('تم إنشاء الحساب:');
  console.log('  المعرف        : ' + created.id);
  console.log('  البريد        : ' + created.email);
  console.log('  الدور         : ' + created.role);
  console.log('  مدير عام      : ' + (created.is_super_admin ? 'نعم' : 'لا'));
  console.log('  محظور         : ' + (created.blocked ? 'نعم' : 'لا'));
  console.log('  تغيير كلمة المرور مطلوب: ' + (created.must_change_password ? 'نعم' : 'لا'));
  console.log(
    '  الصلاحيات     : ' +
      created.permissions +
      (created.is_super_admin ? ' (المدير العام يتجاوز فحص الصلاحيات)' : '')
  );
  if (nameColumn) console.log('  الاسم         : ' + created[nameColumn]);
  console.log(line('='));
  console.log('سجّل الدخول من /login بهذا البريد وكلمة المرور.');
  if (!created.is_super_admin) {
    console.log('');
    console.log('تنبيه: الحساب ليس مديرًا عامًا، وبدون صلاحيات محددة');
    console.log('سيتمكّن من الدخول لكنه سيجد كل صفحات الإدارة مغلقة عليه.');
  }
  console.log('هذا البرنامج للاستخدام مرة واحدة — احذفه من المستودع بعد الاستخدام.');

  finish(0);
}

main().catch((err) => {
  console.error('');
  console.error('فشل إنشاء الحساب: ' + (err && err.message ? err.message : String(err)));
  console.error('لم يُنشأ أي حساب.');
  finish(1);
});
