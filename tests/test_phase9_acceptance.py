"""Phase 9: end-to-end acceptance run through the real HTTP routes, and the
regression for the enrollment-form gate found while writing it.

New student account -> application -> Approved -> Passed -> enrollment form ->
registration form -> assessment -> payment -> verification -> checklist ->
check-in with PGPC control number -> grades entered -> released -> student sees them.
"""

import io
import json
import re
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from conftest import login_as, _uniq
from test_phase3_regform import world, flashes                                   # noqa: F401  (fixtures)
from test_phase4_finance import fees, reset_policy                               # noqa: F401  (fixtures)

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF'
PASSWORD = 'Stud-Passw0rd!'


def new_account(dbx):
    email = _uniq('e2e') + '@example.com'
    suid = dbx.x("INSERT INTO student_users (email, password, is_verified, student_type) VALUES (%s,%s,1,'new')",
                 (email, generate_password_hash(PASSWORD)))
    return {'email': email, 'student_user_id': suid}


def student_login(client, acct):
    with client.session_transaction() as s:
        s.clear()
    r = client.post('/student-login', data={'email': acct['email'], 'password': PASSWORD})
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert s.get('student_id') == acct['student_user_id'], 'student login failed'


def application_form(world, last):
    files = {k: (io.BytesIO(PDF), k + '.pdf', 'application/pdf')
             for k in ('shs_diploma_file_input', 'shs_card_file_input', 'birth_certificate_file_input')}
    data = {
        'program_choice': world['title'], 'last_name': last, 'first_name': 'Ana', 'middle_name': 'Santos',
        'date_of_birth': '2006-03-14', 'place_of_birth': 'Padre Garcia', 'sex': 'Female', 'civil_status': 'Single',
        'religion': 'Roman Catholic', 'citizenship': 'Filipino', 'mobile_number': '09171234567', 'email_address': 'ana@example.com',
        'permanent_address_street_barangay': 'Brgy. Poblacion', 'permanent_address_city_municipality': 'Padre Garcia',
        'permanent_address_province': 'Batangas', 'permanent_address_postal_code': '4224',
        'cultural_minority_group': 'None', 'physical_disability': 'No',
        'date_of_application': '2026-06-01', 'academic_year': '2026-2027', 'average_family_income': 'below_10000',
        'guardian_name': 'Maria Reyes', 'guardian_occupation': 'Vendor', 'guardian_company_address': 'Padre Garcia',
        'guardian_contact_number': '09181234567', 'senior_high_school': 'PGNHS', 'senior_high_school_address': 'Padre Garcia',
        'senior_high_school_track_strand': 'STEM', 'senior_high_school_year_from': '2022', 'senior_high_school_year_to': '2024',
        'agreements': 'Yes', 'final_submission_date': '2026-06-01', 'student_type': 'new',
    }
    data.update(files)
    return data


def app_row(dbx, aid):
    return dbx.one("SELECT application_status, old_student_id, control_number, section_id, enrollment_year_level FROM applicants WHERE applicant_id=%s", (aid,))


def audit_actions(dbx, suid):
    return [r['action'] for r in dbx.q("SELECT action FROM enrollment_audit_log WHERE student_user_id=%s ORDER BY id", (suid,))]


# ---------------------------------------------------------------------------
# the gate on POST /submit-enrollment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('status', ['Pending', 'Approved', 'Rejected', 'Failed', 'Enrolled', 'Dropped'])
def test_enrollment_form_post_refuses_applications_that_are_not_ready(app, client, dbx, world, fees, status):
    """Regression: the GET form checked the status but the POST did not, so any applicant
    could post it and become 'Enrolling' with a permanent student ID and fees."""
    from conftest import make_student
    s = make_student(dbx, {'title': world['title']}, status=status)
    with client.session_transaction() as sess:
        sess['student_id'] = s['student_user_id']
    before = dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (s['applicant_id'],))['n']
    before_id = app_row(dbx, s['applicant_id'])['old_student_id']
    client.post('/submit-enrollment/%d' % s['applicant_id'],
                data={'year_level': '1st Year', 'semester': '1st Semester', 'program_choice': world['title']})
    assert 'not ready for enrollment' in flashes(client)
    row = app_row(dbx, s['applicant_id'])
    assert row['application_status'] == status and row['old_student_id'] == before_id and not before_id.startswith('P')
    assert dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (s['applicant_id'],))['n'] == before


