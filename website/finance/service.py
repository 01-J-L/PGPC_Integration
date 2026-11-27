"""Enrollment assessment, balances and payments - Phase 4.

Ledger (unchanged, source of truth):
  assessments  = charges  (total_amount; amount_paid/balance are kept in sync
                 from the payment rows by `resync_assessment`)
  payments     = money received AND credits: payment_method 'Discount' and
                 'Voucher' (organisation discounts / Garciano voucher) are
                 credits, 'Reversal' rows are contra entries of a reversed payment.

Balance for a term (all Decimal, never float):
  Remaining = Total Assessment + Previous Balance - Discounts - Payments
  * Total Assessment: charges linked to the term's registration form
  * Previous Balance: every other charge of the student (earlier terms, legacy
    rows) minus every credit/payment on them (negative = credit)
  * Discounts: Discount/Voucher credits on this term's charges
  * Payments: other payments on this term's charges, net of reversals

Fees come ONLY from the existing accounting fee rules (batch_assessments_log:
semester / program / year level / resident scope). Nothing is invented: if no
rule matches, the assessment is 0 and accounting is told to set up the fees.
"""

import json
import re
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from website import db
from website.models import (AcademicTerm, Applicant, EnrollmentAssessment, EnrollmentAssessmentItem, FinanceAuditLog,
                            PaymentReversal, PaymentSubmission, PaymentVerification, RegistrationForm, YEAR_LABELS)

CENT = Decimal('0.01')
CREDIT_METHODS = ('Discount', 'Voucher')
PAYMENT_METHODS = ('Cash', 'GCash', 'Bank Transfer', 'Check')
POLICY_MODES = {
    'clearance': 'Accounting clearance (the Accounting item of the approval checklist)',
    'full': 'Full payment of the term assessment',
    'partial': 'Any payment toward the term assessment',
    'minimum': 'Minimum down payment',
}


class FinanceError(Exception):
    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


def money(value):
    if value is None or value == '':
        return Decimal('0.00')
    text = value if not isinstance(value, str) else value.strip().replace(',', '').replace('\u20b1', '').replace('PHP', '').strip()
    try:
        d = Decimal(str(text))
        if not d.is_finite():
            raise InvalidOperation
        return d.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise FinanceError(f"'{value}' is not a valid amount.")


def peso(value):
    return f"₱{money(value):,.2f}"


def _audit(action, actor, applicant_id=None, payment_id=None, ea_id=None, details=None):
    db.session.add(FinanceAuditLog(action=action, applicant_id=applicant_id, payment_id=payment_id,
                                   enrollment_assessment_id=ea_id, performed_by=actor.name, performed_role=actor.role,
                                   details=json.dumps(details, default=str, ensure_ascii=False) if details is not None else None,
                                   performed_at=datetime.now()))


def _q(sql, **params):
    stmt = text(sql)
    for k, v in params.items():
        if isinstance(v, (list, tuple, set)):
            stmt = stmt.bindparams(bindparam(k, expanding=True))
    return db.session.execute(stmt, {k: (list(v) if isinstance(v, (set, tuple)) else v) for k, v in params.items()})


# ---------------------------------------------------------------------------
# settings: enrollment payment policy
# ---------------------------------------------------------------------------

def _setting(key, default=None):
    row = _q("SELECT setting_value FROM system_settings WHERE setting_key = :k", k=key).fetchone()
    return row[0] if row and row[0] not in (None, '') else default


def _set_setting(key, value):
    _q("INSERT INTO system_settings (setting_key, setting_value) VALUES (:k, :v) "
       "ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)", k=key, v=value)


def get_policy():
    mode = _setting('enrollment_payment_policy', 'clearance')
    if mode not in POLICY_MODES:
        mode = 'clearance'
    raw_min = _setting('enrollment_approval_min_payment')   # key already used by the uploaded code
    try:
        minimum = money(raw_min) if raw_min is not None else None
    except FinanceError:
        minimum = None
    return {'mode': mode, 'label': POLICY_MODES[mode], 'min_down_payment': minimum,
            'require_verification': _setting('enrollment_require_payment_verification', '1') == '1'}


