# Phase 4: Enrollment assessment, balance, payments and verification

Branch `phase4-finance`, based on Phase 3 (`ee89f71`).

## What changed for users

| Who | What they get |
|---|---|
| Registrar | Finalizing a registration form now creates the student's **term assessment** from Accounting's existing fee rules. The form page and the enrollment review page show a read-only **Assessment & payments** panel: total assessment, previous balance, discounts, payments, remaining balance, payment status, verification status, latest payment and O.R. number, and whether the student meets the enrollment payment policy. The registrar has no buttons that change money. |
| Cashier | The payment page shows the term balance and a full payment history with a status for each row. Payments now take an **official receipt number**, which must be unique. Double clicks and resubmits record **one** payment. **Undo no longer deletes**: it reverses, with a required reason. A cashier may reverse only their own unverified payment, and only on the day it was recorded. |
| Accounting | New page **Enrollment Assessments** (sidebar): per-term list with balances and statuses, filters (Unpaid / Partially Paid / Fully Paid / No Fees Assessed / For Verification / Payment Verified), **Verify** for payments (never your own), **Reverse** for any payment (reason required), **Re-apply fees** to an assessment, and the **enrollment payment policy** setting. |
| Admin | Same list and policy setting, without verify or regenerate. |
| Student | No change in this phase (see "Not done"). |

## Rules implemented

**Assessment generation** (`finance.generate_for_form`, called from `registration.finalize`)
- Fees come only from Accounting's existing fee rules (`batch_assessments_log`). A rule applies when its semester, program (title) and year level match the form, or are blank/"All". Its scope must match the student's residency (`garciano` / `non_garciano`, using the same Padre Garcia address rule as the existing batch screen). **No amounts are invented.** If no rule matches, the assessment is ₱0 and the registrar sees a warning.
- If the existing automatic fee step already charged the student for this enrollment, that charge is **adopted** and not charged again. "This enrollment" means the same rule, created after the term was set up and after the student's previous assessment, and not yet linked to a term.
- Re-finalizing, reopening, or **Re-apply fees** never charges twice. They only add rules that are new since the last run, and they list charges whose rule no longer matches; nothing is deleted.
- One assessment per student per term (DB unique key). One charge row can belong to only one assessment (DB unique key).

**Balance** (`finance.summary`, all `Decimal`, rounded half-up to centavos)
```
remaining = total assessment + previous balance − discounts − payments
```
- *Total assessment*: the charges linked to this term's assessment.
- *Previous balance*: every other charge, minus what was paid or credited on it. Charges of **later** terms are excluded when you look at an older term. A negative previous balance is shown as a credit.
- *Discounts*: `Discount` and `Voucher` rows on this term's charges.
- *Payments*: all other payment rows on this term's charges. Reversal rows are negative, so a reversed payment nets to zero.
- Payment status is one of Unassessed, Unpaid, Partially Paid, Fully Paid or No Fees Assessed. Verification status is For Verification (any unverified payment) or Payment Verified.

**Payments** (`finance.record_payment`)
- The amount must be over 0, at most the remaining balance of the selected charge, and finite (NaN and Infinity are rejected). `1,234.50` and `₱1,234.50` are accepted.
- The date must be valid and not in the future. The method must be Cash, GCash, Bank Transfer or Check.
- The O.R. number is optional but unique.
- A one-time submission token (stored in `payment_submissions`) makes a repeated POST return the first payment instead of creating a second one.
- `assessments.amount_paid`, `balance` and `status` are recomputed from the payment rows, so the existing screens and receipts keep working.

**Verification** (`finance.verify_payment`)
- Only the `accounting` role can verify, and never a payment they recorded themselves. Students, cashiers and registrars cannot reach it; the backend refuses even a crafted POST.
- Discounts, vouchers, reversal rows and reversed payments cannot be verified. A verification is never edited or deleted.

**Reversal** (`finance.reverse_payment`)
- A reversal never deletes or edits the original row. It adds a negative `Reversal` row and a `payment_reversals` link with the reason, who reversed it and when. Both rows stay in the history; a verification on the original stays recorded.
- Accounting may reverse any payment, verified or not. A cashier may reverse only their own unverified payment from the same day. A reversal can't be reversed, and a payment can't be reversed twice.

**Enrollment payment policy** (Accounting or Admin set it; it is stored in `system_settings`)
| Mode | Met when |
|---|---|
| Accounting clearance *(default, same as today)* | the Accounting item of `student_enrollment_records` is approved |
| Full payment | remaining balance ≤ 0 |
| Any payment | at least one payment toward the term |
| Minimum down payment | payments + discounts ≥ the configured amount (existing key `enrollment_approval_min_payment`) |

"Count only verified payments" is on by default. In this phase the policy result is **shown** to the registrar. It will **block** final check-in in Phase 5.

**Audit**: every generation, regeneration, payment, verification, reversal and policy change is written to `finance_audit_log` with the actor, role, time and a JSON detail.

## Database changes: migration `0005_enrollment_finance`

New tables only. **`assessments` and `payments` are not altered** and no row is deleted.

| Table | Purpose |
|---|---|
| `enrollment_assessments` | one per student and term, linked to the registration form |
| `enrollment_assessment_items` | links each charge (`assessments.id`) to that assessment |
| `payment_verifications` | who verified which payment, and when |
| `payment_reversals` | original payment, its reversal row, reason, who and when |
| `payment_submissions` | double-submit guard |
| `finance_audit_log` | audit trail |