# ---------------------------------------------------------------------------
# the whole flow
# ---------------------------------------------------------------------------

def test_full_enrollment_flow(app, client, dbx, world, fees):
    acct = new_account(dbx)
    last = 'Reyes' + _uniq('x')[-6:]

    # 1. the student applies
    student_login(client, acct)
    r = client.post('/submit_application', data=application_form(world, last), content_type='multipart/form-data')
    assert r.status_code == 302, flashes(client)
    a = dbx.one("SELECT applicant_id, application_status, control_number FROM applicants WHERE student_user_id=%s", (acct['student_user_id'],))
    assert a and a['application_status'] == 'Pending', flashes(client)
    aid = a['applicant_id']
    # not ready yet: the enrollment form is refused (GET and POST)
    client.post('/submit-enrollment/%d' % aid, data={'year_level': '1st Year', 'semester': '1st Semester', 'program_choice': world['title']})
    assert app_row(dbx, aid)['application_status'] == 'Pending'

    # 2. the registrar approves (permit) and records the passed exam
    login_as(client, 'registrar', name='Reg One')
    for status in ('Approved', 'Passed'):
        r = client.post('/admin/application/%d/status' % aid, data={'status': status})
        assert r.get_json()['success'], r.get_data(as_text=True)
    assert app_row(dbx, aid)['application_status'] == 'Passed'
    # 'Enrolled' cannot be set from the status menu before check-in
    r = client.post('/admin/application/%d/status' % aid, data={'status': 'Enrolled'})
    assert r.status_code == 400

    # 3. the student fills in the enrollment form
    student_login(client, acct)
    assert client.get('/enrollment-form/%d' % aid).status_code == 200
    client.post('/submit-enrollment/%d' % aid, data={
        'year_level': '1st Year', 'semester': '1st Semester', 'program_choice': world['title'],
        'section_id': world['sections']['1A'], 'gender': 'Female', 'age': '20', 'mobile_number': '09171234567'})
    row = app_row(dbx, aid)
    assert row['application_status'] == 'Enrolling' and row['old_student_id'].startswith('P'), flashes(client)
    student_no = row['old_student_id']
    legacy_charges = dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (aid,))['n']

    # 4. the registrar prepares and finalizes the registration form
    login_as(client, 'registrar', name='Reg One')
    r = client.post('/admin/registration/student/%d/create' % aid, data={'term_id': world['t1']})
    fid = int(re.search(r'/forms/(\d+)', r.location).group(1))
    assert dbx.one("SELECT COUNT(*) n FROM registration_form_subjects WHERE registration_form_id=%s", (fid,))['n'] > 0
    client.post('/admin/registration/forms/%d/finalize' % fid)
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'finalized', flashes(client)

    # 5. the assessment: the fee rules, never charged twice (the form step had already charged)
    ea = dbx.one("SELECT id, total_assessment FROM enrollment_assessments WHERE registration_form_id=%s", (fid,))
    assert ea['total_assessment'] == Decimal('4200.75')
    charges = dbx.q("SELECT id, balance FROM assessments WHERE student_id=%s", (aid,))
    assert len(charges) == max(legacy_charges, 2) == 2

    # 6. Accounting requires full, verified payment; the cashier collects; another accountant verifies
    login_as(client, 'accounting', name='Acct One')
    client.post('/accounting/enrollment-finance/policy', data={'mode': 'full', 'require_verification': '1'})
    login_as(client, 'cashier', name='Cash One')
    for ch in charges:
        page = client.get('/cashier/payment/%d' % aid).get_data(as_text=True)
        tok = re.search(r'name="submission_token" value="([0-9a-f]{32})"', page).group(1)
        client.post('/cashier/payment/%d' % aid, data={'assessment_id': ch['id'], 'amount': str(ch['balance']),
                                                      'payment_date': '2026-06-10', 'payment_method': 'Cash',
                                                      'submission_token': tok, 'receipt_number': _uniq('OR')[:30]})
    pids = [r['id'] for r in dbx.q("SELECT id FROM payments WHERE student_id=%s ORDER BY id", (aid,))]
    assert len(pids) == 2
    login_as(client, 'registrar', name='Reg One')
    r = check_in(client, aid, world)
    assert r.status_code == 400 and 'Accounting' in r.get_json()['message']          # payments not verified yet
    login_as(client, 'accounting', name='Acct One')
    for pid in pids:
        client.post('/accounting/enrollment-finance/payments/%d/verify' % pid)
    assert dbx.one("SELECT COUNT(*) n FROM payment_verifications WHERE payment_id IN (%s,%s)" % tuple(pids))['n'] == 2

    # 7. the checklist and the check-in
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/enrollment-checkin/%d/items/documents/approve' % aid, data={'term_id': world['t1']})
    r = check_in(client, aid, world)
    body = r.get_json()
    assert body['success'], body
    number = re.search(r'Control No\. (PGPC-26-\d{4,})', body['message']).group(1)
    row = app_row(dbx, aid)
    assert row['application_status'] == 'Enrolled' and row['old_student_id'] == student_no
    assert dbx.one("SELECT control_number FROM registration_forms WHERE id=%s", (fid,))['control_number'] == number
    snap = json.loads(dbx.one("SELECT checklist_snapshot FROM enrollment_checkins WHERE student_user_id=%s AND active=1",
                              (acct['student_user_id'],))['checklist_snapshot'])
    fin = next(x for x in snap if x['code'] == '_finance')
    assert fin['remaining'] in ('0.00', '0') and fin['control_number'] == number
    assert dbx.one("SELECT COUNT(*) n FROM assessments WHERE student_id=%s", (aid,))['n'] == 2              # still no double charge

    # 8. the student sees the REGFORM with the control number
    student_login(client, acct)
    assert client.get('/my-registration').location.endswith('/my-registration/%d' % fid)
    page = client.get('/my-registration/%d' % fid).get_data(as_text=True)
    assert number in page and student_no in page

    # 9. grades: entered by the registrar, hidden until released, then visible
    login_as(client, 'registrar', name='Reg One')
    subj = world['legacy']['A 101']
    r = client.post('/admin/grades/%d/save' % acct['student_user_id'], json={'grades': [
        {'subject_id': subj, 'grade': 1.75, 'percentage': 90, 'remarks': 'Passed', 'academic_year': '2026-2027', 'semester': '1st Semester'}]})
    assert r.get_json()['success']
    code = dbx.one("SELECT subject_code FROM subjects WHERE id=%s", (subj,))['subject_code']
    student_login(client, acct)
    page = client.get('/my-grades').get_data(as_text=True)
    assert code not in page and 'not yet released' in page
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/grade-release/release', data={'academic_year': '2026-2027', 'semester': '1st Semester', 'action': 'release'})
    student_login(client, acct)
    page = client.get('/my-grades').get_data(as_text=True)
    assert code in page and '1.75' in page

    # 10. the trail
    acts = audit_actions(dbx, acct['student_user_id'])
    for expected in ('approve_item', 'control_number', 'check_in'):
        assert expected in acts, acts
    fin_acts = [r['action'] for r in dbx.q("SELECT action FROM finance_audit_log WHERE applicant_id=%s ORDER BY id", (aid,))]
    assert fin_acts.count('record_payment') == 2 and fin_acts.count('verify_payment') == 2, fin_acts


