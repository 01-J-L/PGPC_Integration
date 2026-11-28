"""Enrollment down payments: paid before the fees exist, deducted from the Tuition Fee
once the fees are prepared (and at the latest at check-in)."""

import re
from decimal import Decimal

import pytest

from conftest import login_as, _uniq
from test_phase3_regform import world, student_in, create, flashes                 # noqa: F401  (fixtures)
from test_phase4_finance import fees, run, A, reset_policy, summ                   # noqa: F401  (fixtures)
from test_phase5_checkin import check_in_json

CASH = ('Cash One', 'cashier')
ACCT = ('Acct One', 'accounting')


def svc():
    from website.finance import service
    return service


def pay_down(client, s, amount, receipt=None):
    """Through the cashier's real form, option 'Enrollment down payment'."""
    login_as(client, 'cashier', name='Cash One')
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'value="down"' in page
    tok = re.search(r'name="submission_token" value="([0-9a-f]{32})"', page).group(1)
    data = {'assessment_id': 'down', 'amount': amount, 'payment_date': '2026-06-01', 'payment_method': 'Cash',
            'submission_token': tok, 'receipt_number': receipt or _uniq('DP')[:30]}
    client.post('/cashier/payment/%d' % s['applicant_id'], data=data)
    return data


def dp_row(dbx, s):
    return dbx.one("SELECT d.*, p.assessment_id AS pay_assessment FROM down_payments d JOIN payments p ON p.id=d.payment_id "
                   "WHERE d.student_user_id=%s ORDER BY d.id DESC LIMIT 1", (s['student_user_id'],))


def finalize(client, dbx, s, world):
    login_as(client, 'registrar', name='Reg One')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'finalized'
    return fid


def charge(dbx, s, desc):
    return dbx.one("SELECT id, amount_paid, balance, status FROM assessments WHERE student_id=%s AND description=%s", (s['applicant_id'], desc))


