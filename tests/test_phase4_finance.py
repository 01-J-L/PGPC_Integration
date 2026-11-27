"""Phase 4: enrollment assessment, balance, payments, verification, reversal, policy."""

import datetime
import re
from decimal import Decimal

import pytest

from conftest import login_as, _uniq
from test_phase3_regform import world, student_in, create, flashes  # noqa: F401  (world is a fixture)


def A(name, role):
    from website.curriculum.service import Actor
    return Actor(name, role)


CASHIER = ('Cash One', 'cashier')
ACCT = ('Acct One', 'accounting')
ACCT2 = ('Acct Two', 'accounting')


@pytest.fixture(autouse=True)
def reset_policy(dbx):
    yield
    dbx.x("DELETE FROM system_settings WHERE setting_key IN ('enrollment_payment_policy','enrollment_approval_min_payment',"
          "'enrollment_require_payment_verification')")


@pytest.fixture()
def fees(dbx, world):
    """Two fee rules for the world's program, 1st year, 1st semester (made-up test amounts)."""
    t = world['title']
    r1 = dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
               "VALUES ('Tuition test', 3000.50, '1st Semester', %s, '1st Year', 'all')", (t,))
    r2 = dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
               "VALUES ('Misc test', 1200.25, '1st Semester', %s, '1st Year', 'all')", (t,))
    # rule for non-residents only: must not apply to a Padre Garcia resident
    dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
          "VALUES ('Non-resident fee', 999, '1st Semester', %s, '1st Year', 'non_garciano')", (t,))
    # rule of another year level: must not apply
    dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
          "VALUES ('2nd year fee', 777, '1st Semester', %s, '2nd Year', 'all')", (t,))
    return {'tuition': r1, 'misc': r2}


def finalized(client, dbx, world, section='1A'):
    s = student_in(dbx, world, section=section)
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'finalized', flashes(client)
    s['form_id'] = fid
    return s


def run(app, fn):
    with app.app_context():
        from website import db
        try:
            out = fn()
            db.session.commit()
            return out
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()


def summ(app, s, world):
    from website.finance import service
    return run(app, lambda: service.summary(s['student_user_id'], world['t1']))


def service_money(v):
    from website.finance.service import money
    return money(v)


def charge_ids(dbx, s):
    return [r['id'] for r in dbx.q("SELECT id FROM assessments WHERE student_id=%s ORDER BY id", (s['applicant_id'],))]


def pay(app, s, assessment_id, amount, who=CASHIER, receipt=None, token=None, method='Cash'):
    from website.finance import service
    tok = token or service.new_submission_token()
    return run(app, lambda: service.record_payment(A(*who), s['applicant_id'], assessment_id, amount,
                                                   datetime.date.today().isoformat(), method, None, receipt, tok))


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

