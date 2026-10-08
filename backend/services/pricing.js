/**
 * @file   Pricing and wallet service
 * @module pricing
 *
 * The single source of truth for money in this system: reads the per-service
 * tariff from the database, splits a price into platform commission and
 * technician earning, and maintains the technician wallet.
 *
 * THE WALLET MODEL. A technician's money lives in two separate, non-negative pots:
 *
 *   balance     credit they can withdraw — earnings the customer paid to the
 *               PLATFORM (InstaPay), credited in full to the technician.
 *   total_debt  what they currently owe the platform — commission on work the
 *               customer paid in CASH, which the technician collected.
 *
 * They used to be one signed `balance`, which made "has money" and "owes money"
 * indistinguishable: a positive number could mean either a payout owed to them or a
 * cleared debt, and every reader had to guess. The debt limit is now checked
 * against total_debt alone, so a technician with a large balance is never blocked.
 * total_owed is a third, unrelated figure: the lifetime sum of commissions ever
 * charged, which only grows.
 *
 * broadcast.js and customer.routes.js both depend on this module, so the
 * exported function signatures are a contract — keep them stable.
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Pricing + wallet eligibility. Prices come from the service_pricing table so an
// admin can change the tariff without a redeploy.
const db = require('../db');
const config = require('../config');

// Money is stored as REAL, so round at every boundary to keep float noise
// (e.g. 150 * 0.2 = 30.000000000000004) out of the ledger. Adding a small
// epsilon before rounding makes .5 cases round up rather than down.
function round2(n) {
  return Math.round((Number(n) + Number.EPSILON) * 100) / 100;
}

// Base price for one service, in EGP. An unknown or inactive service prices at
// 0 rather than throwing: a request must still be creatable even when the
// tariff has not been written for it yet, and the admin can price it afterwards.
function getServicePrice(serviceKey) {
  const row = db
    .prepare('SELECT price FROM service_pricing WHERE service_key = ? AND is_active = 1')
    .get(serviceKey);
  return row ? Number(row.price) : 0;
}

// Split a gross price into the platform's cut and the technician's net.
// commission = price * percent / 100 ; earning = price - commission.
function calculatePricing(price) {
  const percent = config.getCommissionPercent();
  const commission = round2((price * percent) / 100);
  const earning = round2(price - commission);
  return { percent, commission, earning };
}

// Stamps the money columns on a request. Idempotent and cheap, so it can safely
// re-run on an already-priced request (broadcast.js calls it again on accept in
// case the tariff changed since the request was made). Returns the computed
// amounts, or null if the request no longer exists.
function applyPricingToRequest(requestId) {
  const req = db.prepare('SELECT specialty FROM requests WHERE id = ?').get(requestId);
  if (!req) return null;

  const price = getServicePrice(req.specialty);
  const { percent, commission, earning } = calculatePricing(price);

  db.prepare(
    `UPDATE requests
        SET service_price = ?, commission_percent = ?, commission_amount = ?,
            technician_earning = ?, updated_at = datetime('now')
      WHERE id = ?`
  ).run(price, percent, commission, earning, requestId);

  return { price, percent, commission, earning };
}

// Fetches a technician's wallet, creating a zeroed one on first use. Lazy
// creation keeps the table in step with the technician roster without a
// migration step or a backfill job.
//
// The returned row is normalised in place: a wallet created before the two-pot
// migration has NULL in the new columns, and a caller comparing NULL against the
// debt limit would get a silently wrong answer.
function ensureWallet(technicianId) {
  let wallet = db
    .prepare('SELECT * FROM technician_wallet WHERE technician_id = ?')
    .get(technicianId);

  if (!wallet) {
    db.prepare(
      `INSERT INTO technician_wallet
         (technician_id, balance, total_debt, total_earned, total_owed, total_paid_to_platform, total_withdrawn)
       VALUES (?, 0, 0, 0, 0, 0, 0)`
    ).run(technicianId);
    wallet = db
      .prepare('SELECT * FROM technician_wallet WHERE technician_id = ?')
      .get(technicianId);
    return wallet;
  }

  const debt = Number(wallet.total_debt || 0);
  const withdrawn = Number(wallet.total_withdrawn || 0);
  const balance = Number(wallet.balance || 0);
  if (
    debt !== wallet.total_debt ||
    withdrawn !== wallet.total_withdrawn ||
    balance !== wallet.balance
  ) {
    db.prepare(
      `UPDATE technician_wallet SET balance = ?, total_debt = ?, total_withdrawn = ? WHERE technician_id = ?`
    ).run(balance, debt, withdrawn, technicianId);
    wallet = Object.assign({}, wallet, {
      balance: balance,
      total_debt: debt,
      total_withdrawn: withdrawn,
    });
  }
  return wallet;
}

// Appends one ledger entry and moves the wallet by `amount`.
//
// The two pots move independently, so `amount` is interpreted per type rather than
// as a signed delta on a single number:
//
//   type          amount sign   effect
//   ---------     ----------   -----------------------------------------------
//   'payment'     positive      balance += amount, total_earned += amount
//                               (customer paid the PLATFORM by InstaPay)
//   'debt'        any sign      total_debt += |amount|, total_owed += |amount|
//                               (customer paid the TECHNICIAN in cash)
//   'payout'      positive      total_debt -= |amount| (floored at 0),
//                               total_paid_to_platform += |amount|
//                               (the technician handed cash over)
//   'withdrawal'  positive      balance -= |amount| (floored at 0),
//                               total_withdrawn += |amount|
//   'adjustment'  —             no automatic movement; the caller supplies it
//
// Sign is taken from the ENTRY type, not trusted from the caller, so a negative
// amount on a 'payment' cannot shrink a technician's earnings. The payout and
// withdrawal floors mean an over-payment cannot manufacture debt or credit: the
// money stops at zero and the ledger still records the full amount, so an admin can
// see what was collected even when only part of it had a pot to come out of.
//
// The INSERT and the UPDATE run in one transaction: a half-written ledger entry
// with no matching balance would desynchronise the two permanently.
function addWalletTransaction(technicianId, { requestId, type, amount, note }) {
  const wallet = ensureWallet(technicianId);

  const magnitude = Math.abs(Number(amount) || 0);
  let newBalance = round2(wallet.balance);
  let newDebt = round2(wallet.total_debt);
  let newEarned = round2(wallet.total_earned);
  let newOwed = round2(wallet.total_owed);
  let newPaid = round2(wallet.total_paid_to_platform);
  let newWithdrawn = round2(wallet.total_withdrawn);

  if (type === 'payment') {
    newBalance = round2(newBalance + magnitude);
    newEarned = round2(newEarned + magnitude);
  } else if (type === 'debt') {
    newDebt = round2(newDebt + magnitude);
    newOwed = round2(newOwed + magnitude);
  } else if (type === 'payout') {
    newDebt = Math.max(0, round2(newDebt - magnitude));
    newPaid = round2(newPaid + magnitude);
  } else if (type === 'withdrawal') {
    newBalance = Math.max(0, round2(newBalance - magnitude));
    newWithdrawn = round2(newWithdrawn + magnitude);
  }

  const run = db.transaction(() => {
    db.prepare(
      `INSERT INTO wallet_transactions
         (technician_id, request_id, type, amount, balance_after, debt_after, note)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(technicianId, requestId || null, type, magnitude, newBalance, newDebt, note || null);

    db.prepare(
      `UPDATE technician_wallet
          SET balance = ?, total_debt = ?, total_earned = ?, total_owed = ?,
              total_paid_to_platform = ?, total_withdrawn = ?, last_updated = datetime('now')
        WHERE technician_id = ?`
    ).run(newBalance, newDebt, newEarned, newOwed, newPaid, newWithdrawn, technicianId);
  });
  run();

  return { balance: newBalance, total_debt: newDebt };
}

// Whether this technician may be offered new work.
//
// Two gates, in order:
//  1. Approval. An unapproved technician is never broadcast to, regardless of
//     how healthy their wallet looks.
//  2. Debt ceiling. A NEGATIVE balance is money the technician owes the
//     platform (commission collected in cash but not yet handed over). Once that
//     debt reaches the limit they are excluded from broadcasts until they pay
//     down or an admin records a payout. The comparison is >=, so sitting
//     exactly at the limit blocks — otherwise a technician could stop one piastre
//     short and keep taking work indefinitely.
//
// broadcast.js already treats a false result as "exclude from the broadcast",
// so nothing else needs to change when the limit changes.
function canReceiveRequests(technicianId) {
  const tech = db.prepare('SELECT approval_status FROM technicians WHERE id = ?').get(technicianId);
  if (!tech || tech.approval_status !== 'approved') return false;

  const wallet = ensureWallet(technicianId);
  const limitRow = db.prepare('SELECT value FROM settings WHERE key = ?').get('wallet_debt_limit');
  const limit = parseFloat(limitRow && limitRow.value) || 300;

  // Only total_debt matters. balance is money the platform owes the technician and
  // is deliberately NOT consulted: earning an InstaPay payment must never look like
  // settling a debt. The comparison is >= so sitting exactly at the limit blocks —
  // otherwise a technician could stop one piastre short and keep taking work.
  if (round2(wallet.total_debt) >= limit) {
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Withdrawals
// ---------------------------------------------------------------------------

// A withdrawal moves credit OUT of balance and into the technician's pocket. The
// balance is debited when an admin marks the request 'paid' — i.e. when the money
// has actually left — not when it is requested or approved, so a technician keeps
// earning (and can keep the money visible) while a transfer is in flight.
function withdrawalSettings() {
  const rows = db
    .prepare(
      "SELECT key, value FROM settings WHERE key IN ('min_withdrawal', 'withdrawal_eta_hours')"
    )
    .all();
  const out = {};
  rows.forEach((r) => {
    out[r.key] = r.value;
  });
  const min = parseFloat(out.min_withdrawal);
  const eta = parseFloat(out.withdrawal_eta_hours);
  return {
    min: Number.isFinite(min) && min > 0 ? min : 50,
    etaHours: Number.isFinite(eta) && eta > 0 ? eta : 24,
  };
}

// Typed failure so the route can map a code to a status without matching on the
// message text, which would break the moment an Arabic string is reworded.
function walletError(code, message) {
  const err = new Error(message);
  err.code = code;
  return err;
}

// A technician asks to withdraw money they have earned.
//
// Guarded three ways: the amount must clear the configured minimum, the balance
// must actually cover it, and only one request may be in flight at a time —
// otherwise a technician could queue several requests each individually within
// their balance and be paid all of them.
function requestWithdrawal(technicianId, amount) {
  const amt = round2(amount);
  if (!Number.isFinite(amt) || amt <= 0) {
    throw walletError('INVALID_AMOUNT', 'مبلغ غير صالح');
  }

  const { min } = withdrawalSettings();
  if (amt < min) {
    throw walletError('MIN_WITHDRAWAL', 'الحد الأدنى للسحب ' + min + ' ج.م');
  }

  const wallet = ensureWallet(technicianId);
  if (round2(wallet.balance) < amt) {
    throw walletError('INSUFFICIENT_BALANCE', 'رصيدك غير كافٍ للسحب');
  }

  const inFlight = db
    .prepare(
      "SELECT id FROM withdrawal_requests WHERE technician_id = ? AND status IN ('pending', 'approved')"
    )
    .get(technicianId);
  if (inFlight) {
    throw walletError('PENDING_EXISTS', 'لديك طلب سحب قيد المراجعة بالفعل');
  }

  const result = db
    .prepare(
      "INSERT INTO withdrawal_requests (technician_id, amount, status) VALUES (?, ?, 'pending')"
    )
    .run(technicianId, amt);

  return { id: result.lastInsertRowid, amount: amt, status: 'pending' };
}

function getWithdrawal(requestId) {
  const req = db.prepare('SELECT * FROM withdrawal_requests WHERE id = ?').get(requestId);
  if (!req) throw walletError('NOT_FOUND', 'طلب السحب غير موجود');
  return req;
}

// The admin has decided to transfer the money. Nothing moves in the wallet yet.
function approveWithdrawal(requestId, adminUserId) {
  const req = getWithdrawal(requestId);
  if (req.status !== 'pending') {
    throw walletError('INVALID_STATUS', 'الطلب ليس قيد المراجعة');
  }
  db.prepare(
    `UPDATE withdrawal_requests
        SET status = 'approved', processed_at = datetime('now'), processed_by = ?
      WHERE id = ? AND status = 'pending'`
  ).run(adminUserId, requestId);
  return Object.assign({}, req, { status: 'approved' });
}

// The money has left the platform: debited from the technician's balance.
//
// The balance is re-checked here rather than trusted from request time. A request
// can sit approved for a while, and anything that reduces the balance in between
// (a future adjustment path, or a manual correction) would otherwise let the
// platform pay out credit that is not there.
function markWithdrawalPaid(requestId, adminUserId, note) {
  const req = getWithdrawal(requestId);
  if (req.status === 'paid') {
    throw walletError('ALREADY_PAID', 'تم دفع هذا الطلب بالفعل');
  }
  if (req.status !== 'approved' && req.status !== 'pending') {
    throw walletError('INVALID_STATUS', 'لا يمكن دفع طلب مرفوض');
  }

  const amount = round2(req.amount);
  const run = db.transaction(() => {
    const wallet = ensureWallet(req.technician_id);
    if (round2(wallet.balance) < amount) {
      throw walletError('INSUFFICIENT_BALANCE', 'رصيد الفني غير كافٍ لتنفيذ التحويل');
    }
    addWalletTransaction(req.technician_id, {
      type: 'withdrawal',
      amount: amount,
      note: note || 'سحب معتمد #' + requestId,
    });

    // Guarded on the status so a concurrent second 'paid' call cannot double-debit
    // the balance: only one of them sees a row still in 'approved'.
    const updated = db
      .prepare(
        `UPDATE withdrawal_requests
            SET status = 'paid', processed_at = datetime('now'), processed_by = ?, note = ?
          WHERE id = ? AND status IN ('pending', 'approved')`
      )
      .run(adminUserId, note || null, requestId);
    if (updated.changes === 0) {
      throw walletError('ALREADY_PAID', 'تم دفع هذا الطلب بالفعل');
    }
  });
  run();

  return Object.assign({}, req, { status: 'paid' });
}

function rejectWithdrawal(requestId, adminUserId, note) {
  const req = getWithdrawal(requestId);
  if (req.status !== 'pending') {
    throw walletError('INVALID_STATUS', 'لا يمكن رفض هذا الطلب');
  }
  db.prepare(
    `UPDATE withdrawal_requests
        SET status = 'rejected', processed_at = datetime('now'), processed_by = ?, note = ?
      WHERE id = ? AND status = 'pending'`
  ).run(adminUserId, note || null, requestId);
  return Object.assign({}, req, { status: 'rejected' });
}

// The technician's own request history, newest first.
function listWithdrawals(technicianId, limit) {
  return db
    .prepare(
      'SELECT * FROM withdrawal_requests WHERE technician_id = ? ORDER BY requested_at DESC, id DESC LIMIT ?'
    )
    .all(technicianId, limit || 30);
}

// The request the technician still has in flight, or null. Shown on their wallet so
// they do not queue a second one.
function pendingWithdrawal(technicianId) {
  const row = db
    .prepare(
      `SELECT id, amount, status, requested_at FROM withdrawal_requests
        WHERE technician_id = ? AND status IN ('pending', 'approved')
        ORDER BY requested_at DESC LIMIT 1`
    )
    .get(technicianId);
  return row || null;
}

module.exports = {
  getServicePrice,
  calculatePricing,
  applyPricingToRequest,
  ensureWallet,
  addWalletTransaction,
  canReceiveRequests,
  requestWithdrawal,
  approveWithdrawal,
  markWithdrawalPaid,
  rejectWithdrawal,
  listWithdrawals,
  pendingWithdrawal,
  withdrawalSettings,
};
