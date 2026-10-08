// Wallet Phase 1 migration, run against the real database.
//
// Prints before/after for every affected table so the change is auditable, and
// verifies rather than assumes: row counts must not move, total_owed (a lifetime
// counter) must be untouched, and the file must pass integrity/FK checks.
const path = require('path');

const REAL = path.join(__dirname, 'database', 'fixrequest.db');
process.env.DATABASE_PATH = REAL;

const Database = require(path.join(__dirname, 'node_modules', 'better-sqlite3'));

// --- inspect the file as it is now, before the app opens it ---------------
const before = new Database(REAL, { readonly: true });
const colsBefore = (t) =>
  before
    .prepare('PRAGMA table_info(' + t + ')')
    .all()
    .map((c) => c.name);
const walletBefore = before.prepare('SELECT * FROM technician_wallet').all();
const countsBefore = {
  wallet: before.prepare('SELECT COUNT(*) c FROM technician_wallet').get().c,
  ledger: before.prepare('SELECT COUNT(*) c FROM wallet_transactions').get().c,
  users: before.prepare('SELECT COUNT(*) c FROM users').get().c,
  requests: before.prepare('SELECT COUNT(*) c FROM requests').get().c,
};
const hasOn = (conn, t) => !!conn.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(t);
const withdrawalExistedBefore = hasOn(before, 'withdrawal_requests');
const ledgerColsBefore = colsBefore('wallet_transactions');
const walletColsBefore = colsBefore('technician_wallet');

console.log('=== BEFORE ===');
console.log('wallet columns:        ' + walletColsBefore.join(', '));
console.log('ledger columns:        ' + ledgerColsBefore.join(', '));
console.log('withdrawal_requests:   ' + (withdrawalExistedBefore ? 'exists' : 'MISSING'));
console.log('row counts:            ' + JSON.stringify(countsBefore));
console.log('wallets:');
walletBefore.forEach((w) => console.log('  ' + JSON.stringify(w)));
before.close();

// --- run the migration (requiring db.js performs it) ----------------------
console.log('\n=== MIGRATION ===');
const db = require(path.join(__dirname, 'backend', 'db'));
console.log('migration complete');

// --- verify ----------------------------------------------------------------
const walletAfter = db.prepare('SELECT * FROM technician_wallet').all();
const colsAfter = {
  wallet: colsAfterSafe('technician_wallet'),
  ledger: colsAfterSafe('wallet_transactions'),
};
function colsAfterSafe(t) {
  return db
    .prepare('PRAGMA table_info(' + t + ')')
    .all()
    .map((c) => c.name);
}

console.log('\n=== AFTER ===');
console.log('wallet columns:        ' + colsAfter.wallet.join(', '));
console.log('ledger columns:        ' + colsAfter.ledger.join(', '));
console.log('withdrawal_requests:   ' + (hasOn(db, 'withdrawal_requests') ? (withdrawalExistedBefore ? 'already present' : 'created') : 'MISSING'));
console.log('indexes:               ' + db
  .prepare("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='withdrawal_requests'")
  .all()
  .map((r) => r.name)
  .join(', '));
const settings = db
  .prepare("SELECT key, value FROM settings WHERE key IN ('min_withdrawal','withdrawal_eta_hours','wallet_debt_limit')")
  .all();
console.log('withdrawal settings:   ' + JSON.stringify(settings));
console.log('wallets:');
walletAfter.forEach((w) => console.log('  ' + JSON.stringify(w)));

console.log('\n=== CHECKS ===');
const countsAfter = {
  wallet: db.prepare('SELECT COUNT(*) c FROM technician_wallet').get().c,
  ledger: db.prepare('SELECT COUNT(*) c FROM wallet_transactions').get().c,
  users: db.prepare('SELECT COUNT(*) c FROM users').get().c,
  requests: db.prepare('SELECT COUNT(*) c FROM requests').get().c,
};
console.log('row counts preserved:   ' + (JSON.stringify(countsBefore) === JSON.stringify(countsAfter)));
console.log('  before ' + JSON.stringify(countsBefore));
console.log('  after  ' + JSON.stringify(countsAfter));

colsAfter.wallet.forEach((c) => {
  if (!walletColsBefore.includes(c)) console.log('  + technician_wallet.' + c);
});
colsAfter.ledger.forEach((c) => {
  if (!ledgerColsBefore.includes(c)) console.log('  + wallet_transactions.' + c);
});
if (!withdrawalExistedBefore) console.log('  + withdrawal_requests (table + 2 indexes)');

const totalOwedBefore = walletBefore.reduce((s, w) => s + Number(w.total_owed || 0), 0);
const totalOwedAfter = walletAfter.reduce((s, w) => s + Number(w.total_owed || 0), 0);
console.log('total_owed untouched:   ' + (totalOwedBefore === totalOwedAfter) + '  (' + totalOwedBefore + ' -> ' + totalOwedAfter + ')');
console.log('no negative balances:   ' + walletAfter.every((w) => Number(w.balance) >= 0));
console.log('no negative debt:       ' + walletAfter.every((w) => Number(w.total_debt) >= 0));
console.log('integrity_check:        ' + db.pragma('integrity_check').map((r) => r.integrity_check).join(','));
console.log('foreign_key_check:      ' + JSON.stringify(db.pragma('foreign_key_check')));
db.close();