def test_finalize_generates_assessment_from_matching_rules_only(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    ea = dbx.one("SELECT * FROM enrollment_assessments WHERE student_user_id=%s AND academic_term_id=%s", (s['student_user_id'], world['t1']))
    assert ea and ea['registration_form_id'] == s['form_id'] and ea['residency'] == 'resident'
    assert ea['total_assessment'] == Decimal('4200.75')
    items = dbx.q("SELECT description, amount, adopted FROM enrollment_assessment_items WHERE enrollment_assessment_id=%s ORDER BY id", (ea['id'],))
    assert [(i['description'], i['amount'], i['adopted']) for i in items] == [('Tuition test', Decimal('3000.50'), 0), ('Misc test', Decimal('1200.25'), 0)]
    rows = dbx.q("SELECT description, total_amount, balance, status FROM assessments WHERE student_id=%s ORDER BY id", (s['applicant_id'],))
    assert [r['description'] for r in rows] == ['Tuition test', 'Misc test']
    sm = summ(app, s, world)
    assert sm['total_assessment'] == Decimal('4200.75') and sm['remaining'] == Decimal('4200.75')
    assert sm['payment_status'] == 'Unpaid' and sm['verification_status'] is None


def test_no_matching_rule_warns_and_records_zero_assessment(app, client, dbx, world):
    s = finalized(client, dbx, world)
    assert 'No fee rule of Accounting matches' in flashes(client)
    ea = dbx.one("SELECT total_assessment FROM enrollment_assessments WHERE student_user_id=%s", (s['student_user_id'],))
    assert ea['total_assessment'] == 0
    assert summ(app, s, world)['payment_status'] == 'No Fees Assessed'


def test_existing_auto_charge_is_adopted_not_charged_twice(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    # the legacy batch step already put the tuition on the ledger for this enrollment
    dbx.x("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status, created_at, batch_id) "
          "VALUES (%s,'Tuition test',3000.50,0,3000.50,'unpaid',NOW(),%s)", (s['applicant_id'], fees['tuition']))
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    rows = dbx.q("SELECT description FROM assessments WHERE student_id=%s ORDER BY id", (s['applicant_id'],))
    assert [r['description'] for r in rows] == ['Tuition test', 'Misc test']
    adopted = dbx.q("SELECT i.description, i.adopted FROM enrollment_assessment_items i JOIN enrollment_assessments e "
                    "ON e.id=i.enrollment_assessment_id WHERE e.registration_form_id=%s ORDER BY i.id", (fid,))
    assert [(r['description'], r['adopted']) for r in adopted] == [('Tuition test', 1), ('Misc test', 0)]
    assert summ(app, s, world)['total_assessment'] == Decimal('4200.75')


def test_regenerate_is_idempotent_and_only_adds_new_rules(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    from website.finance import service
    rep = run(app, lambda: service.regenerate(s['form_id'], A(*ACCT))[1])
    assert rep['created'] == [] and rep['adopted'] == []
    assert len(charge_ids(dbx, s)) == 2
    dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
          "VALUES ('Lab test', 500, '1st Semester', %s, '1st Year', 'all')", (world['title'],))
    rep = run(app, lambda: service.regenerate(s['form_id'], A(*ACCT))[1])
    assert rep['created'] == ['Lab test']
    assert summ(app, s, world)['total_assessment'] == Decimal('4700.75')
    # reopening + finalizing again does not double-charge either
    login_as(client, 'registrar')
    client.post('/admin/registration/forms/%d/reopen' % s['form_id'], data={'reason': 'fix section'})
    client.post('/admin/registration/forms/%d/finalize' % s['form_id'])
    assert len(charge_ids(dbx, s)) == 3


def test_regenerate_route_is_accounting_only(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    for role in ('cashier', 'registrar'):
        login_as(client, role)
        client.post('/accounting/enrollment-finance/forms/%d/regenerate' % s['form_id'])
        assert 'Only accounting' in flashes(client) or role == 'registrar'
    login_as(client, 'accounting')
    client.post('/accounting/enrollment-finance/forms/%d/regenerate' % s['form_id'])
    assert 'Assessment updated' in flashes(client)


# ---------------------------------------------------------------------------
# balance formula
# ---------------------------------------------------------------------------

def test_balance_formula_with_previous_balance_discount_and_payments(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    # an older unpaid charge (previous term), partly paid
    old = dbx.x("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status, created_at) "
                "VALUES (%s,'Old balance',1000.00,0,1000.00,'unpaid','2025-06-01')", (s['applicant_id'],))
    dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
          "VALUES (%s,%s,250.10,'2025-06-02','Cash','legacy')", (s['applicant_id'], old))
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    tuition, misc = [i for i in charge_ids(dbx, s) if i != old]
    # discount = credit on the tuition (inserted like Discount Management does)
    dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
          "VALUES (%s,%s,500.00,CURDATE(),'Discount','acct')", (s['applicant_id'], tuition))
    pay(app, s, tuition, '1000.20')
    pay(app, s, misc, '0.30')
    sm = summ(app, s, world)
    assert sm['total_assessment'] == Decimal('4200.75')
    assert sm['previous_balance'] == Decimal('749.90')
    assert sm['discounts'] == Decimal('500.00')
    assert sm['payments'] == Decimal('1000.50')
    assert sm['remaining'] == Decimal('4200.75') + Decimal('749.90') - Decimal('500.00') - Decimal('1000.50')
    assert isinstance(sm['remaining'], Decimal)
    assert sm['payment_status'] == 'Partially Paid' and sm['verification_status'] == 'For Verification'


def test_fully_paid_status_and_resync_of_legacy_columns(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    pay(app, s, tuition, '3000.50')
    pay(app, s, misc, '1200.25')
    sm = summ(app, s, world)
    assert sm['remaining'] == 0 and sm['payment_status'] == 'Fully Paid'
    row = dbx.one("SELECT amount_paid, balance, status FROM assessments WHERE id=%s", (tuition,))
    assert row['amount_paid'] == Decimal('3000.50') and row['balance'] == 0 and row['status'] == 'paid'


# ---------------------------------------------------------------------------
# recording payments
# ---------------------------------------------------------------------------

def test_payment_validations(app, client, dbx, world, fees):
    from website.finance.service import FinanceError
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    with pytest.raises(FinanceError, match='exceeds the remaining balance'):
        pay(app, s, tuition, '3000.51')
    with pytest.raises(FinanceError, match='greater than zero'):
        pay(app, s, tuition, '0')
    for bad in ('abc', 'NaN', 'Infinity', '1e400'):
        with pytest.raises(FinanceError):
            pay(app, s, tuition, bad)
    assert service_money('1,234.565') == Decimal('1234.57')
    with pytest.raises(FinanceError, match='valid payment method'):
        pay(app, s, tuition, '10', method='Bitcoin')
    with pytest.raises(FinanceError, match='expired'):
        pay(app, s, tuition, '10', token='not-a-token')
    other = student_in(dbx, world, section='1A')
    with pytest.raises(FinanceError, match='Invalid assessment'):
        pay(app, other, tuition, '10')
    assert dbx.one("SELECT COUNT(*) n FROM payments WHERE student_id=%s", (s['applicant_id'],))['n'] == 0


def test_duplicate_submission_token_records_once(app, client, dbx, world, fees):
    from website.finance import service
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    tok = service.new_submission_token()
    pid1, dup1 = pay(app, s, tuition, '100', token=tok)
    pid2, dup2 = pay(app, s, tuition, '100', token=tok)
    assert (dup1, dup2) == (False, True) and pid1 == pid2
    assert dbx.one("SELECT COUNT(*) n FROM payments WHERE student_id=%s", (s['applicant_id'],))['n'] == 1


def test_official_receipt_number_is_unique(app, client, dbx, world, fees):
    from website.finance.service import FinanceError
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    orno = _uniq('OR')[:40]
    pay(app, s, tuition, '100', receipt=orno)
    with pytest.raises(FinanceError, match='already used'):
        pay(app, s, misc, '100', receipt=orno)


def test_payment_route_records_once_and_shows_history(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    login_as(client, 'cashier', name='Cash One')
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    tok = re.search(r'name="submission_token" value="([0-9a-f]{32})"', page).group(1)
    assert 'Remaining Balance' in page
    data = {'assessment_id': tuition, 'amount': '150.00', 'payment_date': datetime.date.today().isoformat(),
            'payment_method': 'Cash', 'submission_token': tok, 'receipt_number': _uniq('R')[:40]}
    client.post('/cashier/payment/%d' % s['applicant_id'], data=data)
    client.post('/cashier/payment/%d' % s['applicant_id'], data=data)     # double click / back + resubmit
    assert dbx.one("SELECT COUNT(*) n FROM payments WHERE student_id=%s", (s['applicant_id'],))['n'] == 1
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'For verification' in page and '150.00' in page


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def test_verification_rules(app, client, dbx, world, fees):
    from website.finance import service
    from website.finance.service import FinanceError
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    pid, _ = pay(app, s, tuition, '500')
    own, _ = pay(app, s, misc, '100', who=ACCT)
    with pytest.raises(FinanceError, match='Only accounting'):
        run(app, lambda: service.verify_payment(A(*CASHIER), pid))
    with pytest.raises(FinanceError, match='Only accounting'):
        run(app, lambda: service.verify_payment(A('Reg', 'registrar'), pid))
    with pytest.raises(FinanceError, match='another accounting'):
        run(app, lambda: service.verify_payment(A(*ACCT), own))
    run(app, lambda: service.verify_payment(A(*ACCT), pid))
    with pytest.raises(FinanceError, match='already verified'):
        run(app, lambda: service.verify_payment(A(*ACCT2), pid))
    v = dbx.one("SELECT verified_by, verified_role FROM payment_verifications WHERE payment_id=%s", (pid,))
    assert v == {'verified_by': 'Acct One', 'verified_role': 'accounting'}
    run(app, lambda: service.verify_payment(A(*ACCT2), own))
    assert summ(app, s, world)['verification_status'] == 'Payment Verified'
    disc = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
                 "VALUES (%s,%s,10,CURDATE(),'Discount','x')", (s['applicant_id'], misc))
    with pytest.raises(FinanceError, match='credits'):
        run(app, lambda: service.verify_payment(A(*ACCT), disc))


def test_student_cannot_reach_verification(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    pid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
                "VALUES (%s,%s,10,CURDATE(),'Cash','c')", (s['applicant_id'], tuition))
    from conftest import login_student
    c2 = client.application.test_client()
    login_student(c2, s)
    r = c2.post('/accounting/enrollment-finance/payments/%d/verify' % pid)
    assert r.status_code in (302, 403)
    assert not dbx.one("SELECT id FROM payment_verifications WHERE payment_id=%s", (pid,))
    assert c2.get('/accounting/enrollment-finance/').status_code in (302, 403)


def test_verify_route(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    pid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
                "VALUES (%s,%s,10,CURDATE(),'Cash','Cash One')", (s['applicant_id'], tuition))
    login_as(client, 'cashier', name='Cash One')
    client.post('/accounting/enrollment-finance/payments/%d/verify' % pid)
    assert 'Only accounting' in flashes(client)
    login_as(client, 'accounting', name='Acct One')
    client.post('/accounting/enrollment-finance/payments/%d/verify' % pid)
    assert dbx.one("SELECT verified_by FROM payment_verifications WHERE payment_id=%s", (pid,))['verified_by'] == 'Acct One'


# ---------------------------------------------------------------------------
# reversal
# ---------------------------------------------------------------------------

def test_reversal_keeps_history_and_restores_balance(app, client, dbx, world, fees):
    from website.finance import service
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    pid, _ = pay(app, s, tuition, '700')
    rid = run(app, lambda: service.reverse_payment(A(*CASHIER), pid, 'wrong student'))
    rows = dbx.q("SELECT id, amount_paid, payment_method FROM payments WHERE student_id=%s ORDER BY id", (s['applicant_id'],))
    assert [(r['id'], r['amount_paid'], r['payment_method']) for r in rows] == [(pid, Decimal('700.00'), 'Cash'), (rid, Decimal('-700.00'), 'Reversal')]
    assert dbx.one("SELECT reason, reversed_by FROM payment_reversals WHERE payment_id=%s", (pid,)) == {'reason': 'wrong student', 'reversed_by': 'Cash One'}
    sm = summ(app, s, world)
    assert sm['payments'] == 0 and sm['remaining'] == Decimal('4200.75') and sm['verification_status'] is None
    assert dbx.one("SELECT balance, status FROM assessments WHERE id=%s", (tuition,)) == {'balance': Decimal('3000.50'), 'status': 'unpaid'}
    log = dbx.one("SELECT COUNT(*) n FROM finance_audit_log WHERE payment_id=%s AND action='reverse_payment'", (pid,))
    assert log['n'] == 1


def test_reversal_permissions(app, client, dbx, world, fees):
    from website.finance import service
    from website.finance.service import FinanceError
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    pid, _ = pay(app, s, tuition, '100')
    with pytest.raises(FinanceError, match='reason'):
        run(app, lambda: service.reverse_payment(A(*ACCT), pid, '  '))
    with pytest.raises(FinanceError, match='own unverified payment'):
        run(app, lambda: service.reverse_payment(A('Cash Two', 'cashier'), pid, 'x'))
    with pytest.raises(FinanceError, match='Only cashier or accounting'):
        run(app, lambda: service.reverse_payment(A('Reg', 'registrar'), pid, 'x'))
    run(app, lambda: service.verify_payment(A(*ACCT), pid))
    with pytest.raises(FinanceError, match='own unverified payment'):
        run(app, lambda: service.reverse_payment(A(*CASHIER), pid, 'x'))      # verified: cashier may not
    # yesterday's payment: cashier may not either
    old = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username, created_at) "
                "VALUES (%s,%s,50,CURDATE() - INTERVAL 1 DAY,'Cash','Cash One',NOW() - INTERVAL 1 DAY)", (s['applicant_id'], misc))
    with pytest.raises(FinanceError, match='own unverified payment'):
        run(app, lambda: service.reverse_payment(A(*CASHIER), old, 'x'))
    rid = run(app, lambda: service.reverse_payment(A(*ACCT), pid, 'OR typo, re-issued'))
    with pytest.raises(FinanceError, match='already reversed'):
        run(app, lambda: service.reverse_payment(A(*ACCT), pid, 'again'))
    with pytest.raises(FinanceError, match='reversal entry'):
        run(app, lambda: service.reverse_payment(A(*ACCT), rid, 'x'))
    with pytest.raises(FinanceError, match='reversal entry'):
        run(app, lambda: service.verify_payment(A(*ACCT2), rid))
    with pytest.raises(FinanceError, match='was reversed'):
        run(app, lambda: service.verify_payment(A(*ACCT2), pid))
    # the verification row of the original is kept (history is not rewritten)
    assert dbx.one("SELECT id FROM payment_verifications WHERE payment_id=%s", (pid,))


def test_undo_route_reverses_instead_of_deleting(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    pid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username, created_at) "
                "VALUES (%s,%s,25,CURDATE(),'Cash','Cash One',NOW())", (s['applicant_id'], tuition))
    login_as(client, 'cashier', name='Cash One')
    client.post('/cashier/payment/%d/undo' % s['applicant_id'], data={'payment_id': pid})
    assert 'reason' in flashes(client).lower()
    client.post('/cashier/payment/%d/undo' % s['applicant_id'], data={'payment_id': pid, 'reason': 'duplicate'})
    assert dbx.one("SELECT COUNT(*) n FROM payments WHERE student_id=%s", (s['applicant_id'],))['n'] == 2
    assert dbx.one("SELECT id FROM payment_reversals WHERE payment_id=%s", (pid,))
    # someone else's payment id in the URL of this student is refused
    other = finalized(client, dbx, world)
    oid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username, created_at) "
                "VALUES (%s,%s,25,CURDATE(),'Cash','Cash One',NOW())", (other['applicant_id'], charge_ids(dbx, other)[0]))
    login_as(client, 'cashier', name='Cash One')
    client.post('/cashier/payment/%d/undo' % s['applicant_id'], data={'payment_id': oid, 'reason': 'x'})
    assert 'does not belong' in flashes(client)
    assert not dbx.one("SELECT id FROM payment_reversals WHERE payment_id=%s", (oid,))


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

