"""Phase 9: permission sweep over every route added in phases 2-8.

Every POST route of the new blueprints is called as an anonymous visitor, as a
logged-in student and as an unrelated staff role (OSA). Each call must be
refused (redirect to a login / page, 400/401/403/404) without a server error,
and no table may change. Every staff GET page must refuse anonymous visitors
and students.
"""

import re

import pytest
from werkzeug.routing import IntegerConverter

from conftest import make_student

NEW_BLUEPRINTS = ('curriculum', 'registration', 'finance', 'checkin', 'grade_release')
WATCHED_TABLES = ['applicants', 'assessments', 'payments', 'student_grades', 'sections', 'system_settings',
                  'curriculum_versions', 'curriculum_subjects', 'student_curricula', 'registration_forms',
                  'registration_form_subjects', 'enrollment_assessments', 'payment_verifications', 'payment_reversals',
                  'enrollment_checklist_requirements', 'enrollment_checklist_items', 'enrollment_checkins',
                  'control_numbers', 'control_number_sequences', 'grade_releases', 'grade_holds', 'section_archives',
                  'student_enrollment_records']


def _urls(app, methods):
    out = []
    for rule in app.url_map.iter_rules():
        bp = rule.endpoint.split('.')[0]
        if bp not in NEW_BLUEPRINTS or not (rule.methods & methods):
            continue
        args = {}
        for name, conv in rule._converters.items():
            args[name] = 1 if isinstance(conv, IntegerConverter) else 'payment'
        with app.test_request_context():
            from flask import url_for
            out.append((rule.endpoint, url_for(rule.endpoint, **args)))
    return sorted(set(out))


def _checksums(dbx):
    rows = dbx.q("CHECKSUM TABLE " + ', '.join(WATCHED_TABLES))
    return {r['Table'].split('.')[-1]: r['Checksum'] for r in rows}


def _session(client, who, student_id):
    with client.session_transaction() as s:
        s.clear()
        if who == 'student':
            s['student_id'] = student_id
        elif who == 'osa':
            s.update(user_role='osa', user_name='OSA Staff', user_id=1)


@pytest.mark.parametrize('who', ['anonymous', 'student', 'osa'])
def test_post_routes_refuse_unauthorised_callers(app, client, dbx, program, who):
    st = make_student(dbx, program)
    urls = _urls(app, {'POST'})
    assert len(urls) > 30, urls                       # the sweep really covers the new modules
    before = _checksums(dbx)
    problems = []
    for endpoint, url in urls:
        _session(client, who, st['student_user_id'])
        r = client.post(url, data={'reason': 'x', 'term_id': 1, 'mode': 'full', 'action': 'release',
                                   'academic_year': '2026-2027', 'semester': '1st Semester', 'unpaid_rule': '1',
                                   'label': 'x', 'approver_roles': ['osa'], 'next_number': '5', 'school_year': '2026-2027'})
        if r.status_code >= 500 or r.status_code == 200:
            problems.append((endpoint, url, r.status_code))
        elif r.status_code == 302 and who != 'osa':
            loc = r.location or ''
            if not re.search(r'login|/admin$|/admin/?$|student-login|application-status|/$', loc):
                problems.append((endpoint, url, 'redirect to ' + loc))
    after = _checksums(dbx)
    changed = [t for t in WATCHED_TABLES if before[t] != after[t]]
    assert not problems, problems
    assert not changed, changed


@pytest.mark.parametrize('who', ['anonymous', 'student'])
def test_staff_pages_refuse_visitors_and_students(app, client, dbx, program, who):
    st = make_student(dbx, program)
    leaked = []
    for endpoint, url in _urls(app, {'GET'}):
        _session(client, who, st['student_user_id'])
        r = client.get(url)
        if r.status_code == 200:
            leaked.append((endpoint, url))
        assert r.status_code < 500, (endpoint, url, r.status_code)
    assert not leaked, leaked
