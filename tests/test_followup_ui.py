"""Follow-up after Phase 9: ID search, the Checklist Approvals page for every office,
and the down payment shown to the cashier and the student."""

import re
from decimal import Decimal

import pytest

from conftest import login_as, login_student
from test_phase3_regform import world, student_in, create, flashes                 # noqa: F401  (fixtures)
from test_phase4_finance import fees, finalized, run, A, reset_policy             # noqa: F401  (fixtures)
from test_phase5_checkin import check_in_json

ACCT = ('Acct One', 'accounting')


def student_no(dbx, s):
    return dbx.one("SELECT control_number FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['control_number']


def listed(page, s):
    return '/admin/registration/student/%d' % s['applicant_id'] in page or ('>%s<' % s['_no']) in page or s['_no'] in page


# ---------------------------------------------------------------------------
# ID search on Registration Forms
# ---------------------------------------------------------------------------

def test_registration_forms_id_search(app, client, dbx, world):
    a, b = student_in(dbx, world), student_in(dbx, world)
    for s in (a, b):
        s['_no'] = student_no(dbx, s)
    login_as(client, 'registrar')
    fa = create(client, a, world['t1'])
    create(client, b, world['t1'])
    base = '/admin/registration/?term_id=%d' % world['t1']
    page = client.get(base + '&q=' + a['_no']).get_data(as_text=True)
    assert a['_no'] in page and b['_no'] not in page and 'Clear search' in page
    page = client.get(base + '&q=' + a['_no'].lower()).get_data(as_text=True)          # case-insensitive
    assert a['_no'] in page
    dbx.x("UPDATE registration_forms SET control_number='PGPC-26-7777' WHERE id=%s", (fa,))
    page = client.get(base + '&q=PGPC-26-7777').get_data(as_text=True)
    assert a['_no'] in page and b['_no'] not in page and 'PGPC-26-7777' in page
    page = client.get(base + '&q=nobody-matches-this').get_data(as_text=True)
    assert a['_no'] not in page and b['_no'] not in page
    # the "no form yet" list is searched too
    c = student_in(dbx, world)
    c_no = student_no(dbx, c)
    page = client.get(base + '&q=' + c_no).get_data(as_text=True)
    assert c_no in page and a['_no'] not in page


def test_check_in_queue_search(app, client, dbx, world):
    a, b = student_in(dbx, world), student_in(dbx, world)
    login_as(client, 'registrar')
    create(client, a, world['t1'])
    create(client, b, world['t1'])
    page = client.get('/admin/enrollment-checkin/?q=' + student_no(dbx, a)).get_data(as_text=True)
    assert ('/admin/enrollment-review/%d?' % a['applicant_id']) in page
    assert ('/admin/enrollment-review/%d?' % b['applicant_id']) not in page


# ---------------------------------------------------------------------------
# Checklist Approvals page
# ---------------------------------------------------------------------------

@pytest.fixture()
def accounting_line(dbx):
    """A manual line approved by Accounting, like the "123" line created on the settings page."""
    dbx.x("INSERT INTO enrollment_checklist_requirements (code, label, kind, approver_roles, applies_to, is_required, is_active, sort_order) "
          "VALUES ('acct_docs_test', 'Promissory note on file', 'manual', 'accounting', 'all', 1, 1, 35) "
          "ON DUPLICATE KEY UPDATE is_active = 1")
    rid = dbx.one("SELECT id FROM enrollment_checklist_requirements WHERE code='acct_docs_test'")['id']
    yield rid
    dbx.x("UPDATE enrollment_checklist_requirements SET is_active=0 WHERE id=%s", (rid,))


def test_accounting_approves_its_checklist_line(app, client, dbx, world, fees, accounting_line):
    s = finalized(client, dbx, world)
    login_as(client, 'registrar')
    page = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert 'Promissory note on file' in page and 'Checklist Approvals' in page        # registrar sees who and where
    login_as(client, 'accounting', name='Acct One')
    page = client.get('/admin/enrollment-checkin/approvals').get_data(as_text=True)
    assert 'Enrollment Assessments' in page                                          # accounting's own sidebar
    assert ('appr-%d-acct_docs_test' % s['applicant_id']) in page and 'Promissory note on file' in page
    client.post('/admin/enrollment-checkin/%d/items/acct_docs_test/approve' % s['applicant_id'],
                data={'term_id': world['t1'], 'notes': 'PN signed'})
    assert 'approved' in flashes(client)
    page = client.get('/admin/enrollment-checkin/approvals').get_data(as_text=True)
    assert ('appr-%d-acct_docs_test' % s['applicant_id']) not in page                 # no longer waiting
    page = client.get('/admin/enrollment-checkin/approvals?show=all&q=' + student_no(dbx, s)).get_data(as_text=True)
    assert ('appr-%d-acct_docs_test' % s['applicant_id']) in page and 'Revoke' in page and 'PN signed' in page
    row = dbx.one("SELECT i.status, i.approved_by, i.approved_role FROM enrollment_checklist_items i WHERE i.requirement_id=%s AND i.student_user_id=%s",
                  (accounting_line, s['student_user_id']))
    assert row == {'status': 'approved', 'approved_by': 'Acct One', 'approved_role': 'accounting'}
    # the registrar's checklist now shows it as met
    login_as(client, 'registrar')
    page = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    line = re.search(r'<li[^>]*id="chk-acct_docs_test".*?</li>', page, re.S).group(0)
    assert '<span class="sr-only">met</span>' in line


def test_approvals_page_per_role(app, client, dbx, world, fees, accounting_line):
    s = finalized(client, dbx, world)
    login_as(client, 'cashier')
    page = client.get('/admin/enrollment-checkin/approvals').get_data(as_text=True)
    assert 'No active checklist line is approved by Cashier' in page
    client.post('/admin/enrollment-checkin/%d/items/acct_docs_test/approve' % s['applicant_id'], data={'term_id': world['t1']})
    assert 'Only accounting' in flashes(client)
    login_as(client, 'registrar')
    page = client.get('/admin/enrollment-checkin/approvals').get_data(as_text=True)
    assert ('appr-%d-documents' % s['applicant_id']) in page and 'acct_docs_test' not in page
    for role in ('guidance', 'osa', 'secretary'):
        login_as(client, role)
        assert client.get('/admin/enrollment-checkin/approvals').status_code == 200
    with client.session_transaction() as sess:
        sess.clear()
    login_student(client, s['student_user_id'])
    assert client.get('/admin/enrollment-checkin/approvals').status_code == 302


# ---------------------------------------------------------------------------
# the down payment
# ---------------------------------------------------------------------------

def test_minimum_down_payment_shown_to_cashier_and_student(app, client, dbx, world, fees):
    from website.finance import service as finance
    s = finalized(client, dbx, world)
    login_as(client, 'accounting', name='Acct One')
    client.post('/accounting/enrollment-finance/policy', data={'mode': 'minimum', 'min_down_payment': '1,500.00', 'require_verification': '1'})
    req = run(app, lambda: finance.enrollment_requirement(s['student_user_id'], world['t1']))
    assert req['required'] == Decimal('1500.00') and req['still_to_pay'] == Decimal('1500.00') and not req['ok']
    login_as(client, 'cashier', name='Cash One')
    page = client.get('/cashier/payment/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'Needed for enrollment: Minimum down payment' in page and '1,500.00' in page
    dbx.x("UPDATE applicants SET application_status='Enrolling' WHERE applicant_id=%s", (s['applicant_id'],))
    login_student(client, s['student_user_id'])
    page = client.get('/application-status').get_data(as_text=True)
    assert 'Pay at least' in page and '1,500.00' in page
    # pay 1,000: still 500 to go (payment not yet verified -> nothing counted)
    tuition = dbx.one("SELECT id FROM assessments WHERE student_id=%s ORDER BY id LIMIT 1", (s['applicant_id'],))['id']
    pid = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
                "VALUES (%s,%s,1000,CURDATE(),'Cash','Cash One')", (s['applicant_id'], tuition))
    req = run(app, lambda: finance.enrollment_requirement(s['student_user_id'], world['t1']))
    assert req['still_to_pay'] == Decimal('1500.00') and 'verified' in req['message']
    run(app, lambda: finance.verify_payment(A(*ACCT), pid))
    req = run(app, lambda: finance.enrollment_requirement(s['student_user_id'], world['t1']))
    assert req['still_to_pay'] == Decimal('500.00') and req['paid'] == Decimal('1000.00')
    pid2 = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
                 "VALUES (%s,%s,500,CURDATE(),'Cash','Cash One')", (s['applicant_id'], tuition))
    run(app, lambda: finance.verify_payment(A(*ACCT), pid2))
    req = run(app, lambda: finance.enrollment_requirement(s['student_user_id'], world['t1']))
    assert req['ok'] and req['still_to_pay'] == 0
    login_student(client, s['student_user_id'])
    assert 'Down payment of' in client.get('/application-status').get_data(as_text=True)
    # once checked in, the box disappears
    dbx.x("UPDATE enrollment_checklist_requirements SET is_active=0 WHERE code='documents'")
    try:
        assert check_in_json(client, s, world).get_json()['success']
    finally:
        dbx.x("UPDATE enrollment_checklist_requirements SET is_active=1 WHERE code='documents'")
    dbx.x("UPDATE applicants SET application_status='Enrolling' WHERE applicant_id=%s", (s['applicant_id'],))
    login_student(client, s['student_user_id'])
    assert 'id="enrollment-payment"' not in client.get('/application-status').get_data(as_text=True)


def test_policy_form_explains_each_mode(client):
    login_as(client, 'accounting')
    page = client.get('/accounting/enrollment-finance/').get_data(as_text=True)
    assert 'must pay at least the amount below' in page and 'officially enroll them' in page
