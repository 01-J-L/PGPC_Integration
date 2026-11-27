"""Phase 7: grade visibility (release per term / program, student holds, optional unpaid-balance rule)."""

import pytest

from conftest import login_as, login_student, make_student, _uniq
from test_phase3_regform import world, student_in, flashes                       # noqa: F401  (fixtures)
from test_phase4_finance import fees, finalized, run, A, reset_policy           # noqa: F401  (fixtures)

AY, SEM = '2026-2027', '1st Semester'
OLD_AY, OLD_SEM = '2024-2025', '2nd Semester'
REG = ('Reg One', 'registrar')


def gv():
    from website.grades import visibility
    return visibility


@pytest.fixture(autouse=True)
def clean_visibility(dbx):
    yield
    dbx.x("DELETE FROM grade_releases WHERE academic_year IN (%s, %s)", (AY, OLD_AY))
    dbx.x("UPDATE grade_holds SET lifted_at = NOW(), lifted_by = 'test cleanup', lift_reason = 'cleanup' WHERE lifted_at IS NULL")
    dbx.x("DELETE FROM system_settings WHERE setting_key = 'grade_hold_unpaid_balance'")


def add_grade(dbx, s, world, ay=AY, sem=SEM, code='A 101', grade='1.75'):
    subj = world['legacy'][code]
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) VALUES (%s,%s,%s,'Passed',%s,%s)",
          (s['student_user_id'], subj, grade, ay, sem))
    return dbx.one("SELECT subject_code FROM subjects WHERE id=%s", (subj,))['subject_code']


def release(app, released=True, program=None, reason=None, ay=AY, sem=SEM):
    return run(app, lambda: gv().set_release(A(*REG), ay, sem, program, released, reason).id)


def my_grades(client, s):
    login_student(client, s['student_user_id'])
    r = client.get('/my-grades')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def dashboard_grades(app, client, s):
    """current_term_grades handed to the dashboard template (it is computed but not displayed today)."""
    from flask import template_rendered
    seen = []
    def rec(sender, template, context, **extra):
        if 'current_term_grades' in context:
            seen.append(context['current_term_grades'])
    login_student(client, s['student_user_id'])
    with template_rendered.connected_to(rec, app):
        client.get('/application-status')
    assert seen, 'dashboard not rendered'
    return seen[-1]


def print_card(client, s):
    login_student(client, s['student_user_id'])
    return client.get('/my-grades/print-card').get_data(as_text=True)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------

def test_unreleased_term_is_hidden_everywhere_for_the_student(app, client, dbx, world):
    s = student_in(dbx, world)
    code = add_grade(dbx, s, world)
    page = my_grades(client, s)
    assert 'Grades for this term are not yet released.' in page and code not in page and '1.75' not in page
    assert code not in print_card(client, s)
    login_student(client, s['student_user_id'])
    dbx.x("UPDATE applicants SET application_status='Enrolled' WHERE applicant_id=%s", (s['applicant_id'],))
    assert code not in client.get('/application-status').get_data(as_text=True)
    # staff always see everything
    login_as(client, 'registrar')
    assert code in client.get('/admin/grades/%d/print-card' % s['student_user_id']).get_data(as_text=True)


def test_release_and_withhold(app, client, dbx, world):
    s = student_in(dbx, world)
    code = add_grade(dbx, s, world)
    release(app)
    page = my_grades(client, s)
    assert code in page and '1.75' in page and 'not yet released' not in page
    assert code in print_card(client, s)
    dbx.x("UPDATE applicants SET application_status='Enrolled' WHERE applicant_id=%s", (s['applicant_id'],))
    assert [g['subject_code'] for g in dashboard_grades(app, client, s)] == [code]   # dashboard context (not displayed today)
    with pytest.raises(gv().GradeVisibilityError, match='reason'):
        release(app, released=False)
    release(app, released=False, reason='Faculty correcting GE grades')
    page = my_grades(client, s)
    assert code not in page and 'not yet released' in page
    assert 'Faculty correcting' not in page                                   # internal reason never shown
    assert dashboard_grades(app, client, s) == []
    actions = [r['action'] for r in dbx.q("SELECT action FROM enrollment_audit_log WHERE action LIKE 'grade_%%' ORDER BY id DESC LIMIT 2")]
    assert actions == ['grade_withhold', 'grade_release']


def test_terms_are_independent(app, client, dbx, world):
    s = student_in(dbx, world)
    new_code = add_grade(dbx, s, world, code='A 101')
    old_code = add_grade(dbx, s, world, ay=OLD_AY, sem=OLD_SEM, code='A 102', grade='2.00')
    release(app, ay=OLD_AY, sem=OLD_SEM)
    page = my_grades(client, s)
    assert old_code in page and '2.00' in page and new_code not in page
    assert 'not yet released' in page