def test_down_payment_before_fees_is_deducted_from_tuition(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    data = pay_down(client, s, '1,500.00')
    dp = dp_row(dbx, s)
    assert dp['status'] == 'held' and dp['pay_assessment'] is None and dp['amount'] == Decimal('1500.00')
    assert dbx.one("SELECT remark FROM payments WHERE id=%s", (dp['payment_id'],))['remark'].startswith('Enrollment down payment')
    sm = summ(app, s, world)
    assert sm['assessment'] is None and sm['payments'] == Decimal('1500.00') and sm['held_down_payments'] == Decimal('1500.00')
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'waiting for the Tuition Fee' in page and 'Includes a down payment of' in page

    # Accounting finds it and verifies it even though the student has no assessment yet
    login_as(client, 'accounting', name='Acct One')
    page = client.get('/accounting/enrollment-finance/?term_id=%d' % world['t1']).get_data(as_text=True)
    assert 'id="down-payments"' in page and data['receipt_number'] in page
    client.post('/accounting/enrollment-finance/payments/%d/verify' % dp['payment_id'])
    assert dbx.one("SELECT verified_by FROM payment_verifications WHERE payment_id=%s", (dp['payment_id'],))['verified_by'] == 'Acct One'

    # the registrar finalizes: the down payment moves onto the Tuition Fee
    finalize(client, dbx, s, world)
    dp = dp_row(dbx, s)
    tuition = charge(dbx, s, 'Tuition test')
    assert dp['status'] == 'applied' and dp['pay_assessment'] == tuition['id'] == dp['applied_assessment_id']
    assert tuition['amount_paid'] == Decimal('1500.00') and tuition['balance'] == Decimal('1500.50') and tuition['status'] == 'partial'
    assert charge(dbx, s, 'Misc test')['amount_paid'] == 0
    sm = summ(app, s, world)
    assert sm['total_assessment'] == Decimal('4200.75') and sm['payments'] == Decimal('1500.00')
    assert sm['remaining'] == Decimal('2700.75') and sm['held_down_payments'] == 0 and sm['verified_payments'] == Decimal('1500.00')
    assert dbx.one("SELECT verified_by FROM payment_verifications WHERE payment_id=%s", (dp['payment_id'],))   # verification kept
    log = dbx.one("SELECT details FROM finance_audit_log WHERE action='apply_down_payment' AND payment_id=%s", (dp['payment_id'],))
    assert 'Tuition test' in log['details']
    login_as(client, 'cashier', name='Cash One')
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'deducted from Tuition test' in page


def test_minimum_down_payment_met_before_assessment(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    run(app, lambda: svc().save_policy(A(*ACCT), 'minimum', '1500', True))
    req = run(app, lambda: svc().enrollment_requirement(s['student_user_id'], world['t1']))
    assert not req['ok'] and req['still_to_pay'] == Decimal('1500.00')
    pay_down(client, s, '1500')
    pid = dp_row(dbx, s)['payment_id']
    assert not run(app, lambda: svc().check_policy(s['student_user_id'], world['t1']))[0]          # not verified yet
    run(app, lambda: svc().verify_payment(A(*ACCT), pid))
    ok, why = run(app, lambda: svc().check_policy(s['student_user_id'], world['t1']))
    assert ok and '1,500.00' in why
    # full-payment mode still needs the assessment
    run(app, lambda: svc().save_policy(A(*ACCT), 'full', None, True))
    assert not run(app, lambda: svc().check_policy(s['student_user_id'], world['t1']))[0]


def test_down_payment_after_fees_exist_goes_to_tuition_at_once(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    finalize(client, dbx, s, world)
    pay_down(client, s, '1000')
    dp = dp_row(dbx, s)
    assert dp['status'] == 'applied' and charge(dbx, s, 'Tuition test')['amount_paid'] == Decimal('1000.00')


def test_check_in_applies_a_held_down_payment(app, client, dbx, world, fees):
    from test_phase5_checkin import approve
    s = student_in(dbx, world, section='1A')
    finalize(client, dbx, s, world)
    # a down payment that was still waiting when the student is checked in
    tuition = charge(dbx, s, 'Tuition test')
    pid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, remark, cashier_username) "
                "VALUES (%s,NULL,800,CURDATE(),'Cash','Enrollment down payment','Cash One')", (s['applicant_id'],))
    dbx.x("INSERT INTO down_payments (payment_id, student_user_id, applicant_id, academic_term_id, amount, status, created_by) "
          "VALUES (%s,%s,%s,%s,800,'held','Cash One')", (pid, s['student_user_id'], s['applicant_id'], world['t1']))
    s['form_id'] = dbx.one("SELECT id FROM registration_forms WHERE applicant_id=%s", (s['applicant_id'],))['id']
    approve(app, s, world, 'documents')
    approve(app, s, world, 'payment', who=ACCT)
    r = check_in_json(client, s, world)
    body = r.get_json()
    assert body['success'] and 'Down payment of ₱800.00 deducted from Tuition test' in body['message'], body
    assert dbx.one("SELECT assessment_id FROM payments WHERE id=%s", (pid,))['assessment_id'] == tuition['id']
    assert dp_row(dbx, s)['status'] == 'applied'


def test_down_payment_too_big_for_any_charge_stays_as_credit(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    pay_down(client, s, '3500')                         # tuition 3,000.50 and misc 1,200.25 cannot take it whole
    finalize(client, dbx, s, world)
    dp = dp_row(dbx, s)
    assert dp['status'] == 'held' and dp['pay_assessment'] is None
    sm = summ(app, s, world)
    assert sm['payments'] == Decimal('3500.00') and sm['remaining'] == Decimal('700.75')      # still counted
    login_as(client, 'accounting')
    assert 'id="down-payments"' in client.get('/accounting/enrollment-finance/?term_id=%d' % world['t1']).get_data(as_text=True)


def test_reversed_down_payment_is_never_applied(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    pay_down(client, s, '1000')
    pid = dp_row(dbx, s)['payment_id']
    run(app, lambda: svc().reverse_payment(A(*CASH), pid, 'wrong student'))
    assert dp_row(dbx, s)['status'] == 'reversed'
    sm = summ(app, s, world)
    assert sm['payments'] == 0 and sm['held_down_payments'] == 0
    finalize(client, dbx, s, world)
    assert charge(dbx, s, 'Tuition test')['amount_paid'] == 0
    assert dbx.one("SELECT assessment_id FROM payments WHERE id=%s", (pid,))['assessment_id'] is None


def test_down_payment_rules(app, client, dbx, world, fees):
    E = svc().FinanceError
    s = student_in(dbx, world, section='1A')
    tok = svc().new_submission_token
    with pytest.raises(E, match='Only cashier or accounting'):
        run(app, lambda: svc().record_down_payment(A('Reg', 'registrar'), s['applicant_id'], world['t1'], '100', '2026-06-01', 'Cash', token=tok()))
    with pytest.raises(E, match='greater than zero'):
        run(app, lambda: svc().record_down_payment(A(*CASH), s['applicant_id'], world['t1'], '0', '2026-06-01', 'Cash', token=tok()))
    t = tok()
    p1 = run(app, lambda: svc().record_down_payment(A(*CASH), s['applicant_id'], world['t1'], '100', '2026-06-01', 'Cash', token=t))
    p2 = run(app, lambda: svc().record_down_payment(A(*CASH), s['applicant_id'], world['t1'], '100', '2026-06-01', 'Cash', token=t))
    assert p1[0] == p2[0] and p2[1] is True                                                     # double submit -> once
    assert dbx.one("SELECT COUNT(*) n FROM down_payments WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 1
    # once checked in, money goes to the fees, not to a down payment
    dbx.x("INSERT INTO enrollment_checkins (student_user_id, applicant_id, academic_term_id, active, checklist_snapshot, checked_in_by, checked_in_role) "
          "VALUES (%s,%s,%s,1,'[]','x','registrar')", (s['student_user_id'], s['applicant_id'], world['t1']))
    with pytest.raises(E, match='already enrolled'):
        run(app, lambda: svc().record_down_payment(A(*CASH), s['applicant_id'], world['t1'], '100', '2026-06-01', 'Cash', token=tok()))
    login_as(client, 'cashier')
    assert 'value="down"' not in client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)


def test_receipt_names_the_down_payment(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    pay_down(client, s, '500')
    pid = dp_row(dbx, s)['payment_id']
    login_as(client, 'cashier')
    r = client.get('/cashier/print-receipt/%d' % pid)
    assert r.status_code == 200
    assert 'Enrollment down payment' in r.get_data(as_text=True)
