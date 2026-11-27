"""Phase 3: registration forms for regular and irregular students."""

import datetime
import re

import pytest

from conftest import login_as, login_student, make_student, _uniq


def _actor():
    from website.curriculum.service import Actor
    return Actor('Reg Tester', 'registrar')


# ---------------------------------------------------------------------------
# world: program, curriculum, sections, schedules, terms
# ---------------------------------------------------------------------------

CURRICULUM = [
    # code, units, year, sem, prereqs
    ('A 101', 3, 1, 1, []), ('A 102', 3, 1, 1, []), ('PE 1', 2, 1, 1, []),
    ('A 103', 3, 1, 2, ['A 101']), ('A 104', 3, 1, 2, []), ('PE 2', 2, 1, 2, ['PE 1']),
    ('B 201', 3, 2, 1, ['A 103']), ('B 202', 3, 2, 1, ['other:Approved by the dean']), ('B 203', 3, 2, 1, []),
    ('B 204', 3, 2, 2, ['B 201']), ('B 205', 3, 2, 2, []),
]


def _term(dbx, year, sem, active=False, max_units=26):
    row = dbx.one("SELECT id FROM academic_terms WHERE year_name=%s AND semester=%s", (year, sem))
    tid = row['id'] if row else dbx.x("INSERT INTO academic_terms (year_name, semester, is_active, max_units) VALUES (%s,%s,0,%s)", (year, sem, max_units))
    dbx.x("UPDATE academic_terms SET max_units=%s WHERE id=%s", (max_units, tid))
    if active:
        dbx.x("UPDATE academic_terms SET is_active = (id = %s)", (tid,))
    return tid


@pytest.fixture()
def world(app, dbx):
    code = _uniq('RF').upper()
    pid = code.lower()
    title = 'Regform Program ' + code
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,%s)", (pid, title, code, title))
    sections = {}
    for yl, name in (('1st Year', '1A'), ('1st Year', '1B'), ('2nd Year', '2A')):
        sections[name] = dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
                               "VALUES (%s,%s,%s,%s,40,1,1,'2026-2027')", (pid, yl, name, name[-1]))
    legacy = {}
    for c, u, y, s, _ in CURRICULUM:
        legacy[c] = dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) VALUES (%s,%s,%s,%s,%s,%s)",
                          (code + c.replace(' ', ''), 'Legacy ' + c, pid, u, s, y))
    with app.app_context():
        from website import db
        from website.curriculum import service
        v = service.create_version(pid, 'Plan ' + code, '2025-2026', _actor(), max_year_level=2)
        subj = {}
        for c, u, y, s, pre in CURRICULUM:
            subj[c] = service.add_subject(v.id, _actor(), code + ' ' + c, 'Subject ' + c, str(u), y, s)
        for c, u, y, s, pre in CURRICULUM:
            for p in pre:
                if p.startswith('other:'):
                    service.add_prerequisite(subj[c].id, _actor(), 'other', requirement_text=p[6:])
                else:
                    service.add_prerequisite(subj[c].id, _actor(), 'subject', prerequisite_subject_id=subj[p].id)
        service.activate_version(v.id, _actor(), confirm_incomplete=True, note='test fixture')
        service.set_default_version(v.id, _actor())
        db.session.commit()
        ids = {c: s.id for c, s in subj.items()}
        vid = v.id
        db.session.remove()
    t1 = _term(dbx, '2026-2027', '1st Semester', active=True)
    t2 = _term(dbx, '2026-2027', '2nd Semester')
    return {'pid': pid, 'code': code, 'title': title, 'sections': sections, 'legacy': legacy, 'cs': ids,
            'version': vid, 't1': t1, 't2': t2}


def full(world, c):
    return world['code'] + ' ' + c


def student_in(dbx, world, year_level='1st Year', section=None):
    s = make_student(dbx, {'title': world['title']})
    dbx.x("UPDATE applicants SET enrollment_year_level=%s, section_id=%s WHERE applicant_id=%s",
          (year_level, world['sections'].get(section) if section else None, s['applicant_id']))
    return s


