"""Phase 8: section deletion protection and archiving."""

import pytest
from werkzeug.security import generate_password_hash

from conftest import login_as, make_student, _uniq
from test_phase3_regform import world, student_in, create, flashes, schedule      # noqa: F401  (fixtures)
from test_phase4_finance import run, A, reset_policy, fees                       # noqa: F401  (fixtures)
from test_phase5_checkin import ready_student, check_in_json

REG = ('Reg One', 'registrar')


def prot():
    from website.sections import protection
    return protection


def new_section(dbx, world, name='1Z', year='1st Year', school_year='2026-2027', active=1):
    return dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
                 "VALUES (%s,%s,%s,%s,40,%s,1,%s)", (world['pid'], year, name, name[-1], active, school_year))


def exists(dbx, sid):
    return dbx.one("SELECT id FROM sections WHERE id=%s", (sid,)) is not None


def usage(app, sid):
    from website import db
    from website.models import Section
    return run(app, lambda: dict(prot().usage(db.session.get(Section, sid))))


def faculty(dbx, role='dean'):
    return dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`, department) "
                 "VALUES (%s,%s,%s,%s,'active','Dean Test','faculty','CCBA')",
                 (_uniq('dean'), _uniq('d') + '@example.com', generate_password_hash('x'), role))


def as_faculty(client, uid):
    with client.session_transaction() as s:
        s.clear()
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def registrar_post(client, data):
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/manage-sections', data=data)
    return flashes(client)


# ---------------------------------------------------------------------------
# what counts as "in use"
# ---------------------------------------------------------------------------

def test_usage_finds_every_kind_of_reference(app, client, dbx, world):
    sid = new_section(dbx, world, '1Y')
    assert usage(app, sid) == {}
    s = student_in(dbx, world)
    dbx.x("UPDATE applicants SET section_id=%s WHERE applicant_id=%s", (sid, s['applicant_id']))
    teacher = faculty(dbx, 'faculty')
    dbx.x("INSERT INTO faculty_assignments (faculty_id, subject_id, section_id) VALUES (%s,%s,%s)", (teacher, world['legacy']['A 101'], sid))
    schedule(dbx, world, 'A 101', '1Y', 'Mon', '08:00', '09:00')
    login_as(client, 'registrar')
    t = create(client, student_in(dbx, world), world['t1'])
    dbx.x("UPDATE registration_forms SET section_id=%s WHERE id=%s", (sid, t))
    dbx.x("UPDATE registration_form_subjects SET section_id=%s WHERE registration_form_id=%s LIMIT 1", (sid, t))
    u = usage(app, sid)
    assert u['student(s) assigned'] == 1 and u['faculty class record(s)'] == 1 and u['class schedule(s)'] == 1
    assert u['registration form(s)'] == 1 and u['registration-form subject line(s)'] == 1


def test_schedule_of_another_school_year_does_not_block(app, dbx, world):
    sid = new_section(dbx, world, '1X', school_year='2026-2027')
    schedule(dbx, world, 'A 101', '1X', 'Tue', '08:00', '09:00', year='2025-2026')
    assert usage(app, sid) == {}
    schedule(dbx, world, 'A 102', '1X', 'Tue', '09:00', '10:00', year='2026-2027')
    assert usage(app, sid) == {'class schedule(s)': 1}


def test_admin_style_section_name_matches_its_schedule(app, dbx, world):
    sid = new_section(dbx, world, world['code'].lower() + ' 1W')
    schedule(dbx, world, 'A 101', '1W', 'Wed', '08:00', '09:00')
    assert usage(app, sid) == {'class schedule(s)': 1}


# ---------------------------------------------------------------------------
# registrar: Manage Sections
# ---------------------------------------------------------------------------

def test_registrar_deletes_only_unused_sections(app, client, dbx, world):
    free = new_section(dbx, world, '1V')
    assert 'deleted' in registrar_post(client, {'action': 'delete_section', 'section_id': free})
    assert not exists(dbx, free)
    log = dbx.one("SELECT performed_by, details FROM enrollment_audit_log WHERE action='section_delete' ORDER BY id DESC LIMIT 1")
    assert log['performed_by'] == 'Reg One' and '"name": "1V"' in log['details']

    used = new_section(dbx, world, '1U')
    teacher = faculty(dbx, 'faculty')
    dbx.x("INSERT INTO faculty_assignments (faculty_id, subject_id, section_id) VALUES (%s,%s,%s)", (teacher, world['legacy']['A 101'], used))
    msg = registrar_post(client, {'action': 'delete_section', 'section_id': used})
    assert 'cannot be deleted' in msg and '1 faculty class record(s)' in msg and 'Archive it instead' in msg
    assert exists(dbx, used)
    assert dbx.one("SELECT COUNT(*) n FROM faculty_assignments WHERE section_id=%s", (used,))['n'] == 1


def test_archive_and_restore(app, client, dbx, world):
    sid = new_section(dbx, world, '1T')
    s = student_in(dbx, world)
    dbx.x("UPDATE applicants SET section_id=%s WHERE applicant_id=%s", (sid, s['applicant_id']))
    assert 'reason' in registrar_post(client, {'action': 'archive_section', 'section_id': sid, 'reason': ''})
    assert 'archived' in registrar_post(client, {'action': 'archive_section', 'section_id': sid, 'reason': 'Merged into 1A'})
    assert dbx.one("SELECT is_active FROM sections WHERE id=%s", (sid,))['is_active'] == 0
    assert dbx.one("SELECT section_id FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['section_id'] == sid   # kept
    assert 'already archived' in registrar_post(client, {'action': 'archive_section', 'section_id': sid, 'reason': 'again'})
    page = client.get('/admin/manage-sections').get_data(as_text=True)
    assert 'Archived' in page and 'Merged into 1A' in page
    # an archived section cannot be switched on with Edit, nor by re-generating the sections
    msg = registrar_post(client, {'action': 'edit_section', 'section_id': sid, 'max_students_edit': '40', 'is_active_edit': 'on'})
    assert 'Restore it before' in msg and dbx.one("SELECT is_active FROM sections WHERE id=%s", (sid,))['is_active'] == 0
    registrar_post(client, {'action': 'generate', 'program_id': world['pid'], 'year_level': '1st Year', 'num_sections': '20',
                            'max_students': '45', 'semester': '1'})
    assert dbx.one("SELECT COUNT(*) n FROM sections WHERE program_id=%s AND section_name='1K'", (world['pid'],))['n'] == 1  # generate ran
    row = dbx.one("SELECT is_active, max_students FROM sections WHERE id=%s", (sid,))
    assert row == {'is_active': 0, 'max_students': 45}          # updated by generate, but still archived
    assert 'restored' in registrar_post(client, {'action': 'restore_section', 'section_id': sid})
    assert dbx.one("SELECT is_active FROM sections WHERE id=%s", (sid,))['is_active'] == 1
    assert 'not archived' in registrar_post(client, {'action': 'restore_section', 'section_id': sid})
    acts = [r['action'] for r in dbx.q("SELECT action FROM enrollment_audit_log WHERE action LIKE 'section_%%' ORDER BY id DESC LIMIT 2")]
    assert acts == ['section_restore', 'section_archive']


def test_restore_puts_back_the_previous_state(app, dbx, world):
    sid = new_section(dbx, world, '1S', active=0)
    run(app, lambda: prot().archive(A(*REG), sid, 'old'))
    run(app, lambda: prot().restore(A(*REG), sid))
    assert dbx.one("SELECT is_active FROM sections WHERE id=%s", (sid,))['is_active'] == 0     # was inactive before


def test_archived_section_takes_no_new_students(app, client, dbx, world, fees):
    from website.auth import _assign_section_automatically
    only = new_section(dbx, world, '3R', year='3rd Year')
    run(app, lambda: prot().archive(A(*REG), only, 'closed'))
    s = make_student(dbx, {'title': world['title']})

    def auto():
        from website import db
        cur = db.session.connection().connection.cursor(dictionary=True)
        try:
            return _assign_section_automatically(cur, s['applicant_id'], world['pid'], '3rd Year')
        finally:
            cur.close()
    assert run(app, auto) is None                                   # auto-assignment skips it
    ready = ready_student(app, client, dbx, world)
    r = check_in_json(client, ready, world, section=only)          # a registrar cannot pick it at check-in
    assert r.status_code == 400 and 'not active' in r.get_json()['message']
    login_as(client, 'registrar')                                  # nor on a registration form
    fid = create(client, student_in(dbx, world), world['t1'])
    line = dbx.one("SELECT id FROM registration_form_subjects WHERE registration_form_id=%s LIMIT 1", (fid,))['id']
    client.post('/admin/registration/forms/%d/lines/%d/section' % (fid, line), data={'section_id': only})
    assert dbx.one("SELECT section_id FROM registration_form_subjects WHERE id=%s", (line,))['section_id'] != only


def test_roles(app, client, dbx, world):
    sid = new_section(dbx, world, '1Q')
    with pytest.raises(prot().SectionError, match='not allowed'):
        run(app, lambda: prot().archive(A('Cash', 'cashier'), sid, 'x'))
    login_as(client, 'cashier')
    client.post('/admin/manage-sections', data={'action': 'delete_section', 'section_id': sid})
    assert exists(dbx, sid)


# ---------------------------------------------------------------------------
# dean: section hub
# ---------------------------------------------------------------------------

def test_dean_delete_no_longer_destroys_class_records(app, client, dbx, world):
    sid = new_section(dbx, world, '1P')
    teacher = faculty(dbx, 'faculty')
    fa = dbx.x("INSERT INTO faculty_assignments (faculty_id, subject_id, section_id) VALUES (%s,%s,%s)", (teacher, world['legacy']['A 101'], sid))
    dean = faculty(dbx, 'dean')
    as_faculty(client, dean)
    client.post('/faculty/section/delete/%d' % sid)
    assert exists(dbx, sid) and dbx.one("SELECT id FROM faculty_assignments WHERE id=%s", (fa,))
    assert 'cannot be deleted' in flashes(client)
    client.post('/faculty/section/archive/%d' % sid, data={'reason': 'Merged'})
    assert dbx.one("SELECT is_archived, archived_by FROM section_archives WHERE section_id=%s", (sid,)) == {'is_archived': 1, 'archived_by': 'Dean Test'}
    client.post('/faculty/section/restore/%d' % sid)
    assert dbx.one("SELECT is_archived FROM section_archives WHERE section_id=%s", (sid,))['is_archived'] == 0
    free = new_section(dbx, world, '1O')
    client.post('/faculty/section/delete/%d' % free)
    assert not exists(dbx, free)
    teacher_login = faculty(dbx, 'faculty')
    as_faculty(client, teacher_login)
    client.post('/faculty/section/archive/%d' % sid, data={'reason': 'x'})
    assert dbx.one("SELECT is_archived FROM section_archives WHERE section_id=%s", (sid,))['is_archived'] == 0


def test_hub_shows_archived_sections(app, client, dbx, world):
    dbx.x("INSERT INTO system_settings (setting_key, setting_value) VALUES ('school_year','2026-2027') ON DUPLICATE KEY UPDATE setting_value='2026-2027'")
    sid = new_section(dbx, world, '1N')
    run(app, lambda: prot().archive(A(*REG), sid, 'closed'))
    as_faculty(client, faculty(dbx, 'dean'))
    page = client.get('/faculty/section-schedule-hub').get_data(as_text=True)
    assert 'Archived' in page and '/faculty/section/restore/%d' % sid in page


# ---------------------------------------------------------------------------
# program deletes
# ---------------------------------------------------------------------------

def _bare_program(dbx):
    code = _uniq('BP').upper()[:10]
    pid = code.lower()
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,%s)", (pid, 'Bare ' + code, code, 'Bare ' + code))
    sid = dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
                "VALUES (%s,'1st Year','1A','A',40,1,1,'2026-2027')", (pid,))
    return pid, sid


def test_faculty_program_delete_checks_sections(app, client, dbx, world):
    pid, sid = _bare_program(dbx)
    s = make_student(dbx, {'title': 'Someone else'})
    dbx.x("UPDATE applicants SET section_id=%s WHERE applicant_id=%s", (sid, s['applicant_id']))
    as_faculty(client, faculty(dbx, 'dean'))
    client.post('/faculty/delete-program/%s' % pid)
    assert 'section(s) still in use' in flashes(client)
    assert dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (pid,))
    dbx.x("UPDATE applicants SET section_id=NULL WHERE applicant_id=%s", (s['applicant_id'],))
    run(app, lambda: prot().archive(A(*REG), sid, 'unused'))          # an archive row must not block the delete
    client.post('/faculty/delete-program/%s' % pid)
    assert not dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (pid,)) and not exists(dbx, sid)


def test_registrar_program_delete_counts_registration_forms(app, client, dbx, world):
    login_as(client, 'admin')
    s = student_in(dbx, world)
    login_as(client, 'registrar')
    create(client, s, world['t1'])
    dbx.x("UPDATE applicants SET program_choice='moved' WHERE applicant_id=%s", (s['applicant_id'],))
    from website import db  # noqa: F401
    login_as(client, 'admin')
    client.post('/admin/program/delete/%s' % world['pid'])
    assert 'registration form(s)' in flashes(client)
    assert dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (world['pid'],))


def test_superadmin_force_delete_keeps_history(app, client, dbx, world):
    s = student_in(dbx, world)
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) VALUES (%s,%s,1.5,'Passed','2025-2026','1st Semester')",
          (s['student_user_id'], world['legacy']['A 101']))
    as_faculty(client, faculty(dbx, 'superadmin'))
    client.post('/faculty/grading-admin/delete_program/%s' % world['pid'])
    assert 'never deleted' in flashes(client)
    assert dbx.one("SELECT COUNT(*) n FROM student_grades WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 1
    pid, sid = _bare_program(dbx)                                      # configuration only: still deletable
    client.post('/faculty/grading-admin/delete_program/%s' % pid)
    assert not dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (pid,)) and not exists(dbx, sid)
