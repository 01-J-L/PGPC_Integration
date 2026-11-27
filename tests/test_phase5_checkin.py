"""Phase 5: approval checklist, registrar check-in, undo, and the routes that used to bypass it."""

import json

import pytest

from conftest import login_as
from test_phase3_regform import world, student_in, create, flashes                 # noqa: F401  (fixtures)
from test_phase4_finance import fees, finalized, run, A, reset_policy             # noqa: F401  (fixtures)

REG = ('Reg One', 'registrar')
ACCT = ('Acct One', 'accounting')


def svc():
    from website.checkin import service
    return service


def approve(app, s, world, code, who=REG, notes=None):
    return run(app, lambda: svc().approve_item(A(*who), s['applicant_id'], world['t1'], code, notes).id)


def ready_student(app, client, dbx, world):
    """Finalized form + documents reviewed + accounting clearance."""
    s = finalized(client, dbx, world)
    approve(app, s, world, 'documents')
    approve(app, s, world, 'payment', who=ACCT)
    return s


def state(app, s, world, role='registrar'):
    from website.models import Applicant
    from website import db

    def f():
        cl = svc().checklist(db.session.get(Applicant, s['applicant_id']), world['t1'], role)
        return {'ready': cl['ready'], 'blockers': cl['blockers'],
                'items': {i['req'].code: (i['met'], i['can_act']) for i in cl['items']},
                'checked_in': cl['checkin'] is not None}
    return run(app, f)


