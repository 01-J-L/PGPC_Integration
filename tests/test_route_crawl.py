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
    values = {
        'applicant_id': st['applicant_id'], 'student_id': st['applicant_id'], 'student_id_db': st['applicant_id'],
        'student_user_id': st['student_user_id'], 'program_id': pid, 'subject_id': sid, 'section_id': sec,
        'term_id': term['id'], 'payment_id': pay, 'user_id': dean, 'room_id': room, 'semester': 1,
        'section_name': '1A', 'year': '1st Year', 'id': 1, 'export_type': 'all', 'doc_type': 'card',
        'filename': 'none.pdf', 'token': 'bad-token', 'batch_id': 1, 'assignment_id': 1, 'report_type': 'prelim',
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
