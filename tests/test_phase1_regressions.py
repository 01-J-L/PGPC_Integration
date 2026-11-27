"""Phase 1 regression tests.

Each test reproduces one bug found during the Phase 1 audit (IDs match the
audit report, e.g. P1-01). They failed on the uploaded code and must pass
after the fixes.
"""

import io
import re

import pytest

from conftest import login_as, login_student, make_student


# ---------------------------------------------------------------------------
# P1-01  CSRF protection missing on every admission/registrar/cashier form
# ---------------------------------------------------------------------------

def test_p1_01_post_without_csrf_token_is_rejected(csrf_app, dbx, program, student):
    client = csrf_app.test_client()
    login_as(client, 'accounting')
    before = dbx.one("SELECT COUNT(*) AS n FROM chart_of_accounts")['n']
    resp = client.post('/accounting/add-account',
                       data={'account_code': '9999', 'account_name': 'X', 'type': 'Asset'})
    after = dbx.one("SELECT COUNT(*) AS n FROM chart_of_accounts")['n']
    assert after == before, "a cross-site POST without a token must not change data"
    assert resp.status_code in (400, 302)


def test_p1_01_json_post_without_csrf_header_is_rejected(csrf_app, dbx, program, student, active_term):
    client = csrf_app.test_client()
    login_as(client, 'registrar')
    resp = client.post('/admin/enrollment/%d/toggle-approval' % student['applicant_id'],
                       json={'role': 'accounting', 'status': True, 'term_id': active_term['id']})
    assert resp.status_code == 400
    assert resp.is_json and resp.get_json().get('success') is False


def test_p1_01_pages_carry_a_csrf_token_and_header_shim(csrf_app, program, student, active_term):
    client = csrf_app.test_client()
    login_as(client, 'registrar')
    html = client.get('/admin/enrollment-review/%d' % student['applicant_id']).get_data(as_text=True)
    assert 'name="csrf-token"' in html
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    resp = client.post('/admin/enrollment/%d/toggle-approval' % student['applicant_id'],
                       json={'role': 'accounting', 'status': True, 'term_id': active_term['id']},
                       headers={'X-CSRFToken': token})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_p1_01_student_login_page_still_works_with_csrf(csrf_app, dbx, program, student):
    client = csrf_app.test_client()
    html = client.get('/student-login').get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    resp = client.post('/student-login', data={'email': student['email'], 'password': 'Student123!',
                                                 'csrf_token': token})
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert s.get('student_id') == student['student_user_id']


# ---------------------------------------------------------------------------
# P1-02  Deleting a program from the admission admin wiped historical grades
# ---------------------------------------------------------------------------

def test_p1_02_program_delete_keeps_historical_grades(client, dbx, program, student):
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) "
          "VALUES (%s, %s, 1.50, 'Passed', '2025-2026', '1st Semester')",
          (student['student_user_id'], program['subjects'][0]))
    login_as(client, 'registrar')
    client.post('/admin/program/delete/%s' % program['id'])
    assert dbx.one("SELECT COUNT(*) n FROM student_grades WHERE subject_id=%s", (program['subjects'][0],))['n'] == 1
    assert dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (program['id'],)) is not None


def test_p1_02_empty_program_can_still_be_deleted(client, dbx):
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES ('zz1','Empty','ZZ1','Empty')")
    login_as(client, 'admin')
    client.post('/admin/program/delete/zz1')
    assert dbx.one("SELECT program_id FROM programs WHERE program_id='zz1'") is None


# ---------------------------------------------------------------------------
# P1-03  Registrar upload / view of Form 138 and SHS Diploma crashed
#        (columns enrollment_shs_card_* / enrollment_shs_diploma_* missing)
# ---------------------------------------------------------------------------

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF'