def app_row(dbx, s):
    return dbx.one("SELECT application_status, old_student_id, section_id FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))


def check_in_json(client, s, world, role='registrar', section=None):
    login_as(client, role, name='Reg One')
    return client.post('/admin/enrollment/%d/action' % s['applicant_id'],
                       json={'action': 'approve', 'term_id': world['t1'], 'manual_section_id': section})


# ---------------------------------------------------------------------------
# defaults and the read model
# ---------------------------------------------------------------------------

def test_default_checklist_is_seeded(dbx):
    rows = {r['code']: r for r in dbx.q("SELECT * FROM enrollment_checklist_requirements")}
    assert {'regform', 'documents', 'payment', 'dean', 'president'} <= set(rows)
    assert rows['regform']['kind'] == 'regform' and rows['payment']['kind'] == 'payment'
    assert rows['documents']['is_required'] == 1 and rows['documents']['is_active'] == 1
    assert rows['dean']['is_active'] == 0 and rows['president']['is_active'] == 0


def test_checklist_state_and_who_may_act(app, client, dbx, world, fees):
    s = student_in(dbx, world, section='1A')
    st = state(app, s, world)
    assert st['ready'] is False and st['items']['regform'] == (False, False)
    assert st['blockers'] == ['Registration form finalized', 'Enrollment documents reviewed', 'Accounting / enrollment payment policy']
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    st = state(app, s, world)
    assert st['items'] == {'regform': (True, False), 'documents': (False, True), 'payment': (False, False)}
    assert state(app, s, world, 'accounting')['items']['payment'] == (False, True)
    assert state(app, s, world, 'cashier')['items']['documents'] == (False, False)


# ---------------------------------------------------------------------------
# approving / revoking lines
# ---------------------------------------------------------------------------

def test_item_permissions_and_revoke_history(app, client, dbx, world, fees):
    CheckinError = svc().CheckinError
    s = finalized(client, dbx, world)
    with pytest.raises(CheckinError, match='Only registrar or admin'):
        approve(app, s, world, 'documents', who=('Cash', 'cashier'))
    with pytest.raises(CheckinError, match='Only accounting or admin'):
        approve(app, s, world, 'payment', who=REG)
    with pytest.raises(CheckinError, match='met automatically'):
        approve(app, s, world, 'regform', who=REG)
    with pytest.raises(CheckinError, match='Unknown or inactive'):
        approve(app, s, world, 'dean', who=('Adm', 'admin'))
    approve(app, s, world, 'documents', notes='Form 138 and PSA seen')
    with pytest.raises(CheckinError, match='already approved'):
        approve(app, s, world, 'documents')
    approve(app, s, world, 'payment', who=ACCT)
    rec = dbx.one("SELECT is_approved_by_accounting FROM student_enrollment_records WHERE student_user_id=%s AND academic_term_id=%s",
                  (s['student_user_id'], world['t1']))
    assert rec['is_approved_by_accounting'] == 1
    assert state(app, s, world)['ready'] is True

    with pytest.raises(CheckinError, match='reason'):
        run(app, lambda: svc().revoke_item(A(*REG), s['applicant_id'], world['t1'], 'documents', ' '))
    run(app, lambda: svc().revoke_item(A(*REG), s['applicant_id'], world['t1'], 'documents', 'PSA is a photocopy'))
    run(app, lambda: svc().revoke_item(A(*ACCT), s['applicant_id'], world['t1'], 'payment', 'cheque bounced'))
    st = state(app, s, world)
    assert st['ready'] is False and st['items']['documents'][0] is False
    assert dbx.one("SELECT is_approved_by_accounting v FROM student_enrollment_records WHERE student_user_id=%s AND academic_term_id=%s",
                   (s['student_user_id'], world['t1']))['v'] == 0
    log = [r['action'] for r in dbx.q("SELECT action FROM enrollment_audit_log WHERE applicant_id=%s ORDER BY id", (s['applicant_id'],))]
    assert log == ['approve_item', 'approve_item', 'revoke_item', 'revoke_item']
    row = dbx.one("SELECT i.status, i.revoke_reason, i.revoked_by FROM enrollment_checklist_items i JOIN enrollment_checklist_requirements r "
                  "ON r.id=i.requirement_id WHERE r.code='documents' AND i.student_user_id=%s", (s['student_user_id'],))
    assert row == {'status': 'revoked', 'revoke_reason': 'PSA is a photocopy', 'revoked_by': 'Reg One'}


def test_payment_line_follows_policy_when_not_clearance(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    run(app, lambda: __import__('website.finance.service', fromlist=['x']).save_policy(A(*ACCT), 'partial', None, False))
    with pytest.raises(svc().CheckinError, match='not "Accounting clearance"'):
        approve(app, s, world, 'payment', who=ACCT)
    assert state(app, s, world)['items']['payment'][0] is False
    tuition = dbx.one("SELECT id FROM assessments WHERE student_id=%s ORDER BY id LIMIT 1", (s['applicant_id'],))['id']
    dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
          "VALUES (%s,%s,100,CURDATE(),'Cash','c')", (s['applicant_id'], tuition))
    assert state(app, s, world)['items']['payment'][0] is True


def test_item_routes(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    login_as(client, 'cashier')
    client.post('/admin/enrollment-checkin/%d/items/documents/approve' % s['applicant_id'], data={'term_id': world['t1']})
    assert 'Only registrar or admin' in flashes(client)
    login_as(client, 'accounting', name='Acct One')
    client.post('/admin/enrollment-checkin/%d/items/payment/approve' % s['applicant_id'], data={'term_id': world['t1']})
    assert 'approved' in flashes(client)
    # accounting list shows the clearance as given
    page = client.get('/accounting/enrollment-finance/?term_id=%d' % world['t1']).get_data(as_text=True)
    assert 'Withdraw clearance' in page
    client.post('/admin/enrollment-checkin/%d/items/payment/revoke' % s['applicant_id'], data={'term_id': world['t1']})
    assert 'reason' in flashes(client)
    client.post('/admin/enrollment-checkin/%d/items/payment/revoke' % s['applicant_id'], data={'term_id': world['t1'], 'reason': 'mistake'})
    assert 'revoked' in flashes(client)
    # a student session cannot reach the endpoints
    with client.session_transaction() as sess:
        sess.clear()
        sess['student_id'] = s['student_user_id']
    r = client.post('/admin/enrollment-checkin/%d/items/documents/approve' % s['applicant_id'], data={'term_id': world['t1']})
    assert r.status_code == 302
    assert not dbx.one("SELECT 1 x FROM enrollment_checklist_items i JOIN enrollment_checklist_requirements r ON r.id=i.requirement_id "
                       "WHERE r.code='documents' AND i.student_user_id=%s", (s['student_user_id'],))


# ---------------------------------------------------------------------------
# check-in
# ---------------------------------------------------------------------------

def test_check_in_is_refused_until_the_checklist_is_complete(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)
    r = check_in_json(client, s, world)
    assert r.status_code == 400 and 'Still needed: Enrollment documents reviewed; Accounting' in r.get_json()['message']
    approve(app, s, world, 'documents')
    r = check_in_json(client, s, world)
    assert r.status_code == 400 and 'Accounting' in r.get_json()['message']
    assert app_row(dbx, s)['application_status'] == 'Enrolling'
    assert not dbx.one("SELECT id FROM enrollment_checkins WHERE student_user_id=%s", (s['student_user_id'],))
    # the button is disabled on the review page
    login_as(client, 'registrar')
    page = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert 'Complete the required checklist items first.' in page


def test_check_in_happy_path(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    charges = dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (s['applicant_id'],))['n']
    dbx.x("UPDATE applicants SET section_id=NULL WHERE applicant_id=%s", (s['applicant_id'],))
    r = check_in_json(client, s, world)
    assert r.status_code == 200 and r.get_json()['success'], r.get_data(as_text=True)
    row = app_row(dbx, s)
    assert row['application_status'] == 'Enrolled'
    assert row['old_student_id'].startswith('P')                                   # permanent ID for a new student
    assert row['section_id'] == world['sections']['1A']                            # block section of the registration form
    assert 'New ID: %s' % row['old_student_id'] in r.get_json()['message']
    rec = dbx.one("SELECT enrollment_status FROM student_enrollment_records WHERE student_user_id=%s AND academic_term_id=%s",
                  (s['student_user_id'], world['t1']))
    assert rec['enrollment_status'] == 'Enrolled'
    assert dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (s['applicant_id'],))['n'] == charges   # nothing charged twice
    ci = dbx.one("SELECT * FROM enrollment_checkins WHERE student_user_id=%s", (s['student_user_id'],))
    assert ci['active'] == 1 and ci['checked_in_by'] == 'Reg One' and ci['checked_in_role'] == 'registrar'
    snap = {x['code']: x for x in json.loads(ci['checklist_snapshot'])}
    assert snap['documents']['met'] and snap['payment']['met'] and snap['_finance']['total_assessment'] == '4200.75'
    assert dbx.one("SELECT COUNT(*) n FROM enrollment_audit_log WHERE applicant_id=%s AND action='check_in'", (s['applicant_id'],))['n'] == 1
    # the page now shows who checked the student in, and the button is off
    page = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert 'Checked in by Reg One' in page and 'Already checked in for this term.' in page


def test_manual_section_choice_is_validated(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    r = check_in_json(client, s, world, section=999999)
    assert r.status_code == 400 and 'section' in r.get_json()['message']
    r = check_in_json(client, s, world, section=world['sections']['1B'])
    assert r.get_json()['success']
    assert app_row(dbx, s)['section_id'] == world['sections']['1B']


def test_check_in_guards(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    login_as(client, 'registrar')
    r = client.post('/admin/enrollment/%d/action' % s['applicant_id'], json={'action': 'approve', 'term_id': world['t2']})
    assert r.status_code == 400 and 'active term' in r.get_json()['message']
    login_as(client, 'cashier')
    r = client.post('/admin/enrollment/%d/action' % s['applicant_id'], json={'action': 'approve', 'term_id': world['t1']})
    assert r.status_code == 401
    with pytest.raises(svc().CheckinError, match='Only the registrar'):
        run(app, lambda: svc().check_in(A(*ACCT), s['applicant_id'], world['t1']))
    assert check_in_json(client, s, world).get_json()['success']
    r = check_in_json(client, s, world)
    assert r.status_code == 400 and 'already checked in' in r.get_json()['message']
    assert dbx.one("SELECT COUNT(*) n FROM enrollment_checkins WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 1
    # one active check-in per student and term is also enforced by the database
    import mysql.connector
    with pytest.raises(mysql.connector.IntegrityError):
        dbx.x("INSERT INTO enrollment_checkins (student_user_id, applicant_id, academic_term_id, active, checked_in_by, checked_in_role) "
              "VALUES (%s,%s,%s,1,'x','admin')", (s['student_user_id'], s['applicant_id'], world['t1']))
    p = student_in(dbx, world)
    dbx.x("UPDATE applicants SET application_status='Pending' WHERE applicant_id=%s", (p['applicant_id'],))
    with pytest.raises(svc().CheckinError, match='Only students who are enrolling'):
        run(app, lambda: svc().check_in(A(*REG), p['applicant_id'], world['t1']))


def test_after_check_in_everything_is_locked_until_undo(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    assert check_in_json(client, s, world).get_json()['success']
    with pytest.raises(svc().CheckinError, match='Undo the check-in first'):
        run(app, lambda: svc().revoke_item(A(*REG), s['applicant_id'], world['t1'], 'documents', 'x'))
    login_as(client, 'registrar')
    client.post('/admin/registration/forms/%d/reopen' % s['form_id'], data={'reason': 'add subject'})
    assert 'Undo the check-in' in flashes(client)
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (s['form_id'],))['status'] == 'finalized'
    r = client.post('/admin/enrollment/%d/action' % s['applicant_id'], json={'action': 'reject', 'term_id': world['t1'], 'reason': 'x'})
    assert r.status_code == 400 and 'Undo the check-in' in r.get_json()['message']


def test_undo_check_in_keeps_history_and_allows_a_new_check_in(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    assert check_in_json(client, s, world).get_json()['success']
    pid = app_row(dbx, s)['old_student_id']
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/enrollment-checkin/%d/undo' % s['applicant_id'], data={'term_id': world['t1']})
    assert 'reason' in flashes(client)
    client.post('/admin/enrollment-checkin/%d/undo' % s['applicant_id'], data={'term_id': world['t1'], 'reason': 'wrong student'})
    row = app_row(dbx, s)
    assert row['application_status'] == 'Enrolling' and row['old_student_id'] == pid      # the ID is kept
    ci = dbx.one("SELECT active, undone_by, undo_reason FROM enrollment_checkins WHERE student_user_id=%s", (s['student_user_id'],))
    assert ci == {'active': None, 'undone_by': 'Reg One', 'undo_reason': 'wrong student'}
    assert dbx.one("SELECT enrollment_status FROM student_enrollment_records WHERE student_user_id=%s AND academic_term_id=%s",
                   (s['student_user_id'], world['t1']))['enrollment_status'] == 'Enrolling'
    page = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert 'Earlier check-ins (1)' in page and 'wrong student' in page
    assert check_in_json(client, s, world).get_json()['success']
    rows = dbx.q("SELECT active FROM enrollment_checkins WHERE student_user_id=%s ORDER BY id", (s['student_user_id'],))
    assert [r['active'] for r in rows] == [None, 1]
    assert app_row(dbx, s)['old_student_id'] == pid                                     # no second ID


# ---------------------------------------------------------------------------
# the old bypass routes
# ---------------------------------------------------------------------------

def test_status_dropdown_and_bulk_update_go_through_check_in(app, client, dbx, world, fees):
    ready = ready_student(app, client, dbx, world)
    not_ready = finalized(client, dbx, world)
    login_as(client, 'registrar')
    r = client.post('/admin/application/%d/status' % not_ready['applicant_id'], data={'status': 'Enrolled'})
    assert r.status_code == 400 and 'Still needed' in r.get_json()['message']
    assert app_row(dbx, not_ready)['application_status'] == 'Enrolling'
    r = client.post('/admin/applications/bulk-update-status',
                    json={'ids': [ready['applicant_id'], not_ready['applicant_id']], 'action': 'Enrolled', 'type': 'status'})
    body = r.get_json()
    assert body['success'] and '1 student(s) checked in' in body['message'] and len(body['failed']) == 1
    assert app_row(dbx, ready)['application_status'] == 'Enrolled'
    assert app_row(dbx, not_ready)['application_status'] == 'Enrolling'
    assert dbx.one("SELECT active FROM enrollment_checkins WHERE student_user_id=%s", (ready['student_user_id'],))['active'] == 1
    # other statuses still work as before
    r = client.post('/admin/application/%d/status' % not_ready['applicant_id'], data={'status': 'Dropped'})
    assert r.get_json()['success'] and app_row(dbx, not_ready)['application_status'] == 'Dropped'


def test_legacy_toggle_endpoint_goes_through_the_checklist(client, dbx, world, fees):
    s = finalized(client, dbx, world)
    login_as(client, 'registrar')
    r = client.post('/admin/enrollment/%d/toggle-approval' % s['applicant_id'], json={'role': 'accounting', 'status': True, 'term_id': world['t1']})
    assert r.status_code == 400
    login_as(client, 'accounting')
    r = client.post('/admin/enrollment/%d/toggle-approval' % s['applicant_id'], json={'role': 'accounting', 'status': True, 'term_id': world['t1']})
    assert r.status_code == 200
    r = client.post('/admin/enrollment/%d/toggle-approval' % s['applicant_id'], json={'role': 'accounting', 'status': False, 'term_id': world['t1']})
    assert r.status_code == 400 and 'reason' in r.get_json()['message']


# ---------------------------------------------------------------------------
# configuration and screens
# ---------------------------------------------------------------------------

def test_admin_configures_checklist(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    login_as(client, 'registrar')
    r = client.post('/admin/enrollment-checkin/requirements', data={'label': 'X', 'approver_roles': ['registrar']})
    assert not dbx.one("SELECT id FROM enrollment_checklist_requirements WHERE label='X'")
    login_as(client, 'admin', name='Adm')
    label = 'Guidance interview ' + world['code']
    client.post('/admin/enrollment-checkin/requirements', data={'label': label, 'approver_roles': ['guidance'], 'applies_to': 'new',
                                                                'is_required': '1', 'is_active': '1', 'sort_order': '25'})
    req = dbx.one("SELECT * FROM enrollment_checklist_requirements WHERE label=%s", (label,))
    try:
        assert req and req['kind'] == 'manual' and req['approver_roles'] == 'guidance'
        st = state(app, s, world, 'guidance')
        assert st['ready'] is False and label in st['blockers'] and st['items'][req['code']] == (False, True)
        approve(app, s, world, req['code'], who=('Gui', 'guidance'))
        assert state(app, s, world)['ready'] is True
        # existing students are not affected by a 'new only' line
        dbx.x("UPDATE applicants SET enrollment_student_type='Existing' WHERE applicant_id=%s", (s['applicant_id'],))
        assert req['code'] not in state(app, s, world)['items']
        dbx.x("UPDATE applicants SET enrollment_student_type='New' WHERE applicant_id=%s", (s['applicant_id'],))
        # optional lines never block; inactive lines disappear
        client.post('/admin/enrollment-checkin/requirements', data={'id': req['id'], 'label': label, 'approver_roles': ['guidance'],
                                                                    'applies_to': 'new', 'is_active': '1', 'sort_order': '25'})
        run(app, lambda: svc().revoke_item(A('Gui', 'guidance'), s['applicant_id'], world['t1'], req['code'], 'redo'))
        assert state(app, s, world)['ready'] is True
        client.post('/admin/enrollment-checkin/requirements', data={'id': req['id'], 'label': label, 'approver_roles': [], 'applies_to': 'new'})
        assert 'at least one role' in flashes(client)
        assert dbx.one("SELECT COUNT(*) n FROM enrollment_audit_log WHERE action='requirement'")['n'] >= 2
    finally:
        dbx.x("UPDATE enrollment_checklist_requirements SET is_active=0 WHERE id=%s", (req['id'],))


def test_queue_page(app, client, dbx, world, fees):
    ready = ready_student(app, client, dbx, world)
    pending = finalized(client, dbx, world)
    login_as(client, 'registrar')
    def ids(page):
        import re
        return set(int(x) for x in re.findall(r'/admin/enrollment-review/(\d+)\?', page))
    page = client.get('/admin/enrollment-checkin/?state=ready').get_data(as_text=True)
    assert ready['applicant_id'] in ids(page) and pending['applicant_id'] not in ids(page)
    page = client.get('/admin/enrollment-checkin/?state=pending').get_data(as_text=True)
    assert pending['applicant_id'] in ids(page) and 'Enrollment documents reviewed' in page
    client.post('/admin/enrollment-checkin/%d/check-in' % ready['applicant_id'], data={'term_id': world['t1']})
    assert 'checked in and officially enrolled' in flashes(client)
    page = client.get('/admin/enrollment-checkin/?state=checked_in').get_data(as_text=True)
    assert ready['applicant_id'] in ids(page)
    # still 'Enrolled' from last term: listed once a registration form for this term exists
    old_student = student_in(dbx, world, section='1A')
    dbx.x("UPDATE applicants SET application_status='Enrolled' WHERE applicant_id=%s", (old_student['applicant_id'],))
    assert old_student['applicant_id'] not in ids(client.get('/admin/enrollment-checkin/').get_data(as_text=True))
    create(client, old_student, world['t1'])
    assert old_student['applicant_id'] in ids(client.get('/admin/enrollment-checkin/?state=pending').get_data(as_text=True))
    login_as(client, 'cashier')
    assert client.get('/admin/enrollment-checkin/').status_code == 302