def grade(dbx, world, s, c, remarks, ay='2025-2026', sem='1st Semester', g=None):
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) VALUES (%s,%s,%s,%s,%s,%s)",
          (s['student_user_id'], world['legacy'][c], g, remarks, ay, sem))


def schedule(dbx, world, c, section, day, start, end, year='2026-2027', sem=1):
    code = world['code'] + c.replace(' ', '')
    if not dbx.one("SELECT id FROM courses WHERE program_id=%s AND course_code=%s", (world['pid'], code)):
        dbx.x("INSERT INTO courses (program_id, course_code, course_description, units, year, semester) VALUES (%s,%s,'x',3,1,%s)",
              (world['pid'], code, sem))
    teacher = dbx.one("SELECT id FROM users WHERE username='rf_teacher'")
    if not teacher:
        tid = dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`) VALUES ('rf_teacher','rf_t@example.com','x','faculty','active','RF Teacher','faculty')")
    else:
        tid = teacher['id']
    dbx.x("INSERT INTO teaching_loads (user_id, course_code, section, day, time_start, time_end, room, school_year, is_published) "
          "VALUES (%s,%s,%s,%s,%s,%s,'R1',%s,1)", (tid, code, f"{world['code']} {section}", day, start, end, year))


def create(client, s, term_id):
    resp = client.post('/admin/registration/student/%d/create' % s['applicant_id'], data={'term_id': term_id})
    m = re.search(r'/forms/(\d+)', resp.location or '')
    assert m, (resp.status_code, resp.location)
    return int(m.group(1))


def lines(dbx, fid):
    return [r['subject_code'] for r in dbx.q("SELECT subject_code FROM registration_form_subjects WHERE registration_form_id=%s ORDER BY id", (fid,))]


def line_id(dbx, fid, code):
    return dbx.one("SELECT id FROM registration_form_subjects WHERE registration_form_id=%s AND subject_code=%s", (fid, code))['id']


def flashes(client):
    with client.session_transaction() as s:
        return ' '.join(m for _, m in s.get('_flashes', []))


# ---------------------------------------------------------------------------
# regular students
# ---------------------------------------------------------------------------

def test_regular_student_gets_the_curriculum_term_automatically(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    f = dbx.one("SELECT * FROM registration_forms WHERE id=%s", (fid,))
    assert f['classification'] == 'Regular' and f['status'] == 'draft'
    assert f['curriculum_version_id'] == world['version'] and f['year_level'] == 1 and f['semester'] == 1
    assert sorted(lines(dbx, fid)) == sorted(full(world, c) for c in ('A 101', 'A 102', 'PE 1'))
    assert float(f['total_units']) == 8.0
    # every line defaults to the home section
    assert {r['section_id'] for r in dbx.q("SELECT section_id FROM registration_form_subjects WHERE registration_form_id=%s", (fid,))} == {world['sections']['1A']}
    page = client.get('/admin/registration/forms/%d' % fid).get_data(as_text=True)
    assert full(world, 'A 101') in page and 'Regular students take exactly' in page


def test_regular_form_cannot_be_customised(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 104']})
    assert 'Mark the student Irregular' in flashes(client)
    client.post('/admin/registration/forms/%d/lines/%d/remove' % (fid, line_id(dbx, fid, full(world, 'A 101'))))
    assert len(lines(dbx, fid)) == 3


def test_finalize_requires_sections_and_sets_home_section(client, dbx, world):
    s = student_in(dbx, world)            # no section yet
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    page = client.get('/admin/registration/forms/%d' % fid).get_data(as_text=True)
    assert 'has no section' in page
    client.post('/admin/registration/forms/%d/finalize' % fid)
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'draft'
    client.post('/admin/registration/forms/%d/block-section' % fid, data={'section_id': world['sections']['1B']})
    client.post('/admin/registration/forms/%d/finalize' % fid)
    f = dbx.one("SELECT status, finalized_by, section_id FROM registration_forms WHERE id=%s", (fid,))
    assert f['status'] == 'finalized' and f['finalized_by'] == 'Test User'
    assert dbx.one("SELECT section_id FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['section_id'] == world['sections']['1B']


def test_schedule_snapshot_and_print(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    schedule(dbx, world, 'A 101', '1A', 'Monday', '08:00:00', '11:00:00')
    schedule(dbx, world, 'A 101', '1A', 'Wednesday', '08:00:00', '09:30:00')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    txt = dbx.one("SELECT schedule_text FROM registration_form_subjects WHERE registration_form_id=%s AND subject_code=%s",
                  (fid, full(world, 'A 101')))['schedule_text']
    assert txt == 'MON 8:00a-11:00a R1; WED 8:00a-9:30a R1'
    html = client.get('/admin/registration/forms/%d/print' % fid).get_data(as_text=True)
    assert 'CERTIFICATE OF REGISTRATION' in html and 'MON 8:00a-11:00a' in html and 'DRAFT' not in html
    assert 'Enrolled by' in html and 'College Registrar' in html
    assert re.search(r'TOTAL</td><td class="num">8</td>', html)


# ---------------------------------------------------------------------------
# irregular students
# ---------------------------------------------------------------------------

def test_failed_subject_makes_student_irregular_and_blocks_dependants(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    grade(dbx, world, s, 'A 101', 'Failed', g=5.0)
    grade(dbx, world, s, 'A 102', 'Passed', g=2.0)
    grade(dbx, world, s, 'PE 1', 'Passed', g=1.5)
    login_as(client, 'registrar')
    fid = create(client, s, world['t2'])
    f = dbx.one("SELECT classification, classification_reason, semester FROM registration_forms WHERE id=%s", (fid,))
    assert f['classification'] == 'Irregular' and f['semester'] == 2 and full(world, 'A 101') in f['classification_reason']
    # suggestions: eligible subjects of the term only (A 103 needs A 101)
    assert sorted(lines(dbx, fid)) == sorted([full(world, 'A 104'), full(world, 'PE 2')])
    page = client.get('/admin/registration/forms/%d' % fid).get_data(as_text=True)
    assert 'Prerequisite %s not passed (failed)' % full(world, 'A 101') in page
    assert 'Already passed' in page                                    # A 102
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 103']})
    assert 'not passed' in flashes(client) and full(world, 'A 103') not in lines(dbx, fid)
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 102']})
    assert 'Already passed' in flashes(client)
    # the failed subject can be retaken (back subject, off-semester)
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 101'], 'section_id': world['sections']['1A']})
    row = dbx.one("SELECT source FROM registration_form_subjects WHERE registration_form_id=%s AND subject_code=%s", (fid, full(world, 'A 101')))
    assert row['source'] == 'back_subject'
    log = dbx.one("SELECT details FROM registration_form_changes WHERE registration_form_id=%s AND action='add'", (fid,))
    assert 'not this term' in log['details']


def test_subjects_from_other_year_levels_and_standing(client, dbx, world):
    s = student_in(dbx, world, year_level='2nd Year', section='2A')
    for c in ('A 101', 'A 102', 'PE 1', 'A 104', 'PE 2'):
        grade(dbx, world, s, c, 'Passed', g=2.0)
    grade(dbx, world, s, 'A 103', 'Incomplete')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    assert dbx.one("SELECT classification FROM registration_forms WHERE id=%s", (fid,))['classification'] == 'Irregular'
    # B 201 needs A 103 (incomplete) -> blocked; B 203 fine; retake A 103
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['B 201']})
    assert 'incomplete' in flashes(client)
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 103']})
    assert full(world, 'A 103') in lines(dbx, fid)


def test_unverifiable_requirement_needs_registrar_confirmation(client, dbx, world):
    s = student_in(dbx, world, year_level='2nd Year', section='2A')
    for c in ('A 101', 'A 102', 'PE 1', 'A 103', 'A 104', 'PE 2'):
        grade(dbx, world, s, c, 'Passed', g=2.0)
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    dbx.x("UPDATE registration_forms SET classification='Irregular' WHERE id=%s", (fid,))
    client.post('/admin/registration/forms/%d/lines/%d/remove' % (fid, line_id(dbx, fid, full(world, 'B 201'))))
    # B 202 has a free-text requirement -> confirmation step
    if full(world, 'B 202') in lines(dbx, fid):
        client.post('/admin/registration/forms/%d/lines/%d/remove' % (fid, line_id(dbx, fid, full(world, 'B 202'))))
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['B 202']})
    assert full(world, 'B 202') not in lines(dbx, fid)
    page = client.get('/admin/registration/forms/%d' % fid).get_data(as_text=True)
    assert 'needs your confirmation' in page and 'Approved by the dean' in page
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['B 202'], 'acknowledge': '1',
                                                             'remarks': 'Dean approval on file'})
    row = dbx.one("SELECT remarks FROM registration_form_subjects WHERE registration_form_id=%s AND subject_code=%s", (fid, full(world, 'B 202')))
    assert row['remarks'] == 'Dean approval on file'
    log = dbx.one("SELECT details FROM registration_form_changes WHERE registration_form_id=%s AND action='add' AND subject_code=%s",
                  (fid, full(world, 'B 202')))
    assert 'Approved by the dean' in log['details']


def test_regular_subjects_with_free_text_requirement_are_not_auto_added_for_irregulars(client, dbx, world):
    s = student_in(dbx, world, year_level='2nd Year', section='2A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])          # nothing passed -> irregular
    assert full(world, 'B 202') not in lines(dbx, fid)


def test_unit_limit(client, dbx, world):
    _term(dbx, '2026-2027', '1st Semester', max_units=10)
    s = student_in(dbx, world, year_level='2nd Year', section='2A')
    for c in ('A 101', 'A 102', 'PE 1', 'A 103', 'A 104', 'PE 2'):
        grade(dbx, world, s, c, 'Passed', g=2.0)
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    f = dbx.one("SELECT max_units, total_units, classification FROM registration_forms WHERE id=%s", (fid,))
    # the regular 2nd-year load (9 units) is below 10 -> limit 10
    assert float(f['max_units']) == 10.0
    client.post('/admin/registration/forms/%d/classification' % fid, data={'classification': 'Irregular', 'reason': 'test'})
    assert float(f['total_units']) == 9.0
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['B 205']})
    assert 'limit for this student is 10' in flashes(client)
    assert full(world, 'B 205') not in lines(dbx, fid)
    _term(dbx, '2026-2027', '1st Semester', max_units=26)


def test_regular_load_above_configured_limit_is_still_allowed(client, dbx, world):
    _term(dbx, '2026-2027', '1st Semester', max_units=5)
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    f = dbx.one("SELECT max_units, total_units FROM registration_forms WHERE id=%s", (fid,))
    assert float(f['max_units']) == 8.0 and float(f['total_units']) == 8.0
    _term(dbx, '2026-2027', '1st Semester', max_units=26)


def test_schedule_conflict_is_refused(client, dbx, world):
    s = student_in(dbx, world, year_level='2nd Year', section='2A')
    for c in ('A 101', 'A 102', 'PE 1', 'A 104', 'PE 2'):
        grade(dbx, world, s, c, 'Passed', g=2.0)
    schedule(dbx, world, 'B 203', '2A', 'Tuesday', '08:00:00', '11:00:00')
    schedule(dbx, world, 'A 103', '1B', 'Tuesday', '10:00:00', '12:00:00', sem=1)
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    assert full(world, 'B 203') in lines(dbx, fid)
    client.post('/admin/registration/forms/%d/add' % fid, data={'curriculum_subject_id': world['cs']['A 103'], 'section_id': world['sections']['1B']})
    msg = flashes(client)
    assert 'Schedule conflict' in msg and 'Tuesday' in msg
    assert full(world, 'A 103') not in lines(dbx, fid)


def test_full_section_is_refused(client, dbx, world):
    dbx.x("UPDATE sections SET max_students=1 WHERE id=%s", (world['sections']['1B'],))
    login_as(client, 'registrar')
    first = student_in(dbx, world)
    fid1 = create(client, first, world['t1'])
    client.post('/admin/registration/forms/%d/block-section' % fid1, data={'section_id': world['sections']['1B']})
    second = student_in(dbx, world)
    fid2 = create(client, second, world['t1'])
    client.post('/admin/registration/forms/%d/block-section' % fid2, data={'section_id': world['sections']['1B']})
    assert 'is full' in flashes(client)
    assert dbx.one("SELECT section_id FROM registration_forms WHERE id=%s", (fid2,))['section_id'] is None


def test_section_must_offer_the_subject(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    schedule(dbx, world, 'A 102', '1A', 'Friday', '13:00:00', '16:00:00')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/lines/%d/section' % (fid, line_id(dbx, fid, full(world, 'A 102'))),
                data={'section_id': world['sections']['2A']})
    assert 'does not offer' in flashes(client)


def test_reclassify_to_regular_rebuilds_and_logs(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/classification' % fid, data={'classification': 'Irregular', 'reason': 'Transferee'})
    client.post('/admin/registration/forms/%d/lines/%d/remove' % (fid, line_id(dbx, fid, full(world, 'PE 1'))))
    assert len(lines(dbx, fid)) == 2
    client.post('/admin/registration/forms/%d/classification' % fid, data={'classification': 'Regular', 'reason': ''})
    assert 'reason' in flashes(client)
    client.post('/admin/registration/forms/%d/classification' % fid, data={'classification': 'Regular', 'reason': 'Evaluation done'})
    assert len(lines(dbx, fid)) == 3
    actions = [r['action'] for r in dbx.q("SELECT action FROM registration_form_changes WHERE registration_form_id=%s ORDER BY id", (fid,))]
    assert actions == ['create', 'classification', 'remove', 'rebuild_regular', 'classification']
    rebuilt = dbx.one("SELECT details FROM registration_form_changes WHERE registration_form_id=%s AND action='rebuild_regular'", (fid,))['details']
    assert '"before"' in rebuilt and '"after"' in rebuilt


# ---------------------------------------------------------------------------
# history, finalize / reopen, duplicates
# ---------------------------------------------------------------------------

def test_finalized_form_is_frozen_and_reopen_needs_reason(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    client.post('/admin/registration/forms/%d/classification' % fid, data={'classification': 'Irregular', 'reason': 'x'})
    assert 'finalized' in flashes(client)
    client.post('/admin/registration/forms/%d/reopen' % fid, data={'reason': ''})
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'finalized'
    client.post('/admin/registration/forms/%d/reopen' % fid, data={'reason': 'Student shifted section'})
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'draft'
    reo = dbx.one("SELECT details FROM registration_form_changes WHERE registration_form_id=%s AND action='reopen'", (fid,))['details']
    assert 'Student shifted section' in reo and full(world, 'A 101') in reo


def test_one_form_per_term_and_separate_history(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid1 = create(client, s, world['t1'])
    assert create(client, s, world['t1']) == fid1          # same term -> same form
    client.post('/admin/registration/forms/%d/finalize' % fid1)
    fid2 = create(client, s, world['t2'])
    assert fid2 != fid1
    # first-term subjects on a finalized form, no grade yet -> "in progress", needs confirmation to retake
    assert dbx.one("SELECT COUNT(*) n FROM registration_forms WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 2
    assert sorted(lines(dbx, fid1)) == sorted(full(world, c) for c in ('A 101', 'A 102', 'PE 1'))


def test_new_curriculum_does_not_change_existing_forms(app, client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid)
    before = dbx.q("SELECT subject_code, description, units FROM registration_form_subjects WHERE registration_form_id=%s ORDER BY id", (fid,))
    with app.app_context():
        from website import db
        from website.curriculum import service
        new = service.clone_version(world['version'], _actor(), 'Plan v2 ' + world['code'], '2027-2028')
        sub = [x for x in new.subjects.all() if x.code_key.endswith('A101')][0]
        service.update_subject(sub.id, _actor(), sub.subject_code, 'Renamed in v2', '5', 1, 1)
        service.activate_version(new.id, _actor(), confirm_incomplete=True, note='v2')
        service.set_default_version(new.id, _actor())
        db.session.commit()
        db.session.remove()
    after = dbx.q("SELECT subject_code, description, units FROM registration_form_subjects WHERE registration_form_id=%s ORDER BY id", (fid,))
    assert before == after
    assert dbx.one("SELECT curriculum_version_id v FROM registration_forms WHERE id=%s", (fid,))['v'] == world['version']


def test_missing_curriculum_is_explained(client, dbx):
    pid = _uniq('nc')
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,%s)", (pid, 'NoCurr ' + pid, pid.upper(), 'x'))
    s = make_student(dbx, {'title': 'NoCurr ' + pid})
    login_as(client, 'registrar')
    html = client.get('/admin/registration/student/%d' % s['applicant_id']).get_data(as_text=True)
    assert 'No curriculum applies to this student' in html


# ---------------------------------------------------------------------------
# students and access control
# ---------------------------------------------------------------------------

def test_student_sees_only_own_finalized_form(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    other = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid = create(client, s, world['t1'])
    fid_other = create(client, other, world['t1'])
    login_student(client, s['student_user_id'])
    assert client.get('/my-registration/%d' % fid).status_code == 404        # still a draft
    login_as(client, 'registrar')
    client.post('/admin/registration/forms/%d/finalize' % fid)
    client.post('/admin/registration/forms/%d/finalize' % fid_other)
    with client.session_transaction() as sess:
        sess.clear()
    login_student(client, s['student_user_id'])
    assert client.get('/my-registration/%d' % fid).status_code == 200
    assert client.get('/my-registration/%d' % fid_other).status_code == 404
    assert client.get('/my-registration').location.endswith('/my-registration/%d' % fid)
    assert client.post('/admin/registration/forms/%d/reopen' % fid, data={'reason': 'x'}).status_code == 302
    assert dbx.one("SELECT status FROM registration_forms WHERE id=%s", (fid,))['status'] == 'finalized'


@pytest.mark.parametrize('role', ['cashier', 'accounting', 'guidance', 'secretary'])
def test_only_registrar_and_admin_manage_forms(client, dbx, world, role):
    s = student_in(dbx, world, section='1A')
    login_as(client, role)
    assert client.get('/admin/registration/').status_code == 302
    client.post('/admin/registration/student/%d/create' % s['applicant_id'], data={'term_id': world['t1']})
    assert dbx.one("SELECT COUNT(*) n FROM registration_forms WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 0


def test_enrollment_review_links_to_regform(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    html = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert 'Prepare registration form' in html
    fid = create(client, s, world['t1'])
    html = client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert '/admin/registration/forms/%d' % fid in html
    lst = client.get('/admin/registration/?term_id=%d&program=%s' % (world['t1'], world['pid'])).get_data(as_text=True)
    assert s['email'] not in lst and 'Dela Cruz' in lst


def test_retaking_a_subject_whose_grade_is_not_recorded_needs_confirmation(client, dbx, world):
    s = student_in(dbx, world, section='1A')
    login_as(client, 'registrar')
    fid1 = create(client, s, world['t1'])
    client.post('/admin/registration/forms/%d/finalize' % fid1)
    fid2 = create(client, s, world['t2'])
    client.post('/admin/registration/forms/%d/classification' % fid2, data={'classification': 'Irregular', 'reason': 'Retake check'})
    client.post('/admin/registration/forms/%d/add' % fid2, data={'curriculum_subject_id': world['cs']['A 102']})
    assert full(world, 'A 102') not in lines(dbx, fid2)
    page = client.get('/admin/registration/forms/%d' % fid2).get_data(as_text=True)
    assert 'no grade is recorded yet' in page


def test_two_registrars_creating_the_same_form_get_one_form(app, dbx, world):
    import threading
    s = student_in(dbx, world, section='1A')
    results = []

    def go():
        c = app.test_client()
        login_as(c, 'registrar')
        r = c.post('/admin/registration/student/%d/create' % s['applicant_id'], data={'term_id': world['t1']})
        results.append(r.status_code)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [302] * 4
    rows = dbx.q("SELECT id FROM registration_forms WHERE student_user_id=%s AND academic_term_id=%s", (s['student_user_id'], world['t1']))
    assert len(rows) == 1
    assert len(lines(dbx, rows[0]['id'])) == 3