def test_program_override(app, client, dbx, world):
    s = student_in(dbx, world)
    code = add_grade(dbx, s, world)
    other_title = 'Other Program ' + _uniq('P')
    other_pid = _uniq('op').lower()[:40]
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,%s)", (other_pid, other_title, other_pid.upper()[:10], other_title))
    o = make_student(dbx, {'title': other_title})
    add_grade(dbx, o, world)
    release(app)                                                             # all programs
    release(app, released=False, program=world['pid'], reason='not yet checked')
    assert code not in my_grades(client, s)
    assert code in my_grades(client, o)
    release(app, released=False, reason='hold everything')                   # all programs withheld ...
    release(app, released=True, program=world['pid'])                        # ... but this program released
    assert code in my_grades(client, s) and code not in my_grades(client, o)
    with pytest.raises(gv().GradeVisibilityError, match='Unknown program'):
        release(app, program='no-such-program')


# ---------------------------------------------------------------------------
# holds
# ---------------------------------------------------------------------------

def test_holds(app, client, dbx, world):
    s = student_in(dbx, world)
    new_code = add_grade(dbx, s, world)
    old_code = add_grade(dbx, s, world, ay=OLD_AY, sem=OLD_SEM, code='A 102')
    release(app)
    release(app, ay=OLD_AY, sem=OLD_SEM)
    E = gv().GradeVisibilityError
    with pytest.raises(E, match='reason'):
        run(app, lambda: gv().place_hold(A(*REG), s['student_user_id'], AY, SEM, ''))
    with pytest.raises(E, match='both'):
        run(app, lambda: gv().place_hold(A(*REG), s['student_user_id'], AY, '', 'x'))
    with pytest.raises(E, match='Only the registrar'):
        run(app, lambda: gv().place_hold(A('C', 'cashier'), s['student_user_id'], AY, SEM, 'x'))
    hid = run(app, lambda: gv().place_hold(A(*REG), s['student_user_id'], AY, SEM, 'Library book not returned').id)
    page = my_grades(client, s)
    assert new_code not in page and old_code in page and 'on hold' in page and 'Library' not in page
    all_id = run(app, lambda: gv().place_hold(A(*REG), s['student_user_id'], '', '', 'Clearance incomplete').id)
    page = my_grades(client, s)
    assert new_code not in page and old_code not in page
    with pytest.raises(E, match='reason'):
        run(app, lambda: gv().lift_hold(A(*REG), hid, ' '))
    run(app, lambda: gv().lift_hold(A(*REG), hid, 'returned'))
    run(app, lambda: gv().lift_hold(A(*REG), all_id, 'cleared'))
    with pytest.raises(E, match='already lifted'):
        run(app, lambda: gv().lift_hold(A(*REG), hid, 'again'))
    page = my_grades(client, s)
    assert new_code in page and old_code in page
    rows = dbx.q("SELECT lifted_by, lift_reason FROM grade_holds WHERE student_user_id=%s ORDER BY id", (s['student_user_id'],))
    assert [r['lift_reason'] for r in rows] == ['returned', 'cleared']                # kept, not deleted


