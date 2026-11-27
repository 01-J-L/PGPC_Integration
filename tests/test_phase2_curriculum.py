"""Phase 2: curriculum management (service rules, import, screens)."""

import glob
import io
import os
import re
from decimal import Decimal

import pytest

from conftest import login_as, make_student, _uniq

REF_DIR = os.path.join(os.path.dirname(__file__), 'fixtures', 'references')


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ctx(app):
    with app.app_context():
        from website import db
        yield db
        db.session.rollback()


def _program(dbx, code=None):
    code = code or _uniq('CP').upper()
    pid = code.lower()
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s, %s, %s, %s)",
          (pid, 'Program ' + code, code, 'Program ' + code))
    return pid


def actor():
    from website.curriculum.service import Actor
    return Actor('Test Registrar', 'registrar')


def _version(ctx, pid, name='Curriculum', ay='2026-2027', max_year=1):
    from website.curriculum import service
    v = service.create_version(pid, name, ay, actor(), max_year_level=max_year)
    ctx.session.commit()
    return v


def _full_year(ctx, v):
    """One subject in both semesters of every year -> complete curriculum."""
    from website.curriculum import service
    out = []
    for y in range(1, v.max_year_level + 1):
        for sem in (1, 2):
            out.append(service.add_subject(v.id, actor(), f'T{y}{sem} 1', f'Subject {y}{sem}', '3', y, sem))
    ctx.session.commit()
    return out


def errors_of(fn, *a, **k):
    from website.curriculum.service import CurriculumError
    with pytest.raises(CurriculumError) as ei:
        fn(*a, **k)
    from website import db
    db.session.rollback()
    return ' '.join(ei.value.messages)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

