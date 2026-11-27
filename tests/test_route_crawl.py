"""Crawl every GET page as every role (Phase 1 audit + regression).

For each GET route the crawler fills URL arguments from seeded fixtures and
requests the page as admin / registrar / cashier / accounting / student /
dean / superadmin. It fails on:
  * any HTTP 500 (unhandled exception, BuildError, TemplateNotFound, SQL error)
  * any rendered page that contains a POST <form> but not the CSRF token meta
"""

import re

import pytest

from conftest import make_student

# GET routes that change data or end the session; never crawl them.
SKIP_ENDPOINTS = {
    'auth.logout', 'faculty_auth.logout', 'faculty_main.grading_logout',
    'faculty_main.accept_substitution_request', 'faculty_main.reset_time_slots',
    'auth.clear_filters', 'static',
}

ADMISSION_ROLES = ['admin', 'registrar', 'cashier', 'accounting']


@pytest.fixture(scope='module')
def seeded(app):
    from conftest import DB, _uniq
    dbx = DB()
    from werkzeug.security import generate_password_hash
    pid = 'crawl'
    dbx.x("DELETE FROM programs WHERE program_id=%s", (pid,))
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,'Crawl Program','CRAWL','Crawl Program')", (pid,))
    sid = dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) VALUES ('CRW101','Crawl 1',%s,3,1,1)", (pid,))
    sec = dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) VALUES (%s,'1st Year','1A','A',40,1,1,'2026-2027')", (pid,))
    program = {'id': pid, 'title': 'Crawl Program', 'subjects': [sid], 'section_id': sec}
    st = make_student(dbx, program, status='Enrolling')
    term = dbx.one("SELECT id FROM academic_terms ORDER BY id LIMIT 1")
    dean_email = 'crawl_dean@example.com'
    dbx.x("DELETE FROM users WHERE email IN (%s, 'crawl_super@example.com')", (dean_email,))
    dean = dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`, department) "
                 "VALUES ('crawl_dean', %s, %s, 'dean', 'active', 'Crawl Dean', 'faculty', 'CCBA')",
                 (dean_email, generate_password_hash('x')))
    sup = dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`) "
                "VALUES ('crawl_super', 'crawl_super@example.com', %s, 'superadmin', 'active', 'Crawl Super', 'admission')",
                (generate_password_hash('x'),))
    aid = dbx.x("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status) VALUES (%s,'Tuition',100,0,100,'unpaid')", (st['applicant_id'],))
    pay = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) VALUES (%s,%s,10,CURDATE(),'Cash','crawler')", (st['applicant_id'], aid))
    room = dbx.x("INSERT INTO room (room_name) VALUES (%s)", (_uniq('R'),))
    ver = dbx.x("INSERT INTO curriculum_versions (program_id, name, effective_academic_year, status) VALUES (%s,'Crawl curriculum','2026-2027','active')", (pid,))
    dbx.x("UPDATE curriculum_versions SET is_default_for_new=1 WHERE id=%s", (ver,))
    cs1 = dbx.x("INSERT INTO curriculum_subjects (curriculum_version_id, year_level, semester, subject_code, code_key, description, units) VALUES (%s,1,1,'CRW 101','CRW101','Crawl subject',3)", (ver,))
    cs2 = dbx.x("INSERT INTO curriculum_subjects (curriculum_version_id, year_level, semester, subject_code, code_key, description, units) VALUES (%s,1,2,'CRW 102','CRW102','Crawl subject 2',3)", (ver,))
    dbx.x("INSERT INTO curriculum_prerequisites (curriculum_subject_id, kind, prerequisite_subject_id) VALUES (%s,'subject',%s)", (cs2, cs1))
    form = dbx.x("INSERT INTO registration_forms (student_user_id, applicant_id, academic_term_id, curriculum_version_id, program_id, "
                 "year_level, semester, classification, status, max_units, total_units) VALUES (%s,%s,%s,%s,%s,1,1,'Irregular','draft',26,3)",
                 (st['student_user_id'], st['applicant_id'], term['id'], ver, pid))
    dbx.x("INSERT INTO registration_form_subjects (registration_form_id, curriculum_subject_id, subject_code, code_key, description, units, year_level, semester) "
          "VALUES (%s,%s,'CRW 101','CRW101','Crawl subject',3,1,1)", (form, cs1))
    # Phase 4: an assessment with one charge, a payment, a verification and a reversal
    ea = dbx.x("INSERT INTO enrollment_assessments (registration_form_id, student_user_id, applicant_id, academic_term_id, residency, total_units, total_assessment, generated_by, generated_at) "
               "VALUES (%s,%s,%s,%s,'resident',3,500,'crawl',NOW())", (form, st['student_user_id'], st['applicant_id'], term['id']))
    ch = dbx.x("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status, created_at) VALUES (%s,'Crawl fee',500,0,500,'unpaid',NOW())", (st['applicant_id'],))
    dbx.x("INSERT INTO enrollment_assessment_items (enrollment_assessment_id, assessment_id, description, amount, adopted) VALUES (%s,%s,'Crawl fee',500,0)", (ea, ch))
    p1 = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) VALUES (%s,%s,100,CURDATE(),'Cash','x')", (st['applicant_id'], ch))
    p2 = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) VALUES (%s,%s,50,CURDATE(),'Cash','x')", (st['applicant_id'], ch))
    p3 = dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) VALUES (%s,%s,-50,CURDATE(),'Reversal','y')", (st['applicant_id'], ch))
    dbx.x("INSERT INTO payment_verifications (payment_id, status, verified_by, verified_role, verified_at) VALUES (%s,'verified','v','accounting',NOW())", (p1,))
    dbx.x("INSERT INTO payment_reversals (payment_id, reversal_payment_id, reason, reversed_by, reversed_role, reversed_at) VALUES (%s,%s,'crawl','y','accounting',NOW())", (p2, p3))
    # Phase 5: an approved checklist line, an undone and an active check-in
    req = dbx.one("SELECT id FROM enrollment_checklist_requirements WHERE code='documents'")['id']
    dbx.x("INSERT INTO enrollment_checklist_items (student_user_id, academic_term_id, requirement_id, status, approved_by, approved_role, approved_at) "
          "VALUES (%s,%s,%s,'approved','crawl','registrar',NOW())", (st['student_user_id'], term['id'], req))
    dbx.x("INSERT INTO enrollment_checkins (student_user_id, applicant_id, academic_term_id, active, checklist_snapshot, checked_in_by, checked_in_role, "
          "checked_in_at, undone_by, undone_role, undone_at, undo_reason) VALUES (%s,%s,%s,NULL,'[]','crawl','registrar',NOW(),'crawl','admin',NOW(),'crawl test')",
          (st['student_user_id'], st['applicant_id'], term['id']))
    dbx.x("INSERT INTO enrollment_checkins (student_user_id, applicant_id, academic_term_id, active, checklist_snapshot, checked_in_by, checked_in_role, checked_in_at) "
          "VALUES (%s,%s,%s,1,'[]','crawl','registrar',NOW())", (st['student_user_id'], st['applicant_id'], term['id']))
    import json
    batch = dbx.x("INSERT INTO curriculum_import_batches (program_id, source_filename, source_kind, source_sha256, payload) VALUES (%s,'crawl.xlsx','registration_form',%s,%s)",
                  (pid, 'x' * 64, json.dumps({'parsed': {'kind': 'registration_form', 'title': 'Crawl', 'level': 'First Year-1st sem',
                                                         'rows': [{'subject_code': 'CRW 101', 'description': 'Crawl subject', 'units': '3', 'prerequisite': '', 'year_level': 1, 'semester': 1},
                                                                  {'subject_code': 'CRW 103', 'description': 'New one', 'units': '', 'prerequisite': 'CRW 999', 'year_level': 1, 'semester': 1}]}, 'edits': {}})))
    values = {
        'applicant_id': st['applicant_id'], 'student_id': st['applicant_id'], 'student_id_db': st['applicant_id'],
        'student_user_id': st['student_user_id'], 'program_id': pid, 'subject_id': sid, 'section_id': sec,
        'term_id': term['id'], 'payment_id': pay, 'user_id': dean, 'room_id': room, 'semester': 1,
        'section_name': '1A', 'year': '1st Year', 'id': 1, 'export_type': 'all', 'doc_type': 'card',
        'filename': 'none.pdf', 'token': 'bad-token', 'batch_id': batch, 'version_id': ver, 'form_id': form, 'assignment_id': 1, 'report_type': 'prelim',
    }
    yield {'values': values, 'student_user_id': st['student_user_id'], 'dean': dean, 'super': sup}
    dbx.close()