@pytest.mark.parametrize('doc_type,view_key', [('shs_card', 'enrollment_card'), ('shs_diploma', 'enrollment_diploma')])
def test_p1_03_registrar_can_upload_and_view_shs_documents(client, dbx, student, doc_type, view_key):
    login_as(client, 'registrar')
    resp = client.post('/admin/enrollment/%d/upload-doc' % student['applicant_id'],
                       data={'doc_type': doc_type, 'file': (io.BytesIO(PDF), 'form.pdf')},
                       content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['success'] is True
    view = client.get('/applicant-document/%d/%s' % (student['applicant_id'], view_key))
    assert view.status_code == 200
    assert view.data.startswith(b'%PDF')


# ---------------------------------------------------------------------------
# P1-04  Chart of accounts add/delete raised BuildError (url_for('chart_of_accounts'))
# ---------------------------------------------------------------------------

def test_p1_04_add_and_delete_account_redirects(client, dbx):
    login_as(client, 'accounting')
    resp = client.post('/accounting/add-account', data={'account_code': '1010', 'account_name': 'Cash', 'type': 'Asset'})
    assert resp.status_code == 302 and resp.location.endswith('/accounting/chart-of-accounts')
    acc = dbx.one("SELECT id FROM chart_of_accounts WHERE account_code='1010'")
    resp = client.post('/accounting/delete-account/%d' % acc['id'])
    assert resp.status_code == 302 and resp.location.endswith('/accounting/chart-of-accounts')


# ---------------------------------------------------------------------------
# P1-05  Review page showed "Accounting approved" for students with no payment
#        (uncommitted working-copy change; display-only, threshold default 0)
# ---------------------------------------------------------------------------

def test_p1_05_accounting_box_not_ticked_without_payment(client, dbx, program, student, active_term):
    dbx.x("INSERT INTO student_enrollment_records (student_user_id, academic_term_id, enrollment_status) "
          "VALUES (%s, %s, 'Enrolling')", (student['student_user_id'], active_term['id']))
    login_as(client, 'registrar')
    html = client.get('/admin/enrollment-review/%d?term_id=%d' % (student['applicant_id'], active_term['id'])).get_data(as_text=True)
    box = re.search(r'<input type="checkbox" data-role="accounting"[^>]*>', html, re.S).group(0)
    assert 'checked' not in box


def test_p1_05_accounting_box_reflects_the_stored_approval(client, dbx, program, student, active_term):
    dbx.x("INSERT INTO student_enrollment_records (student_user_id, academic_term_id, enrollment_status, "
          "is_approved_by_accounting, accounting_approval_date) VALUES (%s, %s, 'Enrolling', 1, NOW())",
          (student['student_user_id'], active_term['id']))
    login_as(client, 'registrar')
    html = client.get('/admin/enrollment-review/%d?term_id=%d' % (student['applicant_id'], active_term['id'])).get_data(as_text=True)
    box = re.search(r'<input type="checkbox" data-role="accounting"[^>]*>', html, re.S).group(0)
    assert 'checked' in box


# ---------------------------------------------------------------------------
# P1-06  Approving an enrollment left no per-term enrollment record
# ---------------------------------------------------------------------------

def test_p1_06_approve_creates_term_record(client, dbx, program, student, active_term):
    login_as(client, 'registrar')
    resp = client.post('/admin/enrollment/%d/action' % student['applicant_id'],
                       json={'action': 'approve', 'term_id': active_term['id']})
    assert resp.get_json()['success'] is True, resp.get_data(as_text=True)
    rec = dbx.one("SELECT * FROM student_enrollment_records WHERE student_user_id=%s AND academic_term_id=%s",
                  (student['student_user_id'], active_term['id']))
    assert rec is not None and rec['enrollment_status'] == 'Enrolled'
    assert rec['year_level'] == '1st Year'


# ---------------------------------------------------------------------------
# P1-07  Fee / student-ID helpers swallowed errors, so a half-done enrollment
#        was committed (student "Enrolled" without fees or permanent ID).
# ---------------------------------------------------------------------------

def test_p1_07_fee_failure_rolls_back_the_approval(client, dbx, program, student, active_term, monkeypatch):
    import website.auth as auth_mod

    def boom(*a, **k):
        raise RuntimeError('simulated fee failure')
    monkeypatch.setattr(auth_mod, '_auto_assign_fees', boom)
    login_as(client, 'registrar')
    resp = client.post('/admin/enrollment/%d/action' % student['applicant_id'],
                       json={'action': 'approve', 'term_id': active_term['id']})
    assert resp.status_code == 500
    row = dbx.one("SELECT application_status, old_student_id FROM applicants WHERE applicant_id=%s", (student['applicant_id'],))
    assert row['application_status'] == 'Enrolling'
    assert not str(row['old_student_id']).startswith('P')


def test_p1_07_fee_helper_propagates_sql_errors(app):
    import website.auth as auth_mod

    class BadCursor:
        def execute(self, *a, **k):
            raise RuntimeError('db down')
    with pytest.raises(RuntimeError):
        auth_mod._auto_assign_fees(BadCursor(), 1, {})


# ---------------------------------------------------------------------------
# P1-08  First-deployment admin bootstrap failed: users.email is NOT NULL
# ---------------------------------------------------------------------------

def test_p1_08_bootstrap_admin_login(client, dbx):
    dbx.x("DELETE FROM users WHERE username='admin'")
    resp = client.post('/admin_login', data={'username': 'admin', 'password': 'AdminPass123'})
    assert resp.status_code == 302
    assert dbx.one("SELECT id FROM users WHERE username='admin' AND role='admin'") is not None
    with client.session_transaction() as s:
        assert s.get('user_role') == 'admin'


def test_p1_08_login_with_null_password_hash_does_not_crash(client, dbx):
    dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`) "
          "VALUES ('nohash', 'nohash@example.com', NULL, 'registrar', 'active', 'No Hash', 'admission')")
    resp = client.post('/admin_login', data={'username': 'nohash', 'password': 'whatever'})
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert s.get('user_role') is None


# ---------------------------------------------------------------------------
# P1-09  /enrollment-form/<id> was registered twice (views + auth)
# ---------------------------------------------------------------------------

def test_p1_09_no_duplicate_url_rules(app):
    seen = {}
    dupes = []
    for rule in app.url_map.iter_rules():
        key = (rule.rule, tuple(sorted(rule.methods - {'HEAD', 'OPTIONS'})))
        if key in seen:
            dupes.append((rule.rule, seen[key], rule.endpoint))
        seen[key] = rule.endpoint
    assert not dupes, dupes


def test_p1_09_eligible_student_can_open_enrollment_form(client, dbx, program):
    s = make_student(dbx, program, status='Eligible for Enrollment', student_type='old', control='P2025')
    login_student(client, s['student_user_id'])
    resp = client.get('/enrollment-form/%d' % s['applicant_id'])
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# P1-10  Every url_for() target in code and templates must exist
# ---------------------------------------------------------------------------

def test_p1_10_all_url_for_targets_exist(app):
    import os
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    pat = re.compile(r"url_for\(\s*['\"]([A-Za-z_][\w\.]*)['\"]")
    missing = []
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'website')
    for d, _, files in os.walk(base):
        for f in files:
            if f.endswith(('.py', '.html')):
                p = os.path.join(d, f)
                for m in pat.finditer(open(p, encoding='utf-8', errors='ignore').read()):
                    if m.group(1) not in endpoints and m.group(1) != 'static':
                        missing.append((os.path.relpath(p, base), m.group(1)))
    assert not missing, missing


# ---------------------------------------------------------------------------
# P1-12  Document columns were BLOB (64 KB max) although uploads allow 5 MB,
#        so ordinary scanned PDFs / photos failed with "Data too long".
# ---------------------------------------------------------------------------

def test_p1_12_registrar_can_upload_a_one_megabyte_document(client, dbx, student):
    big = PDF + b'0' * (1024 * 1024)
    login_as(client, 'registrar')
    resp = client.post('/admin/enrollment/%d/upload-doc' % student['applicant_id'],
                       data={'doc_type': 'good_moral', 'file': (io.BytesIO(big), 'scan.pdf')},
                       content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    row = dbx.one("SELECT LENGTH(enrollment_good_moral_file) AS n FROM applicants WHERE applicant_id=%s",
                  (student['applicant_id'],))
    assert row['n'] == len(big)


def test_p1_12_all_document_columns_are_at_least_mediumblob(dbx):
    rows = dbx.q("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                 "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'applicants' AND DATA_TYPE LIKE '%%blob'")
    assert rows
    small = [r['COLUMN_NAME'] for r in rows if r['DATA_TYPE'] in ('blob', 'tinyblob')]
    assert not small, small


# ---------------------------------------------------------------------------
# Migrations: an old database + migrations == a fresh database from full.sql
# ---------------------------------------------------------------------------

def test_migrations_are_idempotent_and_recorded(dbx):
    import mysql.connector
    from conftest import _raw_conn, DB_NAME
    from website.schema_migrations import apply_all, pending, MIGRATIONS
    conn = _raw_conn(DB_NAME)
    assert pending(conn) == []
    assert apply_all(conn, log=lambda *_: None) == []
    conn.close()
    ids = {r['id'] for r in dbx.q("SELECT id FROM schema_migrations")}
    assert ids == {m['id'] for m in MIGRATIONS}


def test_fresh_full_sql_matches_migrated_schema(dbx):
    from conftest import load_sql_file, ROOT, DB_NAME
    import os
    fresh = 'pgpc_fresh_test'
    load_sql_file(os.path.join(ROOT, 'full.sql'), fresh)

    def columns(schema):
        return {(r['TABLE_NAME'], r['COLUMN_NAME'], r['COLUMN_TYPE'], r['IS_NULLABLE'])
                for r in dbx.q("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                               "WHERE TABLE_SCHEMA = %s", (schema,))}
    try:
        assert columns(fresh) == columns(DB_NAME)
        assert {r['id'] for r in dbx.q("SELECT id FROM `%s`.schema_migrations" % fresh)} == \
               {r['id'] for r in dbx.q("SELECT id FROM schema_migrations")}
    finally:
        dbx.x("DROP DATABASE IF EXISTS `%s`" % fresh)


# ---------------------------------------------------------------------------
# P1-13  Printing a receipt crashed until a cashier had saved a receipt layout
# ---------------------------------------------------------------------------

def test_p1_13_receipt_prints_without_saved_layout(client, dbx, student):
    dbx.x("DELETE FROM receipt_layout_settings")
    pay = dbx.x("INSERT INTO payments (student_id, amount_paid, payment_date, payment_method, cashier_username) "
                "VALUES (%s, 500, CURDATE(), 'Cash', 'tester')", (student['applicant_id'],))
    login_as(client, 'cashier')
    resp = client.get('/cashier/print-receipt/%d' % pay)
    assert resp.status_code == 200
    assert 'Five Hundred Pesos' in resp.get_data(as_text=True)


def test_p1_13_unknown_receipt_redirects(client):
    login_as(client, 'cashier')
    assert client.get('/cashier/print-receipt/999999').status_code == 302


# ---------------------------------------------------------------------------
# P1-14  Dean "Add Subject" screen crashed ('form' is undefined)
# P1-15  Grade view crashed for students without a section
# ---------------------------------------------------------------------------

def _login_dean(client, dbx):
    from werkzeug.security import generate_password_hash
    dbx.x("DELETE FROM users WHERE username='p1dean'")
    uid = dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`, department) "
                "VALUES ('p1dean','p1dean@example.com',%s,'dean','active','P1 Dean','faculty','CCBA')",
                (generate_password_hash('x'),))
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return uid