def check_in(client, aid, world):
    return client.post('/admin/enrollment/%d/action' % aid, json={'action': 'approve', 'term_id': world['t1']})


# ---------------------------------------------------------------------------
# rollback guards added in Phase 9 (no silent loss of uploaded documents)
# ---------------------------------------------------------------------------

def test_rollback_guards_protect_uploaded_documents(dbx, world):
    from website.schema_migrations import MIGRATIONS
    guards = {m['id']: m['rollback_guard'] for m in MIGRATIONS if m['id'].startswith(('0001', '0002'))}
    base1, base2 = dbx.one(guards['0001_enrollment_shs_document_columns'])['COUNT(*)'], \
        dbx.one(guards['0002_document_columns_mediumblob'])['COUNT(*)']
    from conftest import make_student
    s = make_student(dbx, {'title': world['title']})
    dbx.x("UPDATE applicants SET enrollment_shs_card_file=%s WHERE applicant_id=%s", (b'x' * 10, s['applicant_id']))
    assert dbx.one(guards['0001_enrollment_shs_document_columns'])['COUNT(*)'] == base1 + 1     # 0001 would drop it
    dbx.x("UPDATE applicants SET shs_card_file=%s WHERE applicant_id=%s", (b'x' * 70000, s['applicant_id']))
    assert dbx.one(guards['0002_document_columns_mediumblob'])['COUNT(*)'] == base2 + 1     # 0002 would truncate it