All are InnoDB with foreign keys `ON DELETE RESTRICT` and the live table collation. `full.sql` is updated for fresh installs.

**Deploying:**
```
mysqldump --single-transaction --routines <db> > backup_before_0005.sql
python scripts/migrate.py status
python scripts/migrate.py apply --backup-done
```
**Rollback:** `python scripts/migrate.py rollback 0005_enrollment_finance --backup-done`. It refuses while assessments or reversals exist (that would erase financial history); export first and add `--force` only if you really mean it. Then restore the previous code. Note that once reversals exist, the old code would show a reversed payment and its negative row as two lines.

Tested on the local copy of `pgpc_db`: backup → apply → rollback → re-apply all succeeded. On the test DB, which contains assessments, the guard refused the rollback. `scripts/audit_data.py` stays clean.

## Files

New:
- `website/finance/__init__.py`, `service.py` (all rules above), `routes.py` (`/accounting/enrollment-finance`)
- `website/templates/finance/index.html`, `_balance_table.html`, `_payments_table.html`
- `tests/test_phase4_finance.py` (20 tests)
- `docs/PHASE4_REPORT.md`

Changed:
- `website/models.py`: 6 models plus `FINANCE_TABLES`
- `website/schema_migrations.py`: migration 0005
- `full.sql`
- `website/__init__.py`: registers the blueprint
- `website/registration/service.py`: `finalize` generates the assessment
- `website/registration/routes.py`, `templates/registration/form.html`: finance panel
- `website/auth.py`:
  - `update_payment` uses the finance service and passes the history and balance.
  - `undo_last_payment` reverses instead of deleting, with a reason.
  - The enrollment review shows the balance.
- `templates/update_payment.html`: O.R. field, token, balance card, history with Verify and Reverse, button disabled after submit
- `templates/admin_enrollment_review.html`: finance panel
- `accounting_base.html`, `cashier_base.html`: sidebar link
- `tests/test_route_crawl.py`: seeds an assessment, a verified payment and a reversed payment so every page renders with real finance data

## Tests

`pytest`: **117 passed, 1 xfailed** (the xfail is the pre-existing one documented in Phase 1). Phase 4 tests cover:
- Generation from matching rules only: residency scope and other year levels are excluded, and the ₱0 warning appears.
- Adoption of an existing charge, so nothing is charged twice. Regenerate is idempotent, and reopen + finalize does not double-charge.
- The balance formula with previous balance, a discount, payments and centavo amounts, with Decimal results. Fully Paid, and the legacy `assessments` columns resynced.
- Payment validations (over-payment, zero, invalid amount, NaN/Infinity, bad method, bad token, another student's charge). Duplicate token → one payment. O.R. uniqueness. The real form POSTed twice → one payment.
- Verification by role, not your own, not twice, not credits. A student cannot verify; a cashier cannot verify through the route.
- Reversal keeps both rows and restores the balance. Cashier limits (other cashier, verified, yesterday), reason required, no double reversal, a reversal can't be reversed or verified. The undo route refuses a payment of another student.
- All four policy modes, the verified-only switch, discounts counting toward the minimum, and policy route permissions plus the audit row.
- The accounting list for accounting, cashier and admin; status filters; the registrar is redirected away from it; the registrar panel is read-only.

Browser run (Chrome; desktop 1440 px and mobile 390 px):
1. The registrar finalizes, and the panel shows ₱6,950.50 from three sample rules.
2. Cashier Ana records ₱2,000 with O.R. OR-000123.
3. Accounting user Ben verifies it.
4. The list shows Partially Paid · Payment Verified, ₱4,950.50 remaining.

No horizontal overflow on mobile. The smoke script still has its one pre-existing failure (the test data has no `bscs` program); it is unrelated.

## Decisions made without your answer (change on request)
1. **Default policy = Accounting clearance**, which is how enrollment works today. Switch it on the new page.
2. **Verified payments only** count toward the policy by default.
3. **Discounts and vouchers count toward the minimum down payment**, since they reduce what the student owes. Say if they should not.
4. A cashier may correct only their own same-day, unverified payment; everything else goes to Accounting.
5. Charges without a term (older ledger rows) count as **previous balance**.

## Not done / known issues
- **Student portal** does not yet show the term balance (the service is ready; tell me if students should see it).
- Discounts and vouchers are still added and removed on their existing screens. The **voucher screen can over-credit and has no undo** (Phase 1 finding, unchanged).
- The legacy batch-assessment screen still creates charges directly. Phase 4 adopts them, but running it after a form is finalized creates a charge the assessment does not include until someone clicks **Re-apply fees**.
- The O.R. number is optional because some existing records have none. Tell me if it should be mandatory for Cash.
- Faculty class lists still miss irregular students sectioned in other blocks (Phase 3 note).

## Questions for Phase 5 (approval checklist and check-in)
1. Which checklist items are required, and which role approves each (Registrar documents, Accounting, Dean, President, OSA, Guidance…)? Should the existing President/Dean/Accounting flags on `student_enrollment_records` stay as they are?
2. If the payment policy is not met, should check-in be **blocked**, or may the registrar override it with a written reason?
3. Which payment policy will PGPC actually use, and if a minimum down payment, how much?
4. Should check-in set the student's status to *Enrolled* and lock the registration form, and who may undo a check-in?