def save_policy(actor, mode, min_down_payment, require_verification):
    if mode not in POLICY_MODES:
        raise FinanceError('Choose a payment policy.')
    minimum = None
    if mode == 'minimum':
        minimum = money(min_down_payment)
        if minimum <= 0:
            raise FinanceError('Enter the minimum down payment amount.')
    before = get_policy()
    _set_setting('enrollment_payment_policy', mode)
    if minimum is not None:
        _set_setting('enrollment_approval_min_payment', str(minimum))
    _set_setting('enrollment_require_payment_verification', '1' if require_verification else '0')
    _audit('policy', actor, details={'before': before, 'after': get_policy()})


# ---------------------------------------------------------------------------
# assessment generation from a finalized registration form
# ---------------------------------------------------------------------------

def residency_of(applicant):
    # Same rule the existing batch fees use (scope 'garciano' / 'non_garciano').
    return 'resident' if 'padre garcia' in (applicant.permanent_address_city_municipality or '').lower() else 'non_resident'


def matching_fee_rules(form):
    term = form.term
    title = (form.program.title or '').strip()
    yl = YEAR_LABELS.get(form.year_level)
    resident = residency_of(form.applicant) == 'resident'
    rules = []
    for r in _q("SELECT id, description, amount, semester, course, year_level, scope FROM batch_assessments_log ORDER BY id").mappings():
        def ok(v, want):
            return v in (None, '', 'All') or (want is not None and str(v).strip() == str(want).strip())
        if not (ok(r['semester'], term.semester) and ok(r['course'], title) and ok(r['year_level'], yl)):
            continue
        if r['scope'] == 'garciano' and not resident:
            continue
        if r['scope'] == 'non_garciano' and resident:
            continue
        rules.append(dict(r))
    return rules


def _student_applicant_ids(student_user_id):
    return [r[0] for r in _q("SELECT applicant_id FROM applicants WHERE student_user_id = :s", s=student_user_id)]


def generate_for_form(form, actor):
    """Create (or top up) the term assessment of a FINALIZED registration form.

    Charges already put on the student's ledger for this enrollment by the
    existing automatic fee step (same fee rule, created after the term was set
    up / after the student's previous assessment, not linked to any term yet)
    are ADOPTED instead of charged twice. Returns (assessment, report)."""
    if form.status != 'finalized':
        raise FinanceError('The registration form must be finalized before the assessment is generated.')
    ea = EnrollmentAssessment.query.filter_by(student_user_id=form.student_user_id,
                                              academic_term_id=form.academic_term_id).with_for_update().first()
    created_ea = ea is None
    if ea is None:
        ea = EnrollmentAssessment(registration_form_id=form.id, student_user_id=form.student_user_id,
                                  applicant_id=form.applicant_id, academic_term_id=form.academic_term_id,
                                  residency=residency_of(form.applicant), total_units=form.total_units,
                                  total_assessment=Decimal('0'), generated_by=actor.name, generated_at=datetime.now())
        db.session.add(ea)
        db.session.flush()
    else:
        ea.registration_form_id = form.id
        ea.total_units = form.total_units
        ea.regenerated_at = datetime.now()

    aids = _student_applicant_ids(form.student_user_id)
    prev = _q("SELECT MAX(generated_at) FROM enrollment_assessments WHERE student_user_id = :s AND id <> :e",
              s=form.student_user_id, e=ea.id).scalar()
    cutoff = max([d for d in (form.term.created_at, prev) if d is not None], default=None)

    rules = matching_fee_rules(form)
    have = {i.batch_id for i in ea.items if i.batch_id}
    created, adopted = [], []
    for r in rules:
        if r['id'] in have:
            continue
        params = {'aids': aids, 'b': r['id']}
        sql = ("SELECT a.id, a.description, a.total_amount FROM assessments a "
               "LEFT JOIN enrollment_assessment_items i ON i.assessment_id = a.id "
               "WHERE a.student_id IN :aids AND a.batch_id = :b AND i.id IS NULL")
        if cutoff is not None:
            sql += " AND a.created_at >= :cut"
            params['cut'] = cutoff
        sql += " ORDER BY a.id LIMIT 1 FOR UPDATE"
        row = _q(sql, **params).mappings().first()
        if row:
            aid, desc, amount, was_adopted = row['id'], row['description'], money(row['total_amount']), True
            adopted.append(desc)
        else:
            amount = money(r['amount'])
            res = _q("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status, created_at, batch_id) "
                     "VALUES (:sid, :d, :t, 0, :t, 'unpaid', NOW(), :b)", sid=form.applicant_id, d=r['description'], t=amount, b=r['id'])
            aid, desc, was_adopted = res.lastrowid, r['description'], False
            created.append(desc)
        item = EnrollmentAssessmentItem(enrollment_assessment_id=ea.id, assessment_id=aid, batch_id=r['id'],
                                        description=desc, amount=amount, adopted=was_adopted)
        ea.items.append(item)
    db.session.flush()
    ea.total_assessment = sum((money(i.amount) for i in ea.items), Decimal('0.00'))
    matched = {r['id'] for r in rules}
    no_longer = [i.description for i in ea.items if i.batch_id and i.batch_id not in matched]
    report = {'created': created, 'adopted': adopted, 'rules_matched': len(rules), 'no_longer_matching': no_longer,
              'total_assessment': ea.total_assessment, 'first_generation': created_ea}
    _audit('generate_assessment' if created_ea else 'regenerate_assessment', actor, applicant_id=form.applicant_id,
           ea_id=ea.id, details=report)
    return ea, report