def test_policy_modes(app, client, dbx, world, fees):
    from website.finance import service
    from website.finance.service import FinanceError
    s = finalized(client, dbx, world)
    tuition, misc = charge_ids(dbx, s)
    chk = lambda: run(app, lambda: service.check_policy(s['student_user_id'], world['t1']))   # noqa: E731
    setp = lambda *a: run(app, lambda: service.save_policy(A(*ACCT), *a))                    # noqa: E731

    assert service.POLICY_MODES and chk()[0] is False                                         # default: clearance
    dbx.x("INSERT INTO student_enrollment_records (student_user_id, academic_term_id, is_approved_by_accounting) VALUES (%s,%s,1) "
          "ON DUPLICATE KEY UPDATE is_approved_by_accounting=1", (s['student_user_id'], world['t1']))
    assert chk()[0] is True

    setp('partial', None, True)
    assert chk()[0] is False
    pid, _ = pay(app, s, tuition, '1000')
    assert chk() == (False, 'No payment yet (verified payments only).')                       # not verified yet
    setp('partial', None, False)
    assert chk()[0] is True
    setp('partial', None, True)
    run(app, lambda: service.verify_payment(A(*ACCT2), pid))
    assert chk()[0] is True

    with pytest.raises(FinanceError, match='minimum down payment'):
        setp('minimum', '0', True)
    setp('minimum', '1500', True)
    ok, why = chk()
    assert ok is False and '1,000.00' in why and '1,500.00' in why
    dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
          "VALUES (%s,%s,500,CURDATE(),'Discount','acct')", (s['applicant_id'], misc))
    assert chk()[0] is True                                                                  # discount counts toward it

    setp('full', None, True)
    ok, why = chk()
    assert ok is False and '2,700.75' in why
    with pytest.raises(FinanceError):
        setp('anything', None, True)


