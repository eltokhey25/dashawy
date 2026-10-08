// One-off data cleanup: requests settled before the two-sided cash confirmation
// existed still carry amount_status='pending', so they sit in the admin's
// "مدفوعات معلقة" queue looking like open work.
//
// A request qualifies only if ALL of these hold:
//   payment_status = 'confirmed'   it was already settled
//   amount_status  = 'pending'    but still claims to be awaiting confirmation
//   final_amount IS NULL          nothing was ever agreed
//   it has a booked ledger entry   the money actually moved
//
// The last condition is what makes this safe: it is the same "is it booked" test
// settle() uses, so a row without a ledger entry is genuinely pending and is left
// for an admin to decide. Marking one of those 'resolved' would settle an
// unconfirmed payment on the technician's word alone.
//
// Two deviations from the script in the task description, both because the
// original could write a false record:
//   1. EXISTS is restricted to type IN ('debt','payment'). The original matched ANY
//      ledger row, so a request whose only entry was a payout or a withdrawal would
//      have been flipped to 'resolved' with a meaningless final_amount.
//   2. The final_amount fallback ends at NULL, not 0. Writing 0 would assert that
//      the agreed cash amount was zero, which is a different statement from "not
//      known", and the admin UI would render it as a real figure.
const db = require('./backend/db');

const BOOKED = "('debt', 'payment')";

const stale = db
  .prepare(
    `SELECT r.id, r.amount_status, r.final_amount, r.technician_reported_amount,
            r.customer_reported_amount, r.payment_status
       FROM requests r
      WHERE r.payment_status = 'confirmed'
        AND r.amount_status = 'pending'
        AND r.final_amount IS NULL
        AND EXISTS (
          SELECT 1 FROM wallet_transactions wt
           WHERE wt.request_id = r.id AND wt.type IN ${BOOKED}
        )
      ORDER BY r.id`
  )
  .all();

console.log('Found', stale.length, 'stale rows to fix');
stale.forEach((r) => console.log('  #' + r.id, JSON.stringify(r)));

// The gross the customer paid, best evidence first:
//   1. what the technician reported (the figure the commission was taken from)
//   2. the commission on the ledger, grossed back up by the configured rate
// A credit ('payment') row is deliberately NOT used as a source: it records the
// technician's NET earning, not the gross, so treating it as final_amount would
// understate the amount the customer paid.
const percent = require('./backend/config').getCommissionPercent();
const rate = percent > 0 ? percent / 100 : 0.2;

const run = db.transaction(() => {
  stale.forEach((r) => {
    const debt = db
      .prepare(
        "SELECT amount AS a FROM wallet_transactions WHERE request_id = ? AND type = 'debt' ORDER BY id LIMIT 1"
      )
      .get(r.id);

    let source = 'technician_reported_amount';
    let finalAmount = r.technician_reported_amount != null ? Number(r.technician_reported_amount) : null;

    if (finalAmount == null && debt && debt.a) {
      source = 'commission grossed up from the ledger';
      finalAmount = Math.round((Math.abs(Number(debt.a)) / rate) * 100) / 100;
    }

    db.prepare(
      `UPDATE requests
          SET amount_status = 'resolved',
              final_amount = ?,
              updated_at = datetime('now')
        WHERE id = ?`
    ).run(finalAmount, r.id);

    console.log(
      'Fixed request #' + r.id,
      '-> resolved,',
      'final_amount =',
      finalAmount === null ? 'NULL (unknown, left unset)' : finalAmount,
      '(' + source + ')'
    );
  });
});
run();

const remaining = db
  .prepare(
    `SELECT COUNT(*) AS n FROM requests
      WHERE payment_status = 'confirmed'
        AND amount_status = 'pending'
        AND EXISTS (
          SELECT 1 FROM wallet_transactions wt
           WHERE wt.request_id = requests.id AND wt.type IN ${BOOKED}
        )`
  )
  .get().n;

console.log('Remaining stale rows:', remaining);
console.log('Re-running the migration is a no-op: the predicate no longer matches any row.');