def regenerate(form_id, actor):
    form = db.session.get(RegistrationForm, form_id)
    if form is None:
        raise FinanceError('Registration form not found.')
    return generate_for_form(form, actor)


# ---------------------------------------------------------------------------
# balances / statuses
# ---------------------------------------------------------------------------

def _term_key(term):
    sem = (term.semester or '').lower()
    s = 3 if 'summer' in sem else (2 if sem.startswith('2') else 1)
    return (term.year_name or '', s)


def payment_rows(applicant_ids):
    if not applicant_ids:
        return []
    return [dict(r) for r in _q("""
        SELECT p.id, p.student_id, p.assessment_id, p.amount_paid, p.payment_method, p.payment_date, p.receipt_number,
               p.remark, p.cashier_username, p.created_at,
               COALESCE(orig.payment_method, p.payment_method) AS category_method,
               (pr_in.id IS NOT NULL) AS is_reversal, (pr_out.id IS NOT NULL) AS is_reversed,
               pr_out.reason AS reversal_reason, pr_out.reversed_by AS reversed_by,
               pv.status AS verification, pv.verified_by, pv.verified_at
        FROM payments p
        LEFT JOIN payment_reversals pr_in ON pr_in.reversal_payment_id = p.id
        LEFT JOIN payments orig ON orig.id = pr_in.payment_id
        LEFT JOIN payment_reversals pr_out ON pr_out.payment_id = p.id
        LEFT JOIN payment_verifications pv ON pv.payment_id = p.id
        WHERE p.student_id IN :aids
        ORDER BY p.payment_date, p.id
    """, aids=applicant_ids).mappings()]


