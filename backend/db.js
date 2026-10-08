/**
 * @file   SQLite connection and schema bootstrap
 * @module db
 *
 * Opens the better-sqlite3 database, applies database/schema.sql on first run, and seeds
 * default specialties and settings. Exports the shared db handle used by every service and
 * route.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// SQLite connection + schema initialisation.
const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');
const config = require('./config');
const { normalizePhone } = require('./utils/validation');
const logger = require('./utils/logger');

// Ensure the database directory exists.
fs.mkdirSync(path.dirname(config.DATABASE_PATH), { recursive: true });

const db = new Database(config.DATABASE_PATH);

// Enforce foreign keys and sane defaults.
db.pragma('foreign_keys = ON');
db.pragma('journal_mode = WAL');

// Load the schema if the database has not been initialised yet.
function initSchema() {
  if (!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").get()) {
    const schema = fs.readFileSync(config.SCHEMA_PATH, 'utf8');
    db.exec(schema);
    logger.info('db', `[db] Schema initialised.`);
  }
}

initSchema();

initSchema();

// Lightweight migration: add reference tables (specialties, settings) on
// existing databases that were created before these tables existed.
function migrate() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS specialties (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      key        TEXT NOT NULL UNIQUE,
      label      TEXT NOT NULL,
      icon       TEXT NOT NULL DEFAULT 'fa-solid fa-wrench',
      enabled    INTEGER NOT NULL DEFAULT 1,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL DEFAULT ''
    );

    -- Technician profiles, portfolio gallery and customer reviews.
    CREATE TABLE IF NOT EXISTS technician_profiles (
      technician_id      INTEGER PRIMARY KEY,
      bio                TEXT,
      years_experience   INTEGER DEFAULT 0,
      profile_image_path TEXT,
      updated_at         TEXT DEFAULT (datetime('now')),
      FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS technician_portfolio (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      technician_id INTEGER NOT NULL,
      image_path    TEXT NOT NULL,
      caption       TEXT,
      created_at    TEXT DEFAULT (datetime('now')),
      FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reviews (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id       INTEGER NOT NULL UNIQUE,
      customer_id      INTEGER NOT NULL,
      technician_id    INTEGER NOT NULL,
      rating           INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
      comment          TEXT,
      technician_reply TEXT,
      replied_at       TEXT,
      created_at       TEXT DEFAULT (datetime('now')),
      FOREIGN KEY (request_id)    REFERENCES requests(id)    ON DELETE CASCADE,
      FOREIGN KEY (customer_id)   REFERENCES customers(id)   ON DELETE CASCADE,
      FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_portfolio_technician ON technician_portfolio(technician_id);
    CREATE INDEX IF NOT EXISTS idx_reviews_technician  ON reviews(technician_id);
  `);

  // Seed specialties from config if the table is empty.
  const seeded = db.prepare('SELECT COUNT(*) AS n FROM specialties').get().n;
  if (seeded === 0) {
    const insert = db.prepare(
      'INSERT INTO specialties (key, label, icon, enabled, sort_order) VALUES (?, ?, ?, ?, ?)'
    );
    const tx = db.transaction(() => {
      config.SPECIALTIES.forEach((s, i) => {
        insert.run(s.key, s.label, s.icon || config.DEFAULT_SPECIALTY_ICON, 1, i);
      });
    });
    tx();
    logger.info('db', `[db] Seeded ` + config.SPECIALTIES.length + ' specialties.');
  }

  // Backfill specialties added to config AFTER a database was first seeded.
  //
  // The block above only ever runs on an empty table, so a specialty introduced in a
  // later release stayed invisible: no row meant no option in the request form, and
  // `isValidSpecialty` rejected it — even though ensureServicePricing below was
  // already pricing it. A database created before signal_tuning existed therefore
  // offered four services while config listed five.
  //
  // INSERT OR IGNORE keyed on `key` so an existing row is never touched: its
  // label, icon, enabled flag and sort_order are admin-editable and must survive.
  // sort_order comes from the config index, which is the slot the missing entry
  // would have taken.
  const insertSpecialty = db.prepare(
    'INSERT OR IGNORE INTO specialties (key, label, icon, enabled, sort_order) VALUES (?, ?, ?, ?, ?)'
  );
  config.SPECIALTIES.forEach((s, i) => {
    const info = insertSpecialty.run(s.key, s.label, s.icon || config.DEFAULT_SPECIALTY_ICON, 1, i);
    if (info.changes > 0) logger.info('db', `[db] Added missing specialty: ` + s.key);
  });

  // Seed default settings.
  const settingsCount = db.prepare('SELECT COUNT(*) AS n FROM settings').get().n;
  if (settingsCount === 0) {
    const insert = db.prepare('INSERT INTO settings (key, value) VALUES (?, ?)');
    const tx = db.transaction(() => {
      const defaults = {
        site_name: 'صلحها',
        support_phone: '',
        support_email: 'support@salahha.example',
        auto_assign_enabled: '1',
        auto_assign_radius_km: '25',
      };
      Object.entries(defaults).forEach(([k, v]) => insert.run(k, v));
    });
    tx();
    logger.info('db', `[db] Seeded default settings.`);
  }

  // The seed above only runs on a completely empty settings table, so databases
  // created before the site-settings keys existed never receive them. INSERT OR
  // IGNORE is idempotent: existing values (including admin edits) are preserved.
  const ensureSetting = db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)');
  ensureSetting.run('site_name', 'دشاوي');
  ensureSetting.run(
    'site_tagline',
    'خدمة متخصصة في تركيب وصيانة أطباق الدش والرسيفرات في الإسكندرية'
  );
  ensureSetting.run('contact_phone', '01025330520');
  ensureSetting.run('contact_email', 'meltokhey39@gmail.com');
  ensureSetting.run('contact_address', 'الإسكندرية، مصر');
  ensureSetting.run('whatsapp', '+201025330520');
  ensureSetting.run('facebook_url', '');
  ensureSetting.run('instagram_url', '');
  ensureSetting.run('working_hours', 'يومياً من 9 ص إلى 10 م');
  ensureSetting.run('hero_title', '');
  ensureSetting.run('hero_subtitle', '');
  // Payments. Seeded here as well as in the block above so a database created
  // before payments existed still gets them; INSERT OR IGNORE never overwrites
  // a value an admin has since edited.
  ensureSetting.run('instapay_number', '01025330520');
  ensureSetting.run('instapay_name', 'محمد الطوخي');
  ensureSetting.run('wallet_debt_limit', '300');
  ensureSetting.run('payment_methods', 'cash,instapay');
  ensureSetting.run('commission_percent', '20');

  ensureServicePricing();

  migrateBroadcastVocabulary();
  migrateRequestStatuses();
  ensureQueueColumns();
  ensureWalletSchema();

  ensureCashConfirmationSchema();

  migratePhoneIdentity();
}

// Adds the waiting-queue columns to a requests table that predates them.
//
// ALTER TABLE ADD COLUMN is the one schema change SQLite does allow in place, and
// it is the right tool here: the four columns are all nullable (or have a
// constant default), so appending them cannot rewrite existing rows. This is also
// why they have to be added separately from migrateRequestStatuses() — that one
// REBUILDS the table from schema.sql, so without this a database rebuilt after
// the queue feature shipped would silently lose the columns.
//
// Idempotent by inspection: each column is only added when PRAGMA says it is
// missing, so re-running is a no-op.
function ensureQueueColumns() {
  const columns = [
    ['is_queued', 'is_queued INTEGER NOT NULL DEFAULT 0'],
    ['queued_at', 'queued_at TEXT'],
    ['queue_expires_at', 'queue_expires_at TEXT'],
    ['cancel_reason', 'cancel_reason TEXT'],
  ];
  if (!tableDdl('requests')) return;

  const existing = new Set(
    db
      .prepare('PRAGMA table_info(requests)')
      .all()
      .map((c) => c.name)
  );
  columns.forEach(([name, ddl]) => {
    if (existing.has(name)) return;
    db.exec('ALTER TABLE requests ADD COLUMN ' + ddl);
    logger.info('db', `[db] Added requests.` + name);
  });
}

// ---------------------------------------------------------------------------
// Broadcast / status vocabulary migrations
// ---------------------------------------------------------------------------
//
// The broadcast lifecycle and the request status set both live in SQLite CHECK
// constraints, and SQLite cannot ALTER one — a constraint can only change by
// rebuilding the table. Both migrations below therefore follow the same shape:
//
//   1. read the table's own DDL from sqlite_master;
//   2. if it already allows the new values, do nothing (so this is a no-op on
//      every boot after the first, and on a database created from schema.sql);
//   3. otherwise rebuild inside a transaction: copy the rows through a value map,
//      drop, rename, recreate the indexes;
//   4. verify row count and run PRAGMA foreign_key_check, throwing on any problem
//      so a failed migration rolls back instead of leaving a half-migrated file.
//
// foreign_keys is switched off around the requests rebuild because DROP TABLE on a
// parent row would otherwise cascade-delete every child row (reviews, broadcasts,
// wallet entries). It must be toggled outside a transaction, which is why it is
// done here rather than inside the run() below.
function tableDdl(table) {
  const row = db
    .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?")
    .get(table);
  return row ? row.sql : null;
}

// request_broadcasts.status: pending/accepted/rejected/lost -> sent/accepted/declined/expired.
//
// 'lost' folds into 'expired' because the spec's vocabulary has no separate state
// for "somebody else won it": from the offer's point of view both mean this row is
// closed and can never be answered. Nothing in the UI distinguishes them — the
// technician inbox only ever lists 'sent' rows, and the admin audit endpoint shows
// whatever the table holds.
const BROADCAST_STATUS_MAP = {
  pending: 'sent',
  sent: 'sent',
  accepted: 'accepted',
  rejected: 'declined',
  declined: 'declined',
  lost: 'expired',
  expired: 'expired',
};

function migrateBroadcastVocabulary() {
  const ddl = tableDdl('request_broadcasts');
  if (!ddl) return;
  // Already migrated (or created fresh from schema.sql).
  if (ddl.includes("'sent'") && !ddl.includes("'pending'")) return;

  logger.info('db', `[db] Migrating request_broadcasts status vocabulary…`);
  const before = db.prepare('SELECT COUNT(*) AS n FROM request_broadcasts').get().n;

  db.pragma('foreign_keys = OFF');
  const run = db.transaction(() => {
    db.exec(`
      CREATE TABLE request_broadcasts_new (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id    INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        technician_id INTEGER NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
        status        TEXT NOT NULL DEFAULT 'sent'
          CHECK (status IN ('sent', 'accepted', 'declined', 'expired')),
        sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
        responded_at  TEXT
      );
    `);

    const rows = db.prepare('SELECT * FROM request_broadcasts').all();
    // SQLite does not allow a target-table column reference inside an INSERT's
    // VALUES list, so the NOT NULL sent_at fallback is resolved here. The format
    // matches datetime('now'): UTC, 'YYYY-MM-DD HH:MM:SS', so the column stays
    // sortable as text against every other row.
    const now = new Date().toISOString().slice(0, 19).replace('T', ' ');
    const insert = db.prepare(
      `INSERT INTO request_broadcasts_new (id, request_id, technician_id, status, sent_at, responded_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    );
    rows.forEach((row) => {
      // An unknown status would violate the new CHECK, so it is normalised to
      // 'expired' (closed) rather than aborting the migration.
      const status = BROADCAST_STATUS_MAP[row.status] || 'expired';
      insert.run(
        row.id,
        row.request_id,
        row.technician_id,
        status,
        row.sent_at || now,
        row.responded_at
      );
    });

    db.exec('DROP TABLE request_broadcasts');
    db.exec('ALTER TABLE request_broadcasts_new RENAME TO request_broadcasts');
    db.exec(`
      CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_unique_pair
        ON request_broadcasts(request_id, technician_id);
      CREATE INDEX IF NOT EXISTS idx_broadcast_tech_status
        ON request_broadcasts(technician_id, status);
      CREATE INDEX IF NOT EXISTS idx_broadcast_request
        ON request_broadcasts(request_id);
    `);
  });
  run();
  db.pragma('foreign_keys = ON');

  const after = db.prepare('SELECT COUNT(*) AS n FROM request_broadcasts').get().n;
  if (after !== before) {
    throw new Error('[db] request_broadcasts migration lost rows: ' + before + ' -> ' + after);
  }
  assertNoBrokenForeignKeys('request_broadcasts');
  logger.info('db', `[db] request_broadcasts migrated, ` + after + ' row(s) kept.');
}

// requests.status: add 'no_technician', the terminal state of an expired broadcast.
//
// The rebuild copies the table definition straight out of schema.sql so the
// migrated shape and a fresh install can never drift apart. Every column is
// listed explicitly for the same reason — a SELECT * would silently depend on
// column order matching between the live table and the file.
const REQUEST_COLUMNS = [
  'id',
  'customer_id',
  'technician_id',
  'specialty',
  'custom_specialty_text',
  'description',
  'location_text',
  'latitude',
  'longitude',
  'status',
  'declined_technicians',
  'created_at',
  'updated_at',
  'assigned_at',
  'completed_at',
  'service_price',
  'commission_percent',
  'commission_amount',
  'technician_earning',
  'payment_method',
  'payment_status',
  'payment_reference',
  'payment_confirmed_at',
  'payment_confirmed_by',
  'work_notes',
  'completed_by_technician_at',
  'is_queued',
  'queued_at',
  'queue_expires_at',
  'cancel_reason',
];

function migrateRequestStatuses() {
  const ddl = tableDdl('requests');
  if (!ddl) return;
  if (ddl.includes('no_technician')) return;

  logger.info('db', `[db] Adding the no_technician request status…`);

  // A database from before the money columns must still migrate, so the copy is
  // limited to the columns the live table actually has.
  const live = new Set(
    db
      .prepare('PRAGMA table_info(requests)')
      .all()
      .map((c) => c.name)
  );
  const columns = REQUEST_COLUMNS.filter((c) => live.has(c));
  if (!columns.includes('status')) throw new Error('[db] requests has no status column');

  const schema = fs.readFileSync(config.SCHEMA_PATH, 'utf8');
  const match = schema.match(/CREATE TABLE IF NOT EXISTS requests \(([\s\S]*?)\n\);/);
  if (!match) throw new Error('[db] could not read the requests definition from schema.sql');
  const targetDdl = match[0].replace(
    'CREATE TABLE IF NOT EXISTS requests',
    'CREATE TABLE requests_new'
  );

  const before = db.prepare('SELECT COUNT(*) AS n FROM requests').get().n;
  const idHighWater = db.prepare('SELECT COALESCE(MAX(id), 0) AS m FROM requests').get().m;

  db.pragma('foreign_keys = OFF');
  const run = db.transaction(() => {
    db.exec(targetDdl);
    db.exec(
      'INSERT INTO requests_new (' +
        columns.join(', ') +
        ') SELECT ' +
        columns.join(', ') +
        ' FROM requests'
    );
    db.exec('DROP TABLE requests');
    db.exec('ALTER TABLE requests_new RENAME TO requests');
    // The AUTOINCREMENT high-water mark lives in sqlite_sequence and is dropped
    // with the old table, so it is restored or the next insert would reuse ids
    // that requests/wallet rows already reference. sqlite_sequence has no unique
    // constraint, so this is an explicit update-then-insert rather than an upsert.
    const bumpSeq = db
      .prepare('UPDATE sqlite_sequence SET seq = ? WHERE name = ?')
      .run(idHighWater, 'requests');
    if (bumpSeq.changes === 0) {
      db.prepare('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)').run(
        'requests',
        idHighWater
      );
    }
    db.exec(`
      CREATE INDEX IF NOT EXISTS idx_requests_status       ON requests(status);
      CREATE INDEX IF NOT EXISTS idx_requests_customer     ON requests(customer_id);
      CREATE INDEX IF NOT EXISTS idx_requests_technician   ON requests(technician_id);
      CREATE INDEX IF NOT EXISTS idx_requests_specialty    ON requests(specialty);
      CREATE INDEX IF NOT EXISTS idx_requests_payment_status ON requests(payment_status);
    `);
  });
  run();
  db.pragma('foreign_keys = ON');

  const after = db.prepare('SELECT COUNT(*) AS n FROM requests').get().n;
  if (after !== before) {
    throw new Error('[db] requests migration lost rows: ' + before + ' -> ' + after);
  }
  assertNoBrokenForeignKeys('requests');
  logger.info('db', `[db] requests migrated, ` + after + ' row(s) kept.');
}

// Fails loudly if a rebuild orphaned a child row. Cheap (one pass over the FK
// metadata) and the only way to know a table swap did not silently drop data
// that other tables point at.
function assertNoBrokenForeignKeys(context) {
  const broken = db.pragma('foreign_key_check');
  if (broken.length) {
    throw new Error(
      '[db] foreign key violations after migrating ' +
        context +
        ': ' +
        JSON.stringify(broken.slice(0, 5))
    );
  }
}

// Splits the old single signed `balance` into the two-pot model: `balance` (credit
// only) and `total_debt` (debt only), both non-negative.
//
// WHY a migration and not a view: the sign is baked into every historical row. A
// technician on -90 had their debt represented as a negative balance, so the split
// has to move the magnitude into total_debt and zero the balance, once. The
// wallet_transactions ledger is left untouched — it is append-only, and its
// `balance_after` values are historical facts rather than current state.
//
// Idempotent throughout: columns are added only when missing, and the backfill only
// touches rows that still carry a negative balance or a NULL total_debt.
function ensureWalletSchema() {
  const addColumns = (table, columns) => {
    const existing = new Set(
      db
        .prepare('PRAGMA table_info(' + table + ')')
        .all()
        .map((c) => c.name)
    );
    columns.forEach(([name, ddl]) => {
      if (existing.has(name)) return;
      db.exec('ALTER TABLE ' + table + ' ADD COLUMN ' + ddl);
      logger.info('db', `[db] Added ` + table + '.' + name);
    });
  };

  addColumns('technician_wallet', [
    ['total_debt', 'total_debt REAL NOT NULL DEFAULT 0'],
    ['total_withdrawn', 'total_withdrawn REAL NOT NULL DEFAULT 0'],
  ]);
  addColumns('wallet_transactions', [['debt_after', 'debt_after REAL NOT NULL DEFAULT 0']]);

  // withdrawal_requests, if the deployment predates withdrawals.
  db.exec(`
    CREATE TABLE IF NOT EXISTS withdrawal_requests (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      technician_id INTEGER NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
      amount        REAL NOT NULL,
      status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'paid', 'rejected')),
      requested_at  TEXT DEFAULT (datetime('now')),
      processed_at  TEXT,
      processed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
      note          TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_withdrawal_status ON withdrawal_requests(status);
    CREATE INDEX IF NOT EXISTS idx_withdrawal_tech   ON withdrawal_requests(technician_id);
  `);

  // Backfill. Two passes, because a row can need either or both:
  //   - a negative balance becomes debt, and the balance is zeroed;
  //   - a NULL/0 debt is normalised to 0 so the API never returns null for a
  //     figure the UI is expected to compare against a limit.
  //
  // `total_owed` is deliberately NOT written here. It is the lifetime total of
  // commissions owed, so it does not equal the current debt: a technician who owes
  // 90 now and has since paid some off still has a total_owed well above 90. Copying
  // the migrated figure into it would silently rewrite history and shrink the
  // lifetime stat. Only the balance and the debt pot move.
  const negative = db
    .prepare('SELECT technician_id, balance FROM technician_wallet WHERE balance < 0')
    .all();
  negative.forEach((row) => {
    db.prepare(
      'UPDATE technician_wallet SET balance = 0, total_debt = ? WHERE technician_id = ?'
    ).run(Math.abs(row.balance), row.technician_id);
    logger.info(
      'db',
      `[db] wallet #` + row.technician_id + ': ' + row.balance + ' moved to total_debt'
    );
  });
  db.prepare(
    'UPDATE technician_wallet SET total_debt = 0 WHERE total_debt IS NULL OR total_debt < 0'
  ).run();
  db.prepare(
    'UPDATE technician_wallet SET total_withdrawn = 0 WHERE total_withdrawn IS NULL OR total_withdrawn < 0'
  ).run();
  db.prepare('UPDATE technician_wallet SET balance = 0 WHERE balance IS NULL OR balance < 0').run();

  // Settings for the withdrawal flow. INSERT OR IGNORE never overwrites a value an
  // admin has since edited.
  const ensure = db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)');
  ensure.run('min_withdrawal', '50');
  ensure.run('withdrawal_eta_hours', '24');
}

// Adds the two-sided cash confirmation columns to requests, plus the index the
// dispute queue and the auto-resolve sweep both scan on.
//
// Idempotent for the same reason ensureQueueColumns() is: every column is appended
// only when PRAGMA says it is missing, so this is a no-op on a database created from
// schema.sql and on every boot after the first.
//
// The two time columns hold SQLite's own datetime('now') text, which is what lets
// the sweep compare them in SQL instead of parsing them in JS.
function ensureCashConfirmationSchema() {
  if (!tableDdl('requests')) return;
  const existing = new Set(
    db
      .prepare('PRAGMA table_info(requests)')
      .all()
      .map((c) => c.name)
  );
  const columns = [
    ['customer_reported_amount', 'REAL'],
    ['technician_reported_amount', 'REAL'],
    ['final_amount', 'REAL'],
    ['amount_status', 'TEXT'],
    ['customer_reported_at', 'TEXT'],
    ['technician_reported_at', 'TEXT'],
    ['dispute_resolved_at', 'TEXT'],
    // No REFERENCES clause: SQLite cannot add a foreign key to an existing column,
    // so on a migrated database this one is unenforced. schema.sql declares it, and
    // a fresh database gets the real constraint.
    ['dispute_resolved_by', 'INTEGER'],
    ['dispute_note', 'TEXT'],
  ];
  columns.forEach(([name, type]) => {
    if (existing.has(name)) return;
    db.exec('ALTER TABLE requests ADD COLUMN ' + name + ' ' + type);
    logger.info('db', `[db] Added requests.` + name);
  });

  db.exec(
    'CREATE INDEX IF NOT EXISTS idx_requests_amount_status ON requests(amount_status, technician_reported_at)'
  );

  // The seeded value is the fallback the service reads when the env var is unset;
  // it is also what an admin sees. INSERT OR IGNORE never overwrites an edit.
  db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)').run(
    'cash_confirm_timeout_hours',
    String(config.CASH_CONFIRM_TIMEOUT_HOURS)
  );
}

// Makes sure every specialty has a service_pricing row.
//
// Two things depend on the row existing. pricing.getServicePrice() reads it when
// a request is created — with the table empty every request would be stamped at
// 0 and the commission would vanish. And the admin tariff editor PUTs to an
// existing key, so a specialty created at runtime could never be priced until
// the next restart.
//
// The default comes from config.SPECIALTIES, falling back to 0 for a specialty an
// admin added by hand. INSERT OR IGNORE is what makes this safe to run on every
// boot: a price an admin has since edited is never overwritten.
function ensureServicePricing() {
  const insert = db.prepare(
    'INSERT OR IGNORE INTO service_pricing (service_key, price, is_active) VALUES (?, ?, 1)'
  );
  const tx = db.transaction(() => {
    db.prepare('SELECT key FROM specialties ORDER BY sort_order ASC, id ASC')
      .all()
      .forEach((s) => {
        const configured = config.SPECIALTIES.find((x) => x.key === s.key);
        insert.run(s.key, configured && configured.price ? configured.price : 0);
      });
  });
  tx();
}

// ---------- Phone as a primary identifier ----------
// users.phone is the login identity. Two properties make this non-trivial:
//
//  1. It must be NORMALISED on the way in. Copying the raw customers/technicians
//     value ('01025330520') would create a row that phone login can never match,
//     because login normalises its input to '+201025330520' first.
//  2. It must be UNIQUE across BOTH profiles. The same number can legitimately
//     exist in customers.phone and technicians.phone at once (one person testing
//     with two accounts), which would make the UNIQUE index creation fail and
//     silently disable the constraint for the whole table.
//
// So the backfill walks the profiles in a fixed order and grants each normalised
// number to the FIRST owner only, leaving the loser NULL rather than aborting.
// A NULL phone means "cannot log in by phone", not "account broken" — the email
// path is untouched, so the skipped account stays fully reachable.
function migratePhoneIdentity() {
  const cols = db
    .prepare('PRAGMA table_info(users)')
    .all()
    .map((c) => c.name);
  if (!cols.includes('phone')) {
    // Added without a table-level UNIQUE so the constraint can be created AFTER
    // the backfill: SQLite cannot add a UNIQUE column to a table that already
    // holds the duplicates this migration is about to resolve.
    db.prepare('ALTER TABLE users ADD COLUMN phone TEXT').run();
    logger.info('db', `[db] users.phone column added.`);
  }

  const taken = new Set(
    db
      .prepare("SELECT phone FROM users WHERE phone IS NOT NULL AND phone != ''")
      .all()
      .map((r) => r.phone)
  );

  let filled = 0;
  const skipped = [];
  const claim = db.prepare('UPDATE users SET phone = ? WHERE id = ? AND phone IS NULL');
  const currentPhone = db.prepare('SELECT phone FROM users WHERE id = ?');

  const backfill = db.transaction(() => {
    ['customers', 'technicians'].forEach((table) => {
      db.prepare(`SELECT user_id, phone FROM ${table} WHERE phone IS NOT NULL AND phone != ''`)
        .all()
        .forEach((row) => {
          const norm = normalizePhone(row.phone);
          if (!norm) return;
          // Already assigned (this user legitimately owns the number, or the
          // account is an admin with no phone). Checking this BEFORE the
          // `taken` lookup is what keeps a second boot from reporting every
          // successfully migrated account as a conflict. The `.phone` test
          // matters: the lookup returns a row for admins too, and a NULL row
          // means "still needs a number", not "already has one".
          const existing = currentPhone.get(row.user_id);
          if (existing && existing.phone) return;
          if (taken.has(norm)) {
            skipped.push({ userId: row.user_id, table, phone: norm });
            return;
          }
          const res = claim.run(norm, row.user_id);
          if (res.changes > 0) {
            taken.add(norm);
            filled++;
          }
        });
    });
  });
  backfill();

  // Partial index: many admins legitimately have NULL, and SQLite treats NULLs
  // as distinct in a plain UNIQUE index anyway — the WHERE clause keeps the
  // index small and its intent explicit.
  db.prepare(
    `CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique
       ON users(phone) WHERE phone IS NOT NULL AND phone != ''`
  ).run();

  if (filled) logger.info('db', `[db] Backfilled users.phone for ` + filled + ' account(s).');
  if (skipped.length) {
    // Only genuinely unresolved conflicts reach here — these accounts kept a
    // NULL phone on purpose, so the UNIQUE index can still be created.
    logger.info(
      'db',
      `[db] ` +
        skipped.length +
        ' account(s) share a phone with an earlier account and were left without one: ' +
        skipped.map((s) => 'user ' + s.userId + ' (' + s.phone + ')').join(', ') +
        '. They can still sign in by email; assign a distinct number to enable phone login.'
    );
  }

  // Bring customers.phone / technicians.phone onto the same canonical spelling.
  // These tables have no UNIQUE constraint, so normalising here is purely about
  // display consistency: otherwise the admin list would print a legacy
  // '01012345678' next to a login identity of '+201012345678' for the same
  // person. Runs for skipped accounts too — they keep a readable number even
  // though they cannot authenticate with it.
  let normalizedProfiles = 0;
  ['customers', 'technicians'].forEach((table) => {
    db.prepare(`SELECT id, phone FROM ${table} WHERE phone IS NOT NULL AND phone != ''`)
      .all()
      .forEach((row) => {
        const norm = normalizePhone(row.phone);
        if (!norm || norm === row.phone) return;
        db.prepare(`UPDATE ${table} SET phone = ? WHERE id = ?`).run(norm, row.id);
        normalizedProfiles++;
      });
  });
  if (normalizedProfiles) {
    logger.info('db', `[db] Normalised ` + normalizedProfiles + ' profile phone value(s).');
  }
}
migrate();

module.exports = db;