def _urls(app, values):
    out = []
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.endpoint in SKIP_ENDPOINTS:
            continue
        args = {a: values[a] for a in rule.arguments if a in values}
        if set(rule.arguments) - set(args) - set(rule.defaults or {}):
            continue
        try:
            with app.test_request_context():
                from flask import url_for
                out.append((rule.endpoint, url_for(rule.endpoint, **args)))
        except Exception:
            continue
    return sorted(set(out))


def _as(client, *, role=None, student=None, faculty_user=None, superadmin=False):
    with client.session_transaction() as s:
        s.clear()
        if role:
            s.update(user_role=role, user_name='Crawler', user_id=1, can_edit_student=True)
        if superadmin:
            s['is_superadmin'] = True
        if student:
            s['student_id'] = student
        if faculty_user:
            s['_user_id'] = str(faculty_user)
            s['_fresh'] = True


POST_FORM = re.compile(r'<form[^>]*method=["\']post["\']', re.I)


def _crawl(app, client, values):
    problems = []
    for endpoint, url in _urls(app, values):
        try:
            resp = client.get(url)
        except Exception as e:  # TESTING=True propagates view exceptions
            problems.append((endpoint, url, '%s: %s' % (type(e).__name__, str(e)[:160])))
            continue
        if resp.status_code >= 500:
            problems.append((endpoint, url, resp.status_code))
            continue
        if resp.status_code == 200 and 'text/html' in (resp.content_type or ''):
            html = resp.get_data(as_text=True)
            if POST_FORM.search(html) and 'name="csrf-token"' not in html and 'name="csrf_token"' not in html:
                problems.append((endpoint, url, 'POST form without CSRF token'))
    return problems


@pytest.mark.parametrize('role', ADMISSION_ROLES)
def test_crawl_admission_roles(app, seeded, role):
    client = app.test_client()
    _as(client, role=role)
    problems = _crawl(app, client, seeded['values'])
    assert not problems, '\n'.join(map(str, problems))


def test_crawl_student(app, seeded):
    client = app.test_client()
    _as(client, student=seeded['student_user_id'])
    problems = _crawl(app, client, seeded['values'])
    assert not problems, '\n'.join(map(str, problems))


def test_crawl_dean(app, seeded):
    client = app.test_client()
    _as(client, faculty_user=seeded['dean'])
    problems = _crawl(app, client, seeded['values'])
    assert not problems, '\n'.join(map(str, problems))


def test_crawl_superadmin(app, seeded):
    client = app.test_client()
    _as(client, role='admin', superadmin=True, faculty_user=seeded['super'])
    problems = _crawl(app, client, seeded['values'])
    assert not problems, '\n'.join(map(str, problems))


def test_crawl_anonymous(app, seeded):
    client = app.test_client()
    problems = _crawl(app, client, seeded['values'])
    assert not problems, '\n'.join(map(str, problems))