def test_p1_14_dean_can_add_subject_and_duplicates_are_refused(client, dbx, program):
    _login_dean(client, dbx)
    url = '/faculty/grading-admin/programs/%s/add_subject' % program['id']
    assert client.get(url).status_code == 200
    data = {'code': 'p1x101', 'units': '3', 'description': 'P1 Subject', 'year': '1', 'semester': '2'}
    resp = client.post(url, data=data)
    assert resp.status_code == 302
    row = dbx.one("SELECT * FROM subjects WHERE subject_code='P1X101'")
    assert row and row['semester'] == 2 and row['program_id'] == program['id']
    resp = client.post(url, data=data)
    assert resp.status_code == 200 and 'already exists' in resp.get_data(as_text=True)
    assert dbx.one("SELECT COUNT(*) n FROM subjects WHERE subject_code='P1X101'")['n'] == 1


def test_p1_15_grade_view_for_unsectioned_student(client, dbx, program, student):
    dbx.x("UPDATE applicants SET section_id=NULL WHERE applicant_id=%s", (student['applicant_id'],))
    _login_dean(client, dbx)
    for suffix in ('', '/print'):
        resp = client.get('/faculty/grading-admin/student/%d/grades%s' % (student['applicant_id'], suffix))
        assert resp.status_code == 200
        assert 'CC10' + program['id'] in resp.get_data(as_text=True) or suffix == '/print'


# ---------------------------------------------------------------------------
# Known issue carried into Phase 2 (documented, not fixed in Phase 1)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="P2: subjects.subject_code is globally UNIQUE, so a GE subject "
                                       "shared by BSCS and BSMA cannot exist in both curricula")
def test_known_issue_shared_ge_code_across_programs(dbx):
    dbx.x("INSERT IGNORE INTO programs (program_id, title, program_code, program_name) VALUES ('kia','KIA','KIA','KIA'),('kib','KIB','KIB','KIB')")
    dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) VALUES ('GE105KI','Purposive Communication','kia',3,2,1)")
    dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) VALUES ('GE105KI','Purposive Communication','kib',3,2,1)")