def test_hold_routes_and_student_lookup(client, dbx, world):
    s = student_in(dbx, world)
    student_no = dbx.one("SELECT control_number FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['control_number']
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/grade-release/holds', data={'student': 'nobody-here', 'term': '', 'reason': 'x'})
    assert 'No student found' in flashes(client)
    client.post('/admin/grade-release/holds', data={'student': s['email'], 'term': '%s|%s' % (AY, SEM), 'reason': 'Unreturned ID'})
    assert 'Hold placed' in flashes(client)
    h = dbx.one("SELECT * FROM grade_holds WHERE student_user_id=%s AND lifted_at IS NULL", (s['student_user_id'],))
    assert h['academic_year'] == AY and h['semester'] == SEM and h['placed_by'] == 'Reg One'
    page = client.get('/admin/grade-release/').get_data(as_text=True)
    assert 'Unreturned ID' in page
    client.post('/admin/grade-release/holds/%d/lift' % h['id'], data={'reason': 'returned'})
    assert 'Hold lifted' in flashes(client)
    assert student_no                                                          # lookup by number covered in service test below


def test_find_student(app, dbx, world):
    s = student_in(dbx, world)
    no = dbx.one("SELECT control_number FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['control_number']
    assert run(app, lambda: gv().find_student(s['email'])) == s['student_user_id']
    assert run(app, lambda: gv().find_student(str(s['applicant_id']))) == s['student_user_id']
    assert run(app, lambda: gv().find_student(no)) is not None
    assert run(app, lambda: gv().find_student('')) is None


# ---------------------------------------------------------------------------
# unpaid-balance rule
# ---------------------------------------------------------------------------

def test_unpaid_balance_rule_is_off_by_default_and_admin_only(app, client, dbx, world, fees):
    s = finalized(client, dbx, world)                       # assessment of 4,200.75 for 2026-2027 1st Semester
    code = add_grade(dbx, s, world)
    release(app)
    assert code in my_grades(client, s)                     # off: balance does not matter
    login_as(client, 'registrar')
    client.post('/admin/grade-release/settings', data={'unpaid_rule': '1'})
    assert 'Only an administrator' in flashes(client)
    login_as(client, 'admin')
    client.post('/admin/grade-release/settings', data={'unpaid_rule': '1'})
    assert 'withheld' in flashes(client)
    page = my_grades(client, s)
    assert code not in page and 'account balance' in page
    # paying everything shows the grades again
    for a in dbx.q("SELECT id, balance FROM assessments WHERE student_id=%s", (s['applicant_id'],)):
        dbx.x("INSERT INTO payments (student_id, assessment_id, amount_paid, payment_date, payment_method, cashier_username) "
              "VALUES (%s,%s,%s,CURDATE(),'Cash','c')", (s['applicant_id'], a['id'], a['balance']))
    assert code in my_grades(client, s)
    # a term without any assessment is never withheld by this rule
    other = student_in(dbx, world)
    other_code = add_grade(dbx, other, world)
    assert other_code in my_grades(client, other)


# ---------------------------------------------------------------------------
# screens, permissions, migration seed
# ---------------------------------------------------------------------------

def test_release_page_and_permissions(app, client, dbx, world):
    s = student_in(dbx, world)
    add_grade(dbx, s, world)
    login_as(client, 'registrar', name='Reg One')
    page = client.get('/admin/grade-release/').get_data(as_text=True)
    assert '2026-2027 - 1st Semester' in page and 'Not released' in page
    client.post('/admin/grade-release/release', data={'academic_year': AY, 'semester': SEM, 'program_id': '', 'action': 'release'})
    assert 'visible to students' in flashes(client)
    client.post('/admin/grade-release/release', data={'academic_year': AY, 'semester': SEM, 'program_id': '', 'action': 'withhold'})
    assert 'reason' in flashes(client)
    for role in ('cashier', 'accounting', 'guidance'):
        login_as(client, role)
        assert client.get('/admin/grade-release/').status_code == 302
        client.post('/admin/grade-release/release', data={'academic_year': AY, 'semester': SEM, 'action': 'withhold', 'reason': 'x'})
    assert dbx.one("SELECT is_released FROM grade_releases WHERE academic_year=%s AND semester=%s AND program_key=''", (AY, SEM))['is_released'] == 1
    login_student(client, s['student_user_id'])
    assert client.get('/admin/grade-release/').status_code == 302


def test_migration_seed_keeps_existing_terms_visible(app, dbx, world):
    from website.schema_migrations import GRADE_RELEASE_SEED_SQL
    s = student_in(dbx, world)
    add_grade(dbx, s, world, ay=OLD_AY, sem=OLD_SEM)
    dbx.x(GRADE_RELEASE_SEED_SQL)
    row = dbx.one("SELECT is_released, released_by FROM grade_releases WHERE academic_year=%s AND semester=%s AND program_key=''", (OLD_AY, OLD_SEM))
    assert row == {'is_released': 1, 'released_by': 'migration 0008'}
    dbx.x(GRADE_RELEASE_SEED_SQL)                            # idempotent
    assert dbx.one("SELECT COUNT(*) n FROM grade_releases WHERE academic_year=%s AND semester=%s", (OLD_AY, OLD_SEM))['n'] == 1


def test_grade_without_a_number_renders(app, client, dbx, world):
    """The page compared None > 3.0 and crashed on a row with no numeric grade (e.g. INC)."""
    s = student_in(dbx, world)
    subj = world['legacy']['A 103']
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) VALUES (%s,%s,NULL,'Incomplete',%s,%s)",
          (s['student_user_id'], subj, AY, SEM))
    release(app)
    page = my_grades(client, s)
    assert dbx.one("SELECT subject_code FROM subjects WHERE id=%s", (subj,))['subject_code'] in page and 'Incomplete' in page