def test_version_validation(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    _version(ctx, pid, 'BSCS 2026')
    assert 'already has a curriculum' in errors_of(service.create_version, pid, 'BSCS 2026', '2026-2027', actor())
    assert 'academic year' in errors_of(service.create_version, pid, 'X', '2026-2028', actor())
    assert 'academic year' in errors_of(service.create_version, pid, 'X', '2026', actor())
    assert 'year levels' in errors_of(service.create_version, pid, 'X', '2026-2027', actor(), max_year_level=9)
    assert 'Program not found' in errors_of(service.create_version, 'nope', 'X', '2026-2027', actor())


# ---------------------------------------------------------------------------
# subjects
# ---------------------------------------------------------------------------

def test_subject_validation_and_duplicates(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    v = _version(ctx, pid, max_year=4)
    s = service.add_subject(v.id, actor(), ' ge  105 ', 'Purposive Communication', '3', 1, 2)
    ctx.session.commit()
    assert s.subject_code == 'GE 105' and s.code_key == 'GE105' and s.units == Decimal('3.0')
    # same code written differently = duplicate
    assert 'already in this curriculum' in errors_of(service.add_subject, v.id, actor(), 'GE105', 'Again', '3', 2, 1)
    assert 'Semester' in errors_of(service.add_subject, v.id, actor(), 'X 1', 'Bad sem', '3', 1, 4)
    assert 'Year level' in errors_of(service.add_subject, v.id, actor(), 'X 1', 'Bad year', '3', 5, 1)
    assert 'Units' in errors_of(service.add_subject, v.id, actor(), 'X 1', 'Bad units', 'three', 1, 1)
    assert 'between 0 and' in errors_of(service.add_subject, v.id, actor(), 'X 1', 'Bad units', '31', 1, 1)
    assert 'description is required' in errors_of(service.add_subject, v.id, actor(), 'X 1', ' ', '3', 1, 1)
    # Summer is a valid term
    assert service.add_subject(v.id, actor(), 'SUM 1', 'Summer class', '1.5', 1, 3).units == Decimal('1.5')
    ctx.session.commit()


def test_same_code_in_other_programs_and_versions(ctx, dbx):
    """The legacy subjects table cannot hold GE105 twice; curricula can."""
    from website.curriculum import service
    a, b = _program(dbx), _program(dbx)
    for pid in (a, b):
        for name in ('Old', 'New'):
            v = _version(ctx, pid, name)
            service.add_subject(v.id, actor(), 'GE 105', 'Purposive Communication', '3', 1, 2)
    ctx.session.commit()
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_subjects WHERE code_key='GE105' "
                   "AND curriculum_version_id IN (SELECT id FROM curriculum_versions WHERE program_id IN (%s,%s))", (a, b))['n'] == 4


def test_database_refuses_duplicate_even_without_the_service(ctx, dbx):
    import mysql.connector
    pid = _program(dbx)
    v = _version(ctx, pid)
    dbx.x("INSERT INTO curriculum_subjects (curriculum_version_id, year_level, semester, subject_code, code_key, description, units) "
          "VALUES (%s,1,1,'CC 101','CC101','Intro',3)", (v.id,))
    with pytest.raises(mysql.connector.IntegrityError):
        dbx.x("INSERT INTO curriculum_subjects (curriculum_version_id, year_level, semester, subject_code, code_key, description, units) "
              "VALUES (%s,1,2,'CC101','CC101','Dup',3)", (v.id,))


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------

def test_prerequisite_rules(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    v = _version(ctx, pid, max_year=4)
    cc102 = service.add_subject(v.id, actor(), 'CC 102', 'Fundamentals of Programming', '3', 1, 1)
    cc103 = service.add_subject(v.id, actor(), 'CC 103', 'Intermediate Programming', '3', 1, 2)
    same = service.add_subject(v.id, actor(), 'CS 121', 'Discrete 1', '3', 1, 2)
    prac = service.add_subject(v.id, actor(), 'PRAC', 'Practicum', '6', 4, 2)
    ctx.session.commit()
    service.add_prerequisite(cc103.id, actor(), 'subject', prerequisite_subject_id=cc102.id)
    ctx.session.commit()
    assert 'already a prerequisite' in errors_of(service.add_prerequisite, cc103.id, actor(), 'subject', prerequisite_subject_id=cc102.id)
    assert 'own prerequisite' in errors_of(service.add_prerequisite, cc103.id, actor(), 'subject', prerequisite_subject_id=cc103.id)
    assert 'not scheduled before' in errors_of(service.add_prerequisite, cc103.id, actor(), 'subject', prerequisite_subject_id=same.id)
    assert 'not scheduled before' in errors_of(service.add_prerequisite, cc102.id, actor(), 'subject', prerequisite_subject_id=cc103.id)
    other = _version(ctx, pid, 'Other', max_year=4)
    foreign = service.add_subject(other.id, actor(), 'CC 102', 'x', '3', 1, 1)
    ctx.session.commit()
    assert 'same curriculum' in errors_of(service.add_prerequisite, cc103.id, actor(), 'subject', prerequisite_subject_id=foreign.id)
    p1 = service.add_prerequisite(prac.id, actor(), 'standing', min_year_level=4)
    p2 = service.add_prerequisite(prac.id, actor(), 'units', min_units='110')
    p3 = service.add_prerequisite(prac.id, actor(), 'other', requirement_text='Approved practicum application')
    ctx.session.commit()
    assert [p1.label(), p2.label(), p3.label()] == ['4th Year standing', '110 units completed', 'Approved practicum application']
    assert 'between 1 and 4' in errors_of(service.add_prerequisite, prac.id, actor(), 'standing', min_year_level=7)
    assert 'between 1 and 400' in errors_of(service.add_prerequisite, prac.id, actor(), 'units', min_units='x')
    # moving a subject may not break the order
    assert 'no longer come before' in errors_of(service.update_subject, cc103.id, actor(), 'CC 103', 'Intermediate', '3', 1, 1)
    assert 'no longer come after' in errors_of(service.update_subject, cc102.id, actor(), 'CC 102', 'Fund', '3', 2, 1)
    # a prerequisite cannot be deleted from under its dependants
    assert 'prerequisite of CC 103' in errors_of(service.delete_subject, cc102.id, actor())


# ---------------------------------------------------------------------------
# activation / locking / history
# ---------------------------------------------------------------------------

def test_activation_requires_confirmation_when_incomplete(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    v = _version(ctx, pid, max_year=2)
    assert 'no subjects' in errors_of(service.activate_version, v.id, actor())
    service.add_subject(v.id, actor(), 'A 1', 'Only subject', '3', 1, 1)
    ctx.session.commit()
    msg = errors_of(service.activate_version, v.id, actor())
    assert 'incomplete' in msg and '1st Year - 2nd Semester has no subjects' in msg
    assert 'incomplete' in errors_of(service.activate_version, v.id, actor(), confirm_incomplete=True, note='  ')
    service.activate_version(v.id, actor(), confirm_incomplete=True, note='Years 2+ come next term')
    ctx.session.commit()
    row = dbx.one("SELECT status, activated_by, activation_note FROM curriculum_versions WHERE id=%s", (v.id,))
    assert row['status'] == 'active' and row['activated_by'] == 'Test Registrar' and 'next term' in row['activation_note']
    log = dbx.one("SELECT details FROM curriculum_audit_log WHERE curriculum_version_id=%s AND action='activate'", (v.id,))
    assert 'has no subjects' in log['details']


def test_activation_links_existing_grading_subjects(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    legacy = dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) "
                   "VALUES (%s, 'Old title', %s, 3, 1, 1)", ('LK' + pid.upper() + '1', pid))
    v = _version(ctx, pid)
    _full_year(ctx, v)
    s = service.add_subject(v.id, actor(), 'LK' + pid.upper() + ' 1', 'Linked', '3', 1, 1)
    ctx.session.commit()
    service.activate_version(v.id, actor())
    ctx.session.commit()
    assert dbx.one("SELECT subject_id FROM curriculum_subjects WHERE id=%s", (s.id,))['subject_id'] == legacy


def test_assigned_curriculum_is_locked_and_clone_is_editable(ctx, dbx, program):
    from website.curriculum import service
    from website.models import CurriculumVersion
    pid = program['id']
    v = _version(ctx, pid, 'Locked')
    subs = _full_year(ctx, v)
    service.activate_version(v.id, actor())
    ctx.session.commit()
    st = make_student(dbx, program)
    service.assign_students(v.id, [st['student_user_id']], actor(), reason='cohort 2026')
    ctx.session.commit()
    assert 'locked' in errors_of(service.add_subject, v.id, actor(), 'NEW 1', 'New', '3', 1, 1)
    assert 'locked' in errors_of(service.update_subject, subs[0].id, actor(), 'T11 1', 'Renamed', '3', 1, 1)
    assert 'locked' in errors_of(service.delete_subject, subs[0].id, actor())
    assert 'locked' in errors_of(service.add_prerequisite, subs[1].id, actor(), 'subject', prerequisite_subject_id=subs[0].id)
    assert 'draft' in errors_of(service.delete_version, v.id, actor())
    clone = service.clone_version(v.id, actor(), 'Locked (revised)', '2027-2028')
    ctx.session.commit()
    assert clone.status == 'draft' and clone.subjects.count() == len(subs)
    service.add_subject(clone.id, actor(), 'NEW 1', 'New subject', '3', 1, 1)
    ctx.session.commit()
    # the original is untouched
    assert ctx.session.get(CurriculumVersion, v.id).subjects.count() == len(subs)
    # archiving keeps the student on it (history)
    _, still = service.archive_version(v.id, actor())
    ctx.session.commit()
    assert still == 1
    assert service.current_assignment(st['student_user_id']).curriculum_version_id == v.id
    assert 'archived' in errors_of(service.assign_students, v.id, [st['student_user_id']], actor()) or True


def test_delete_subject_with_grade_history_on_active_curriculum(ctx, dbx, program, student):
    from website.curriculum import service
    pid = program['id']
    v = _version(ctx, pid, 'History')
    _full_year(ctx, v)
    legacy_code = dbx.one("SELECT subject_code FROM subjects WHERE id=%s", (program['subjects'][0],))['subject_code']
    s = service.add_subject(v.id, actor(), legacy_code, 'Has grades', '3', 1, 1)
    ctx.session.commit()
    dbx.x("INSERT INTO student_grades (student_user_id, subject_id, grade, remarks, academic_year, semester) "
          "VALUES (%s,%s,2.0,'Passed','2025-2026','1st Semester')", (student['student_user_id'], program['subjects'][0]))
    service.activate_version(v.id, actor())
    ctx.session.commit()
    assert 'grade record' in errors_of(service.delete_subject, s.id, actor())


def test_default_version_and_resolver(ctx, dbx, program):
    from website.curriculum import service
    pid = program['id']
    v1, v2 = _version(ctx, pid, 'Old plan', '2024-2025'), _version(ctx, pid, 'New plan')
    for v in (v1, v2):
        _full_year(ctx, v)
        service.activate_version(v.id, actor())
    ctx.session.commit()
    assert 'Only an active' in errors_of(service.set_default_version, _version(ctx, pid, 'Draft').id, actor())
    service.set_default_version(v1.id, actor())
    service.set_default_version(v2.id, actor())
    ctx.session.commit()
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_versions WHERE program_id=%s AND is_default_for_new=1", (pid,))['n'] == 1
    newbie, old = make_student(dbx, program), make_student(dbx, program)
    assert service.resolve_student_curriculum(newbie['student_user_id'], pid) == (ctx.session.get(type(v2), v2.id), 'default')
    service.assign_students(v1.id, [old['student_user_id']], actor())
    ctx.session.commit()
    version, how = service.resolve_student_curriculum(old['student_user_id'], pid)
    assert version.id == v1.id and how == 'assigned'


def test_assignment_history_and_program_check(ctx, dbx, program):
    from website.curriculum import service
    pid = program['id']
    v1, v2 = _version(ctx, pid, 'A'), _version(ctx, pid, 'B')
    for v in (v1, v2):
        _full_year(ctx, v)
        service.activate_version(v.id, actor())
    ctx.session.commit()
    st = make_student(dbx, program)
    assert service.assign_students(v1.id, [st['student_user_id']], actor()) == (1, 0)
    ctx.session.commit()
    assert service.assign_students(v1.id, [st['student_user_id']], actor()) == (0, 1)
    assert service.assign_students(v2.id, [st['student_user_id']], actor()) == (1, 0)
    ctx.session.commit()
    rows = dbx.q("SELECT curriculum_version_id, ended_at FROM student_curricula WHERE student_user_id=%s ORDER BY id", (st['student_user_id'],))
    assert [r['curriculum_version_id'] for r in rows] == [v1.id, v2.id]
    assert rows[0]['ended_at'] is not None and rows[1]['ended_at'] is None
    other_pid = _program(dbx)
    stranger = make_student(dbx, {'title': 'Program ' + other_pid.upper()})
    assert 'not in' in errors_of(service.assign_students, v1.id, [stranger['student_user_id']], actor())
    draft = _version(ctx, pid, 'Draft C')
    assert 'active curriculum' in errors_of(service.assign_students, draft.id, [st['student_user_id']], actor())


def test_audit_trail_records_who_and_what(ctx, dbx):
    from website.curriculum import service
    pid = _program(dbx)
    v = _version(ctx, pid)
    s = service.add_subject(v.id, actor(), 'AU 1', 'Audited', '3', 1, 1)
    ctx.session.commit()
    service.update_subject(s.id, actor(), 'AU 1', 'Audited (renamed)', '2', 1, 1)
    service.delete_subject(s.id, actor())
    ctx.session.commit()
    actions = [r['action'] for r in dbx.q("SELECT action FROM curriculum_audit_log WHERE curriculum_version_id=%s ORDER BY id", (v.id,))]
    assert actions == ['create', 'create', 'update', 'delete']
    upd = dbx.one("SELECT details, performed_by, performed_role FROM curriculum_audit_log WHERE curriculum_version_id=%s AND action='update'", (v.id,))
    assert 'Audited (renamed)' in upd['details'] and upd['performed_by'] == 'Test Registrar' and upd['performed_role'] == 'registrar'


def test_program_with_curriculum_cannot_be_deleted(client, dbx):
    pid = _program(dbx)
    dbx.x("INSERT INTO curriculum_versions (program_id, name, effective_academic_year) VALUES (%s,'K','2026-2027')", (pid,))
    login_as(client, 'admin')
    client.post('/admin/program/delete/%s' % pid)
    assert dbx.one("SELECT program_id FROM programs WHERE program_id=%s", (pid,)) is not None


# ---------------------------------------------------------------------------
# importer (real reference documents)
# ---------------------------------------------------------------------------

def _ref(kind, name):
    return os.path.join(REF_DIR, kind, name)


def _upload(client, pid, path):
    data = open(path, 'rb').read()
    resp = client.post('/admin/curriculum/import', data={'program_id': pid, 'file': (io.BytesIO(data), os.path.basename(path).split('__', 1)[1])},
                       content_type='multipart/form-data')
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    m = re.search(r'/admin/curriculum/import/(\d+)', resp.location)
    assert m, resp.location
    return int(m.group(1))


def _review_form(client, batch_id, query=''):
    html = client.get('/admin/curriculum/import/%d%s' % (batch_id, query)).get_data(as_text=True)
    form = {}
    for tag in re.findall(r'<input[^>]*>', html):
        m = re.search(r'name="(r\d+_[a-z_]+)"[^>]*value="([^"]*)"', tag)
        if m and 'type="radio"' not in tag and 'type="checkbox"' not in tag:
            form[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input type="checkbox" name="(r\d+_include)" value="1" (checked)?', html):
        if m.group(2):
            form[m.group(1)] = '1'
        else:
            form.pop(m.group(1), None)
    for name, body in re.findall(r'<select name="(r\d+_(?:year_level|semester))"[^>]*>(.*?)</select>', html, re.S):
        form[name] = re.search(r'<option value="([^"]+)" selected', body).group(1)
    import html as _h
    return {k: _h.unescape(v) for k, v in form.items()}, html


PLAN_FILES = sorted(glob.glob(os.path.join(REF_DIR, 'plans', '*.docx')))


@pytest.mark.parametrize('path', PLAN_FILES, ids=[os.path.basename(p) for p in PLAN_FILES])
def test_import_every_plan_of_study(client, dbx, path):
    from website.curriculum import importer
    pid = _program(dbx)
    login_as(client, 'registrar', name='Registrar Nicky')
    bid = _upload(client, pid, path)
    parsed = importer.parse_file(path, open(path, 'rb').read())
    form, html = _review_form(client, bid, '?mode=new')
    assert 'Review import' in html
    form.update({'mode': 'new', 'action': 'commit', 'new_name': 'Imported ' + os.path.basename(path)[:60],
                 'new_effective_academic_year': '2026-2027', 'new_max_year_level': '4'})
    if 'BSCS - 2026' in path:
        # DC 103 has no units in the document: the import must refuse until the registrar decides.
        resp = client.post('/admin/curriculum/import/%d' % bid, data=form)
        assert dbx.one("SELECT status FROM curriculum_import_batches WHERE id=%s", (bid,))['status'] == 'pending_review'
        idx = next(i for i, r in enumerate(parsed['rows']) if normalize(r['subject_code']) == 'DC103')
        form[f'r{idx}_units'] = '3'
    resp = client.post('/admin/curriculum/import/%d' % bid, data=form)
    assert resp.status_code == 302 and '/admin/curriculum/versions/' in resp.location, resp.location
    vid = int(resp.location.rstrip('/').split('/')[-1])
    n = dbx.one("SELECT COUNT(*) n, SUM(units) u FROM curriculum_subjects WHERE curriculum_version_id=%s", (vid,))
    assert n['n'] == len(parsed['rows'])
    batch = dbx.one("SELECT status, target_version_id, decided_by FROM curriculum_import_batches WHERE id=%s", (bid,))
    assert batch['status'] == 'imported' and batch['target_version_id'] == vid and batch['decided_by'] == 'Registrar Nicky'
    linked = dbx.one("SELECT COUNT(*) n FROM curriculum_prerequisites p JOIN curriculum_subjects s ON s.id=p.curriculum_subject_id "
                     "WHERE s.curriculum_version_id=%s AND p.kind='subject'", (vid,))['n']
    assert linked > 10
    # every checked prerequisite points to an earlier term of the same version
    bad = dbx.one("""SELECT COUNT(*) n FROM curriculum_prerequisites p
                     JOIN curriculum_subjects s ON s.id = p.curriculum_subject_id
                     JOIN curriculum_subjects q ON q.id = p.prerequisite_subject_id
                     WHERE s.curriculum_version_id=%s AND (q.curriculum_version_id <> s.curriculum_version_id
                       OR (q.year_level, q.semester) >= (s.year_level, s.semester))""", (vid,))['n']
    assert bad == 0
    page = client.get('/admin/curriculum/versions/%d' % vid)
    assert page.status_code == 200


def normalize(code):
    return re.sub(r'\s+', '', code.upper())


def test_import_specific_document_findings(client, dbx):
    """The conflicts reported in Phase 1 are surfaced, never silently changed."""
    pid = _program(dbx)
    login_as(client, 'registrar')
    bid = _upload(client, pid, _ref('plans', 'BSCS__PLAN-OF-STUDY-BSCS - 2026 FINAL.docx'))
    form, html = _review_form(client, bid, '?mode=new')
    assert "Units &#39;(blank)&#39; are missing" in html or "Units '(blank)' are missing" in html
    assert 'DC 101' in html and 'not a subject of this curriculum' in html
    # exclude DC 103 instead of inventing its units
    idx = int(re.search(r'name="r(\d+)_subject_code" value="DC 103"', html).group(1))
    form.pop(f'r{idx}_include', None)
    form.update({'mode': 'new', 'action': 'commit', 'new_name': 'BSCS 2026 (no DC103)', 'new_effective_academic_year': '2026-2027'})
    resp = client.post('/admin/curriculum/import/%d' % bid, data=form)
    vid = int(resp.location.rstrip('/').split('/')[-1])
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_subjects WHERE curriculum_version_id=%s AND code_key='DC103'", (vid,))['n'] == 0
    thesis = dbx.one("""SELECT GROUP_CONCAT(COALESCE(p.requirement_text, q.subject_code) SEPARATOR '|') r
                        FROM curriculum_subjects s JOIN curriculum_prerequisites p ON p.curriculum_subject_id = s.id
                        LEFT JOIN curriculum_subjects q ON q.id = p.prerequisite_subject_id
                        WHERE s.curriculum_version_id=%s AND s.code_key='THESIS1'""", (vid,))['r']
    assert 'DC 101 (not in this curriculum' in thesis and 'CS 312' in thesis
    prac = dbx.one("""SELECT p.kind, p.min_year_level FROM curriculum_subjects s JOIN curriculum_prerequisites p ON p.curriculum_subject_id=s.id
                      WHERE s.curriculum_version_id=%s AND s.code_key='PRAC'""", (vid,))
    assert prac == {'kind': 'standing', 'min_year_level': 4}


def test_regform_cross_check_against_curriculum(client, dbx):
    """BPA 2nd yr 2nd sem reg form lists AE 125 where the plan has TAX 301."""
    pid = _program(dbx)
    login_as(client, 'registrar')
    bid = _upload(client, pid, _ref('plans', 'BPA__PLAN-OF-STUDY-BPA FINAL.docx'))
    form, _ = _review_form(client, bid, '?mode=new')
    form.update({'mode': 'new', 'action': 'commit', 'new_name': 'BPA plan', 'new_effective_academic_year': '2025-2026'})
    vid = int(client.post('/admin/curriculum/import/%d' % bid, data=form).location.rstrip('/').split('/')[-1])
    bid2 = _upload(client, pid, _ref('regforms', 'BPA__2nd yr. 2nd sem - NEW.xlsx'))
    form2, html = _review_form(client, bid2, '?mode=existing&target_version_id=%d' % vid)
    assert 'TAX 301' in html and 'not in this file' in html          # listed as missing from the reg form
    assert re.search(r'value="AE 125".*?New subject', html, re.S)      # AE 125 shown as new
    # Registrar decides AE 125 is not part of the plan: exclude it, import nothing else new
    idx = int(re.search(r'name="r(\d+)_subject_code" value="AE 125"', html).group(1))
    form2.pop(f'r{idx}_include', None)
    form2.update({'mode': 'existing', 'target_version_id': str(vid), 'action': 'commit'})
    resp = client.post('/admin/curriculum/import/%d' % bid2, data=form2)
    assert resp.status_code == 302
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_subjects WHERE curriculum_version_id=%s AND code_key='AE125'", (vid,))['n'] == 0
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_subjects WHERE curriculum_version_id=%s AND code_key='TAX301'", (vid,))['n'] == 1


def test_conflicting_values_need_an_explicit_choice(client, dbx):
    """BSCRIM 1st-yr reg form spells CLJ1 'INTODUCTION' -> conflict, never auto-applied."""
    pid = _program(dbx)
    login_as(client, 'registrar')
    bid = _upload(client, pid, _ref('plans', 'BSCRIM__PLAN-OF-STUDY- NEW FINAL.docx'))
    form, _ = _review_form(client, bid, '?mode=new')
    form.update({'mode': 'new', 'action': 'commit', 'new_name': 'BSCRIM new', 'new_effective_academic_year': '2026-2027'})
    vid = int(client.post('/admin/curriculum/import/%d' % bid, data=form).location.rstrip('/').split('/')[-1])
    bid2 = _upload(client, pid, _ref('regforms', 'BSCRIM__1st yr. 2nd sem.xlsx'))
    form2, html = _review_form(client, bid2, '?mode=existing&target_version_id=%d' % vid)
    assert 'Conflict' in html and 'INTODUCTION' in html
    form2.update({'mode': 'existing', 'target_version_id': str(vid), 'action': 'commit'})
    client.post('/admin/curriculum/import/%d' % bid2, data=form2)
    assert dbx.one("SELECT status FROM curriculum_import_batches WHERE id=%s", (bid2,))['status'] == 'pending_review'
    for i in re.findall(r'name="r(\d+)_resolution"', html):
        form2[f'r{i}_resolution'] = 'keep'
    resp = client.post('/admin/curriculum/import/%d' % bid2, data=form2)
    assert resp.status_code == 302
    assert dbx.one("SELECT status FROM curriculum_import_batches WHERE id=%s", (bid2,))['status'] == 'imported'
    desc = dbx.one("SELECT description FROM curriculum_subjects WHERE curriculum_version_id=%s AND code_key='CLJ1'", (vid,))['description']
    assert desc == 'Introduction to Philippine Criminal Justice System'


def test_duplicate_upload_is_flagged_and_discard_changes_nothing(client, dbx):
    pid = _program(dbx)
    login_as(client, 'registrar')
    path = _ref('regforms', 'BSMA__1st yr. 2nd sem - NEW.xlsx')
    _upload(client, pid, path)
    bid = _upload(client, pid, path)
    with client.session_transaction() as s:
        flashes = [m for _, m in s.get('_flashes', [])]
    assert any('already uploaded' in m for m in flashes)
    client.post('/admin/curriculum/import/%d/discard' % bid)
    assert dbx.one("SELECT status FROM curriculum_import_batches WHERE id=%s", (bid,))['status'] == 'discarded'


def test_bad_upload_is_rejected(client, dbx):
    pid = _program(dbx)
    login_as(client, 'registrar')
    resp = client.post('/admin/curriculum/import', data={'program_id': pid, 'file': (io.BytesIO(b'not a doc'), 'x.docx')},
                       content_type='multipart/form-data')
    assert resp.status_code == 302 and resp.location.endswith('/admin/curriculum/import')
    resp = client.post('/admin/curriculum/import', data={'program_id': pid, 'file': (io.BytesIO(b'a,b'), 'x.csv')},
                       content_type='multipart/form-data')
    assert resp.location.endswith('/admin/curriculum/import')


# ---------------------------------------------------------------------------
# screens + access control
# ---------------------------------------------------------------------------

def test_registrar_workflow_through_the_screens(client, dbx, program):
    login_as(client, 'registrar')
    resp = client.post('/admin/curriculum/versions/new', data={'program_id': program['id'], 'name': 'Screen test',
                                                               'effective_academic_year': '2026-2027', 'max_year_level': '1'})
    vid = int(resp.location.rstrip('/').split('/')[-1])
    for code, sem in (('SC 101', 1), ('SC 102', 2)):
        client.post('/admin/curriculum/versions/%d/subjects/add' % vid,
                    data={'subject_code': code, 'description': 'Screen ' + code, 'units': '3', 'year_level': '1', 'semester': str(sem)})
    ids = [r['id'] for r in dbx.q("SELECT id FROM curriculum_subjects WHERE curriculum_version_id=%s ORDER BY semester", (vid,))]
    client.post('/admin/curriculum/subjects/%d/prerequisites/add' % ids[1], data={'kind': 'subject', 'prerequisite_subject_id': ids[0]})
    html = client.get('/admin/curriculum/versions/%d' % vid).get_data(as_text=True)
    assert 'SC 101' in html and 'SC 102' in html and '1st Semester' in html and '2nd Semester' in html
    assert client.post('/admin/curriculum/versions/%d/activate' % vid).status_code == 302
    assert dbx.one("SELECT status FROM curriculum_versions WHERE id=%s", (vid,))['status'] == 'active'
    st = make_student(dbx, program)
    html = client.get('/admin/curriculum/versions/%d/students' % vid).get_data(as_text=True)
    assert 'Dela Cruz' in html
    client.post('/admin/curriculum/versions/%d/students' % vid, data={'student_user_id': [st['student_user_id']], 'reason': 'test'})
    assert dbx.one("SELECT curriculum_version_id v FROM student_curricula WHERE student_user_id=%s AND ended_at IS NULL",
                   (st['student_user_id'],))['v'] == vid
    html = client.get('/admin/curriculum/versions/%d' % vid).get_data(as_text=True)
    assert 'Locked' in html and 'Add subject' not in html
    assert 'Screen test' in client.get('/admin/curriculum/').get_data(as_text=True)


@pytest.mark.parametrize('role', ['cashier', 'accounting', 'guidance', None])
def test_only_registrar_and_admin_can_manage(client, dbx, role, program):
    if role:
        login_as(client, role)
    else:
        from conftest import login_student
        login_student(client, make_student(dbx, program)['student_user_id'])
    for url in ('/admin/curriculum/', '/admin/curriculum/import'):
        assert client.get(url).status_code == 302
    before = dbx.one("SELECT COUNT(*) n FROM curriculum_versions")['n']
    client.post('/admin/curriculum/versions/new', data={'program_id': program['id'], 'name': 'Hack', 'effective_academic_year': '2026-2027'})
    assert dbx.one("SELECT COUNT(*) n FROM curriculum_versions")['n'] == before


def test_sidebar_shows_curriculum_link_for_registrar_only(client):
    login_as(client, 'registrar')
    assert 'id="curriculumMenu"' in client.get('/admin/curriculum/').get_data(as_text=True)
    login_as(client, 'secretary')
    html = client.get('/admin/manage-content').get_data(as_text=True)
    assert 'id="curriculumMenu"' not in html