def summary(student_user_id, term_id):
    """Balance and statuses of one student for one term (dict of Decimals)."""
    term = db.session.get(AcademicTerm, term_id)
    aids = _student_applicant_ids(student_user_id)
    ea = EnrollmentAssessment.query.filter_by(student_user_id=student_user_id, academic_term_id=term_id).first()
    current = {i.assessment_id for i in ea.items} if ea else set()
    # charges of LATER terms are not "previous" when looking at an older term
    later = set()
    if term is not None:
        for other in EnrollmentAssessment.query.filter(EnrollmentAssessment.student_user_id == student_user_id,
                                                       EnrollmentAssessment.academic_term_id != term_id).all():
            if _term_key(db.session.get(AcademicTerm, other.academic_term_id)) > _term_key(term):
                later |= {i.assessment_id for i in other.items}
    charges = {r[0]: money(r[1]) for r in _q("SELECT id, total_amount FROM assessments WHERE student_id IN :aids", aids=aids)} if aids else {}
    total = sum((v for k, v in charges.items() if k in current), Decimal('0.00'))
    prev_charges = sum((v for k, v in charges.items() if k not in current and k not in later), Decimal('0.00'))
    discounts = payments = prev_credits = verified_paid = Decimal('0.00')
    unverified = verified = 0
    latest = None
    rows = payment_rows(aids)
    for p in rows:
        amt = money(p['amount_paid'])
        aid = p['assessment_id']
        if aid in later:
            continue
        if aid in current:
            if p['category_method'] in CREDIT_METHODS:
                discounts += amt
            else:
                payments += amt
                if amt > 0 and not p['is_reversed']:
                    if p['verification'] == 'verified':
                        verified += 1
                        verified_paid += amt
                    else:
                        unverified += 1
                    if latest is None or (p['payment_date'], p['id']) > (latest['payment_date'], latest['id']):
                        latest = p
        else:
            prev_credits += amt
    previous = prev_charges - prev_credits
    remaining = total + previous - discounts - payments
    if ea is None:
        pay_status = 'Unassessed'
    elif remaining <= 0 and total > 0:
        pay_status = 'Fully Paid'
    elif payments + discounts > 0:
        pay_status = 'Partially Paid'
    elif total == 0:
        pay_status = 'No Fees Assessed'
    else:
        pay_status = 'Unpaid'
    ver_status = 'For Verification' if unverified else ('Payment Verified' if verified else None)
    return {
        'assessment': ea, 'total_assessment': total, 'previous_balance': previous, 'discounts': discounts,
        'payments': payments, 'remaining': remaining, 'payment_status': pay_status,
        'verification_status': ver_status, 'unverified_count': unverified, 'verified_count': verified,
        'verified_payments': verified_paid, 'latest_payment': latest, 'term': term,
    }


def check_policy(student_user_id, term_id, s=None):
    """Does the student meet the configured enrollment payment policy?
    Returns (satisfied: bool, explanation: str)."""
    s = s or summary(student_user_id, term_id)
    pol = get_policy()
    if pol['mode'] == 'clearance':
        # Accounting's explicit decision; it does not depend on a generated assessment.
        row = _q("SELECT is_approved_by_accounting FROM student_enrollment_records WHERE student_user_id = :s AND academic_term_id = :t",
                 s=student_user_id, t=term_id).fetchone()
        ok = bool(row and row[0])
        return ok, ('Accounting has cleared the student.' if ok else 'Waiting for accounting clearance.')
    if s['assessment'] is None:
        return False, 'No assessment has been generated for this term (finalize the registration form).'
    counted = s['verified_payments'] if pol['require_verification'] else s['payments']
    note = ' (verified payments only)' if pol['require_verification'] else ''
    if pol['mode'] == 'full':
        left = s['total_assessment'] + s['previous_balance'] - s['discounts'] - counted
        return left <= 0, (f'Fully paid{note}.' if left <= 0 else f'{peso(left)} still to pay{note}.')
    if pol['mode'] == 'partial':
        return counted > 0, (f'Payment received{note}.' if counted > 0 else f'No payment yet{note}.')
    minimum = pol['min_down_payment']
    if minimum is None:
        return False, 'The minimum down payment is not configured.'
    ok = counted + s['discounts'] >= minimum
    return ok, (f'{peso(counted + s["discounts"])} paid of the {peso(minimum)} down payment{note}.')


# ---------------------------------------------------------------------------
# payments
# ---------------------------------------------------------------------------

def new_submission_token():
    return secrets.token_hex(16)