def test_policy_route_permissions(client, dbx):
    for role in ('cashier', 'registrar'):
        login_as(client, role)
        client.post('/accounting/enrollment-finance/policy', data={'mode': 'full', 'require_verification': '1'})
    assert not dbx.one("SELECT 1 x FROM system_settings WHERE setting_key='enrollment_payment_policy'")
    login_as(client, 'accounting')
    client.post('/accounting/enrollment-finance/policy', data={'mode': 'minimum', 'min_down_payment': '2,000.00'})
    got = {r['setting_key']: r['setting_value'] for r in dbx.q("SELECT setting_key, setting_value FROM system_settings WHERE setting_key LIKE 'enrollment_%%'")}
    assert got['enrollment_payment_policy'] == 'minimum' and got['enrollment_approval_min_payment'] == '2000.00'
    assert got['enrollment_require_payment_verification'] == '0'
    assert dbx.one("SELECT COUNT(*) n FROM finance_audit_log WHERE action='policy'")['n'] >= 1


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------

def test_accounting_list_filters_and_registrar_panels(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    tuition, _ = charge_ids(dbx, s)
    pay(app, s, tuition, '100')
    for role in ('accounting', 'cashier', 'admin'):
        login_as(client, role)
        page = client.get('/accounting/enrollment-finance/?term_id=%d' % world['t1']).get_data(as_text=True)
        assert 'Dela Cruz, Juan' in page and 'Partially Paid' in page, role
    page = client.get('/accounting/enrollment-finance/?term_id=%d&status=Fully+Paid' % world['t1']).get_data(as_text=True)
    sid = dbx.one("SELECT control_number FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['control_number']
    assert '>%s<' % sid not in page
    page = client.get('/accounting/enrollment-finance/?term_id=%d&status=For+Verification' % world['t1']).get_data(as_text=True)
    assert '>%s<' % sid in page
    login_as(client, 'registrar')
    assert client.get('/accounting/enrollment-finance/').status_code == 302
    page = client.get('/admin/registration/forms/%d' % s['form_id']).get_data(as_text=True)
    assert 'Assessment &amp; payments' in page and '4,100.75' in page and 'Waiting for accounting clearance' in page
    assert 'Verify</button>' not in page and 'Reverse</button>' not in page