def resync_assessment(assessment_id):
    """amount_paid / balance / status recomputed from the payment rows."""
    row = _q("SELECT a.total_amount, COALESCE(SUM(p.amount_paid), 0) FROM assessments a "
             "LEFT JOIN payments p ON p.assessment_id = a.id WHERE a.id = :a GROUP BY a.id, a.total_amount", a=assessment_id).fetchone()
    if not row:
        return
    total, paid = money(row[0]), money(row[1])
    balance = max(Decimal('0.00'), total - paid)
    status = 'paid' if paid >= total and total > 0 else ('partial' if paid > 0 else 'unpaid')
    _q("UPDATE assessments SET amount_paid = :p, balance = :b, status = :s WHERE id = :a", p=paid, b=balance, s=status, a=assessment_id)


def record_payment(actor, applicant_id, assessment_id, amount, payment_date, method, remark=None, receipt_number=None, token=None):
    """Returns (payment_id, duplicate: bool)."""
    if not token or not re.fullmatch(r'[0-9a-f]{32}', str(token)):
        raise FinanceError('This payment form has expired. Reload the page and enter the payment again.')
    try:
        with db.session.begin_nested():
            db.session.add(PaymentSubmission(token=token, submitted_by=actor.name, created_at=datetime.now()))
            db.session.flush()
    except IntegrityError:
        prior = db.session.get(PaymentSubmission, token)
        return (prior.payment_id if prior else None), True
    amt = money(amount)
    if amt <= 0:
        raise FinanceError('Payment amount must be greater than zero.')
    try:
        pdate = datetime.strptime(str(payment_date), '%Y-%m-%d').date()
    except ValueError:
        raise FinanceError('Enter a valid payment date.')
    if pdate > date.today():
        raise FinanceError('The payment date cannot be in the future.')
    if method not in PAYMENT_METHODS:
        raise FinanceError('Choose a valid payment method.')
    receipt_number = (receipt_number or '').strip() or None
    if receipt_number:
        if len(receipt_number) > 50:
            raise FinanceError('Official receipt number is too long.')
        dup = _q("SELECT id FROM payments WHERE receipt_number = :r", r=receipt_number).fetchone()
        if dup:
            raise FinanceError(f'Official receipt no. {receipt_number} is already used by payment #{dup[0]}.')
    a = _q("SELECT id, description, total_amount FROM assessments WHERE id = :a AND student_id = :s FOR UPDATE",
           a=assessment_id, s=applicant_id).mappings().first()
    if not a:
        raise FinanceError('Invalid assessment selected for this student.')
    paid = money(_q("SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE assessment_id = :a", a=assessment_id).scalar())
    remaining = money(a['total_amount']) - paid
    if remaining <= 0:
        raise FinanceError(f"'{a['description']}' is already fully paid.")
    if amt > remaining:
        raise FinanceError(f"Payment of {peso(amt)} exceeds the remaining balance of {peso(remaining)} for '{a['description']}'.")
    res = _q("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, remark, receipt_number, cashier_username) "
             "VALUES (:s, :a, :amt, :d, :m, :r, :o, :c)", s=applicant_id, a=assessment_id, amt=amt, d=pdate, m=method,
             r=(remark or '')[:100] or None, o=receipt_number, c=actor.name)
    pid = res.lastrowid
    db.session.get(PaymentSubmission, token).payment_id = pid
    resync_assessment(assessment_id)
    _audit('record_payment', actor, applicant_id=applicant_id, payment_id=pid,
           details={'assessment': a['description'], 'amount': amt, 'method': method, 'receipt_number': receipt_number})
    return pid, False


def _payment(payment_id, lock=True):
    sql = "SELECT * FROM payments WHERE id = :p" + (" FOR UPDATE" if lock else "")
    p = _q(sql, p=payment_id).mappings().first()
    if not p:
        raise FinanceError('Payment not found.')
    return dict(p)


def verify_payment(actor, payment_id, notes=None):
    if actor.role != 'accounting':
        raise FinanceError('Only accounting personnel can verify payments.')
    p = _payment(payment_id)
    if money(p['amount_paid']) <= 0 or _q("SELECT 1 FROM payment_reversals WHERE reversal_payment_id = :p", p=payment_id).fetchone():
        raise FinanceError('A reversal entry is not a payment to verify.')
    if p['payment_method'] in CREDIT_METHODS:
        raise FinanceError('Discounts and vouchers are credits, not payments to verify.')
    if _q("SELECT 1 FROM payment_reversals WHERE payment_id = :p", p=payment_id).fetchone():
        raise FinanceError('This payment was reversed.')
    if db.session.query(PaymentVerification).filter_by(payment_id=payment_id).first():
        raise FinanceError('This payment is already verified.')
    if (p['cashier_username'] or '') == actor.name:
        raise FinanceError('You recorded this payment; another accounting staff member must verify it.')
    db.session.add(PaymentVerification(payment_id=payment_id, status='verified', verified_by=actor.name,
                                       verified_role=actor.role, verified_at=datetime.now(), notes=(notes or '').strip()[:255] or None))
    _audit('verify_payment', actor, applicant_id=p['student_id'], payment_id=payment_id,
           details={'amount': p['amount_paid'], 'receipt_number': p['receipt_number'], 'notes': notes})


def reverse_payment(actor, payment_id, reason):
    reason = (reason or '').strip()
    if not reason:
        raise FinanceError('Give the reason for the reversal.')
    p = _payment(payment_id)
    if money(p['amount_paid']) <= 0 or _q("SELECT 1 FROM payment_reversals WHERE reversal_payment_id = :p", p=payment_id).fetchone():
        raise FinanceError('A reversal entry cannot be reversed.')
    if _q("SELECT 1 FROM payment_reversals WHERE payment_id = :p", p=payment_id).fetchone():
        raise FinanceError('This payment was already reversed.')
    if p['payment_method'] in CREDIT_METHODS:
        raise FinanceError('Discounts and vouchers are removed from Discount Management / Return Voucher, not reversed here.')
    verified = db.session.query(PaymentVerification).filter_by(payment_id=payment_id).first()
    if actor.role == 'cashier':
        recorded_today = p['created_at'] is not None and p['created_at'].date() == date.today()
        if verified or (p['cashier_username'] or '') != actor.name or not recorded_today:
            raise FinanceError('A cashier can only reverse their own unverified payment on the day it was recorded. '
                               'Ask accounting to process this correction.')
    elif actor.role != 'accounting':
        raise FinanceError('Only cashier or accounting personnel can reverse payments.')
    amt = money(p['amount_paid'])
    res = _q("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, remark, receipt_number, cashier_username) "
             "VALUES (:s, :a, :amt, :d, 'Reversal', :r, NULL, :c)", s=p['student_id'], a=p['assessment_id'], amt=-amt,
             d=date.today(), r=f"Reversal of #{payment_id}"[:100], c=actor.name)
    rid = res.lastrowid
    db.session.add(PaymentReversal(payment_id=payment_id, reversal_payment_id=rid, reason=reason[:255], reversed_by=actor.name,
                                   reversed_role=actor.role, reversed_at=datetime.now()))
    if p['assessment_id']:
        resync_assessment(p['assessment_id'])
    _audit('reverse_payment', actor, applicant_id=p['student_id'], payment_id=payment_id,
           details={'amount': amt, 'reversal_payment_id': rid, 'reason': reason, 'was_verified': bool(verified)})
    return rid


def last_reversible_payment(applicant_id):
    row = _q("""SELECT p.id FROM payments p
                LEFT JOIN payment_reversals r1 ON r1.payment_id = p.id
                LEFT JOIN payment_reversals r2 ON r2.reversal_payment_id = p.id
                WHERE p.student_id = :s AND p.amount_paid > 0 AND r1.id IS NULL AND r2.id IS NULL
                  AND p.payment_method NOT IN ('Discount', 'Voucher')
                ORDER BY p.id DESC LIMIT 1""", s=applicant_id).fetchone()
    return row[0] if row else None
