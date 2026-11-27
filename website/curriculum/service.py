"""Curriculum management business rules (Phase 2).

Everything that changes curriculum data goes through this module so the
validation rules live in one place and every change is audited. Routes only
translate HTTP to these calls. All functions work inside the caller's
SQLAlchemy session; `commit()` is called by the route once a whole action
succeeded, `rollback()` when a CurriculumError is raised.

Rules (from the master prompt, Phase 2 section E):
  * a subject code appears once per curriculum version (duplicates refused)
  * year level 1..version.max_year_level, semester 1, 2 or 3 (= Summer)
  * a subject prerequisite must be another subject of the SAME version that
    is scheduled in an EARLIER term (this also rules out cycles)
  * a version that students follow (or that is archived) is LOCKED: subjects,
    units and prerequisites can no longer change; clone it instead. This is
    what keeps historical registrations and grades on the curriculum they
    were taken under.
  * activating a version with gaps (empty terms, 0-unit subjects, free-text
    prerequisites) needs an explicit confirmation and a note
"""

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import bindparam, func, text

from website import db
from website.models import (
    CurriculumVersion, CurriculumSubject, CurriculumPrerequisite, StudentCurriculum,
    CurriculumAuditLog, CurriculumImportBatch, Program, Subject, SEMESTER_LABELS, YEAR_LABELS,
)

MAX_UNITS_PER_SUBJECT = Decimal('30')
PREREQ_KINDS = ('subject', 'standing', 'units', 'other')


class CurriculumError(Exception):
    """A rule was broken; `messages` are shown to the registrar."""

    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


class Actor:
    def __init__(self, name, role):
        self.name = name or 'unknown'
        self.role = role or ''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def normalize_code(code):
    return re.sub(r'\s+', '', (code or '').upper())


def clean_code(code):
    """Official spelling kept, whitespace tidied: ' ge  105 ' -> 'GE 105'."""
    return re.sub(r'\s+', ' ', (code or '').strip()).upper()


def parse_units(value):
    if value is None or str(value).strip() == '':
        raise CurriculumError('Units are required.')
    try:
        units = Decimal(str(value).strip())
    except InvalidOperation:
        raise CurriculumError(f"Units '{value}' is not a number.")
    if units < 0 or units > MAX_UNITS_PER_SUBJECT:
        raise CurriculumError(f'Units must be between 0 and {MAX_UNITS_PER_SUBJECT}.')
    if units != units.quantize(Decimal('0.1')):
        raise CurriculumError('Units can have at most one decimal place.')
    return units.quantize(Decimal('0.1'))


def term_order(year_level, semester):
    """Sort key: 1st sem < 2nd sem < Summer within a year."""
    return (int(year_level), int(semester))


def term_label(year_level, semester):
    return f"{YEAR_LABELS.get(year_level, f'Year {year_level}')} - {SEMESTER_LABELS.get(semester, semester)}"


def audit(version_id, entity, entity_id, action, details, actor):
    db.session.add(CurriculumAuditLog(
        curriculum_version_id=version_id, entity=entity, entity_id=entity_id, action=action,
        details=json.dumps(details, default=str, ensure_ascii=False) if details is not None else None,
        performed_by=actor.name, performed_role=actor.role, performed_at=datetime.now()))


def _lock_version(version_id):
    """Row-lock the version so two registrars cannot change it at the same time."""
    v = db.session.query(CurriculumVersion).filter_by(id=version_id).with_for_update().first()
    if v is None:
        raise CurriculumError('Curriculum version not found.')
    return v


def students_on_version(version_id, current_only=False):
    q = db.session.query(func.count(StudentCurriculum.id)).filter(StudentCurriculum.curriculum_version_id == version_id)
    if current_only:
        q = q.filter(StudentCurriculum.ended_at.is_(None))
    return q.scalar() or 0


def lock_reason(version):
    """Why structural changes are refused, or None when the version is editable."""
    if version.status == 'archived':
        return 'This curriculum is archived. Clone it to make changes.'
    n = students_on_version(version.id)
    if n:
        return (f'{n} student assignment(s) reference this curriculum, so its subjects, units and '
                f'prerequisites are locked to protect their records. Clone it to make a new version.')
    return None


def _require_editable(version):
    reason = lock_reason(version)
    if reason:
        raise CurriculumError(reason)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

_AY = re.compile(r'^(\d{4})-(\d{4})$')


def _validate_version_fields(name, effective_academic_year, max_year_level):
    errors = []
    if not (name or '').strip():
        errors.append('Curriculum name is required.')
    elif len(name.strip()) > 150:
        errors.append('Curriculum name is too long (150 characters max).')
    m = _AY.match((effective_academic_year or '').strip())
    if not m or int(m.group(2)) != int(m.group(1)) + 1:
        errors.append("Effective academic year must look like '2026-2027'.")
    try:
        my = int(max_year_level)
        if my < 1 or my > 5:
            raise ValueError
    except (TypeError, ValueError):
        errors.append('Number of year levels must be between 1 and 5.')
    if errors:
        raise CurriculumError(*errors)


def create_version(program_id, name, effective_academic_year, actor, description=None,
                   max_year_level=4, source_reference=None):
    program = db.session.get(Program, program_id)
    if program is None:
        raise CurriculumError('Program not found.')
    _validate_version_fields(name, effective_academic_year, max_year_level)
    name = name.strip()
    if CurriculumVersion.query.filter_by(program_id=program_id, name=name).first():
        raise CurriculumError(f"{program.program_code or program_id.upper()} already has a curriculum named '{name}'.")
    v = CurriculumVersion(program_id=program_id, name=name,
                          effective_academic_year=effective_academic_year.strip(),
                          description=(description or '').strip() or None, status='draft',
                          max_year_level=int(max_year_level), source_reference=source_reference,
                          created_by=actor.name, created_at=datetime.now())
    db.session.add(v)
    db.session.flush()
    audit(v.id, 'version', v.id, 'create', {'name': name, 'effective_academic_year': v.effective_academic_year,
                                             'program_id': program_id}, actor)
    return v


def update_version(version_id, actor, name, effective_academic_year, description, max_year_level):
    v = _lock_version(version_id)
    _validate_version_fields(name, effective_academic_year, max_year_level)
    if v.status == 'archived':
        raise CurriculumError('This curriculum is archived and cannot be edited.')
    name = name.strip()
    clash = CurriculumVersion.query.filter(CurriculumVersion.program_id == v.program_id,
                                           CurriculumVersion.name == name, CurriculumVersion.id != v.id).first()
    if clash:
        raise CurriculumError(f"Another curriculum of this program is already named '{name}'.")
    new_max = int(max_year_level)
    if new_max != v.max_year_level:
        _require_editable(v)
        beyond = v.subjects.filter(CurriculumSubject.year_level > new_max).count()
        if beyond:
            raise CurriculumError(f'{beyond} subject(s) are in a year level above {new_max}. Move or remove them first.')
    before = {'name': v.name, 'effective_academic_year': v.effective_academic_year,
              'description': v.description, 'max_year_level': v.max_year_level}
    v.name, v.effective_academic_year = name, effective_academic_year.strip()
    v.description, v.max_year_level = (description or '').strip() or None, new_max
    audit(v.id, 'version', v.id, 'update', {'before': before, 'after': {
        'name': v.name, 'effective_academic_year': v.effective_academic_year,
        'description': v.description, 'max_year_level': v.max_year_level}}, actor)
    return v


def completeness(version):
    """{'blocking': [...], 'warnings': [...]} for the activation check."""
    subjects = version.subjects.all()
    blocking, warnings = [], []
    if not subjects:
        blocking.append('The curriculum has no subjects.')
        return {'blocking': blocking, 'warnings': warnings}
    present = {(s.year_level, s.semester) for s in subjects}
    for y in range(1, version.max_year_level + 1):
        for sem in (1, 2):
            if (y, sem) not in present:
                warnings.append(f'{term_label(y, sem)} has no subjects.')
    zero = [s.subject_code for s in subjects if s.units is not None and Decimal(s.units) == 0]
    if zero:
        warnings.append('Subjects with 0 units: ' + ', '.join(zero) + '.')
    other = [f"{p.subject.subject_code} ({p.requirement_text})" for s in subjects for p in s.prerequisites if p.kind == 'other']
    if other:
        warnings.append('Prerequisites kept as text that the system cannot check automatically: ' + '; '.join(other) + '.')
    return {'blocking': blocking, 'warnings': warnings}


def link_legacy_subjects(version, actor=None):
    """Point curriculum subjects to the legacy `subjects` row (same program, same
    code ignoring spaces) so grade history can be matched. Never creates or
    changes legacy subjects. Returns the number of new links."""
    legacy = {}
    for s in Subject.query.filter_by(program_id=version.program_id).all():
        legacy.setdefault(normalize_code(s.subject_code), s.id)
    linked = 0
    for cs in version.subjects.filter(CurriculumSubject.subject_id.is_(None)).all():
        sid = legacy.get(cs.code_key)
        if sid:
            cs.subject_id = sid
            linked += 1
    if linked and actor:
        audit(version.id, 'version', version.id, 'link_legacy_subjects', {'linked': linked}, actor)
    return linked


def activate_version(version_id, actor, confirm_incomplete=False, note=None):
    v = _lock_version(version_id)
    if v.status == 'active':
        raise CurriculumError('This curriculum is already active.')
    report = completeness(v)
    if report['blocking']:
        raise CurriculumError(*report['blocking'])
    if report['warnings'] and not (confirm_incomplete and (note or '').strip()):
        raise CurriculumError('This curriculum is incomplete. Tick the confirmation and give a reason to activate it anyway.',
                              *report['warnings'])
    previous = v.status
    v.status = 'active'
    v.activated_by, v.activated_at = actor.name, datetime.now()
    v.activation_note = (note or '').strip() or None
    linked = link_legacy_subjects(v)
    audit(v.id, 'version', v.id, 'activate', {'from': previous, 'warnings': report['warnings'],
                                               'note': v.activation_note, 'legacy_links': linked}, actor)
    return v


def archive_version(version_id, actor, note=None):
    v = _lock_version(version_id)
    if v.status == 'archived':
        raise CurriculumError('This curriculum is already archived.')
    if v.status == 'draft':
        raise CurriculumError('A draft is not used by anyone. Delete it instead of archiving it.')
    current = students_on_version(v.id, current_only=True)
    was_default = v.is_default_for_new
    v.status, v.is_default_for_new = 'archived', False
    audit(v.id, 'version', v.id, 'archive', {'students_still_assigned': current, 'was_default': was_default,
                                              'note': (note or '').strip() or None}, actor)
    return v, current


def set_default_version(version_id, actor):
    v = _lock_version(version_id)
    if v.status != 'active':
        raise CurriculumError('Only an active curriculum can be the default for new students.')
    others = CurriculumVersion.query.filter(CurriculumVersion.program_id == v.program_id,
                                            CurriculumVersion.id != v.id,
                                            CurriculumVersion.is_default_for_new.is_(True)).with_for_update().all()
    for o in others:
        o.is_default_for_new = False
    v.is_default_for_new = True
    audit(v.id, 'version', v.id, 'set_default', {'replaced': [o.id for o in others]}, actor)
    return v


def delete_version(version_id, actor):
    v = _lock_version(version_id)
    if v.status != 'draft':
        raise CurriculumError('Only a draft curriculum can be deleted. Archive an active one instead.')
    if students_on_version(v.id):
        raise CurriculumError('Students are assigned to this curriculum; it cannot be deleted.')
    ids = [s.id for s in v.subjects.all()]
    if ids:
        CurriculumPrerequisite.query.filter(CurriculumPrerequisite.curriculum_subject_id.in_(ids)).delete(synchronize_session=False)
        CurriculumSubject.query.filter(CurriculumSubject.id.in_(ids)).delete(synchronize_session=False)
    CurriculumImportBatch.query.filter_by(target_version_id=v.id).update({'target_version_id': None}, synchronize_session=False)
    audit(v.id, 'version', v.id, 'delete', {'name': v.name, 'subjects': len(ids)}, actor)
    db.session.delete(v)


def clone_version(version_id, actor, name, effective_academic_year, description=None):
    src = db.session.get(CurriculumVersion, version_id)
    if src is None:
        raise CurriculumError('Curriculum version not found.')
    new = create_version(src.program_id, name, effective_academic_year, actor,
                         description=description if description is not None else src.description,
                         max_year_level=src.max_year_level, source_reference=src.source_reference)
    mapping = {}
    for s in src.subjects.all():
        c = CurriculumSubject(curriculum_version_id=new.id, year_level=s.year_level, semester=s.semester,
                              subject_code=s.subject_code, code_key=s.code_key, description=s.description,
                              units=s.units, sort_order=s.sort_order, subject_id=s.subject_id, notes=s.notes)
        db.session.add(c)
        db.session.flush()
        mapping[s.id] = c.id
    for s in src.subjects.all():
        for p in s.prerequisites:
            db.session.add(CurriculumPrerequisite(
                curriculum_subject_id=mapping[s.id], kind=p.kind,
                prerequisite_subject_id=mapping.get(p.prerequisite_subject_id) if p.prerequisite_subject_id else None,
                min_year_level=p.min_year_level, min_units=p.min_units, requirement_text=p.requirement_text))
    audit(new.id, 'version', new.id, 'clone', {'from_version': src.id, 'subjects': len(mapping)}, actor)
    return new


# ---------------------------------------------------------------------------
# subjects
# ---------------------------------------------------------------------------

def _validate_subject(version, code, description, units, year_level, semester, exclude_id=None):
    errors = []
    code_c = clean_code(code)
    key = normalize_code(code)
    if not key:
        errors.append('Subject code is required.')
    elif len(code_c) > 30:
        errors.append('Subject code is too long (30 characters max).')
    desc = re.sub(r'\s+', ' ', (description or '').strip())
    if not desc:
        errors.append('Subject description is required.')
    elif len(desc) > 500:
        errors.append('Subject description is too long.')
    try:
        u = parse_units(units)
    except CurriculumError as e:
        errors.extend(e.messages)
        u = None
    try:
        y = int(year_level)
        if not 1 <= y <= version.max_year_level:
            errors.append(f'Year level must be between 1 and {version.max_year_level} for this curriculum.')
    except (TypeError, ValueError):
        errors.append('Year level is required.')
        y = None
    try:
        sem = int(semester)
        if sem not in SEMESTER_LABELS:
            errors.append('Semester must be 1st Semester, 2nd Semester or Summer.')
    except (TypeError, ValueError):
        errors.append('Semester is required.')
        sem = None
    if key:
        q = CurriculumSubject.query.filter_by(curriculum_version_id=version.id, code_key=key)
        if exclude_id:
            q = q.filter(CurriculumSubject.id != exclude_id)
        dup = q.first()
        if dup:
            errors.append(f'{dup.subject_code} is already in this curriculum ({term_label(dup.year_level, dup.semester)}).')
    if errors:
        raise CurriculumError(*errors)
    return code_c, key, desc, u, y, sem


def add_subject(version_id, actor, code, description, units, year_level, semester, notes=None):
    v = _lock_version(version_id)
    _require_editable(v)
    code_c, key, desc, u, y, sem = _validate_subject(v, code, description, units, year_level, semester)
    order = (db.session.query(func.max(CurriculumSubject.sort_order))
             .filter_by(curriculum_version_id=v.id, year_level=y, semester=sem).scalar() or 0) + 1
    s = CurriculumSubject(curriculum_version_id=v.id, year_level=y, semester=sem, subject_code=code_c, code_key=key,
                          description=desc, units=u, sort_order=order, notes=(notes or '').strip() or None)
    db.session.add(s)
    db.session.flush()
    audit(v.id, 'subject', s.id, 'create', {'code': code_c, 'description': desc, 'units': u,
                                             'term': term_label(y, sem)}, actor)
    return s


def update_subject(subject_id, actor, code, description, units, year_level, semester, notes=None):
    s = db.session.get(CurriculumSubject, subject_id)
    if s is None:
        raise CurriculumError('Subject not found.')
    v = _lock_version(s.curriculum_version_id)
    _require_editable(v)
    code_c, key, desc, u, y, sem = _validate_subject(v, code, description, units, year_level, semester, exclude_id=s.id)
    # Moving a subject must keep every prerequisite chain in order.
    new_order = term_order(y, sem)
    problems = []
    for p in s.prerequisites:
        if p.kind == 'subject' and p.prerequisite and term_order(p.prerequisite.year_level, p.prerequisite.semester) >= new_order:
            problems.append(f'its prerequisite {p.prerequisite.subject_code} would no longer come before it')
    for dep in CurriculumPrerequisite.query.filter_by(prerequisite_subject_id=s.id).all():
        if term_order(dep.subject.year_level, dep.subject.semester) <= new_order:
            problems.append(f'{dep.subject.subject_code} requires it and would no longer come after it')
    if problems:
        raise CurriculumError(f'Cannot move {code_c} to {term_label(y, sem)}: ' + '; '.join(problems) + '.')
    before = {'code': s.subject_code, 'description': s.description, 'units': s.units,
              'term': term_label(s.year_level, s.semester)}
    if key != s.code_key:
        s.subject_id = None   # the legacy link was by code
    s.subject_code, s.code_key, s.description, s.units = code_c, key, desc, u
    if (s.year_level, s.semester) != (y, sem):
        s.year_level, s.semester = y, sem
    s.notes = (notes or '').strip() or None
    audit(v.id, 'subject', s.id, 'update', {'before': before, 'after': {
        'code': code_c, 'description': desc, 'units': u, 'term': term_label(y, sem)}}, actor)
    return s


def _legacy_history_count(subject_id):
    if not subject_id:
        return 0
    row = db.session.execute(text(
        "SELECT (SELECT COUNT(*) FROM student_grades WHERE subject_id = :sid) + "
        "(SELECT COUNT(*) FROM grades WHERE subject_id = :sid)"), {'sid': subject_id}).scalar()
    return int(row or 0)


def delete_subject(subject_id, actor):
    s = db.session.get(CurriculumSubject, subject_id)
    if s is None:
        raise CurriculumError('Subject not found.')
    v = _lock_version(s.curriculum_version_id)
    _require_editable(v)
    if v.status == 'active':
        history = _legacy_history_count(s.subject_id)
        if history:
            raise CurriculumError(f'{s.subject_code} has {history} grade record(s) in the system and this curriculum is '
                                  f'active, so it cannot be removed. Clone the curriculum to drop it from a new version.')
    dependants = CurriculumPrerequisite.query.filter_by(prerequisite_subject_id=s.id).all()
    if dependants:
        raise CurriculumError(f'{s.subject_code} is a prerequisite of '
                              + ', '.join(sorted(d.subject.subject_code for d in dependants))
                              + '. Remove those prerequisites first.')
    CurriculumPrerequisite.query.filter_by(curriculum_subject_id=s.id).delete(synchronize_session=False)
    audit(v.id, 'subject', s.id, 'delete', {'code': s.subject_code, 'description': s.description,
                                             'units': s.units, 'term': term_label(s.year_level, s.semester)}, actor)
    db.session.delete(s)


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------

def add_prerequisite(subject_id, actor, kind, prerequisite_subject_id=None, min_year_level=None,
                     min_units=None, requirement_text=None):
    s = db.session.get(CurriculumSubject, subject_id)
    if s is None:
        raise CurriculumError('Subject not found.')
    v = _lock_version(s.curriculum_version_id)
    _require_editable(v)
    if kind not in PREREQ_KINDS:
        raise CurriculumError('Unknown prerequisite type.')
    p = CurriculumPrerequisite(curriculum_subject_id=s.id, kind=kind)
    if kind == 'subject':
        try:
            pre = db.session.get(CurriculumSubject, int(prerequisite_subject_id))
        except (TypeError, ValueError):
            pre = None
        if pre is None or pre.curriculum_version_id != s.curriculum_version_id:
            raise CurriculumError('The prerequisite must be a subject of the same curriculum.')
        if pre.id == s.id:
            raise CurriculumError('A subject cannot be its own prerequisite.')
        if term_order(pre.year_level, pre.semester) >= term_order(s.year_level, s.semester):
            raise CurriculumError(f'{pre.subject_code} ({term_label(pre.year_level, pre.semester)}) is not scheduled '
                                  f'before {s.subject_code} ({term_label(s.year_level, s.semester)}).')
        if CurriculumPrerequisite.query.filter_by(curriculum_subject_id=s.id, prerequisite_subject_id=pre.id).first():
            raise CurriculumError(f'{pre.subject_code} is already a prerequisite of {s.subject_code}.')
        p.prerequisite_subject_id = pre.id
        detail = pre.subject_code
    elif kind == 'standing':
        try:
            y = int(min_year_level)
        except (TypeError, ValueError):
            y = 0
        if not 1 <= y <= v.max_year_level:
            raise CurriculumError(f'Year standing must be between 1 and {v.max_year_level}.')
        p.min_year_level = y
        detail = f'{y} year standing'
    elif kind == 'units':
        try:
            u = Decimal(str(min_units).strip())
        except (InvalidOperation, AttributeError):
            u = None
        if u is None or u <= 0 or u > 400:
            raise CurriculumError('Required completed units must be between 1 and 400.')
        p.min_units = u
        detail = f'{u} units'
    else:
        t = re.sub(r'\s+', ' ', (requirement_text or '').strip())
        if not t:
            raise CurriculumError('Describe the requirement.')
        p.requirement_text = t[:255]
        detail = t
    if kind != 'other' and requirement_text:
        p.requirement_text = re.sub(r'\s+', ' ', requirement_text.strip())[:255]
    db.session.add(p)
    db.session.flush()
    audit(v.id, 'prerequisite', p.id, 'create', {'subject': s.subject_code, 'kind': kind, 'requirement': detail}, actor)
    return p


def delete_prerequisite(prereq_id, actor):
    p = db.session.get(CurriculumPrerequisite, prereq_id)
    if p is None:
        raise CurriculumError('Prerequisite not found.')
    v = _lock_version(p.subject.curriculum_version_id)
    _require_editable(v)
    audit(v.id, 'prerequisite', p.id, 'delete', {'subject': p.subject.subject_code, 'requirement': p.label()}, actor)
    db.session.delete(p)


# ---------------------------------------------------------------------------
# students
# ---------------------------------------------------------------------------

def current_assignment(student_user_id):
    return (StudentCurriculum.query.filter_by(student_user_id=student_user_id, ended_at=None)
            .order_by(StudentCurriculum.id.desc()).first())


def resolve_student_curriculum(student_user_id, program_id):
    """The curriculum a student follows: their explicit assignment when it is for
    the given program, otherwise the program's default curriculum for new students.
    Returns (version or None, 'assigned' | 'default' | None)."""
    a = current_assignment(student_user_id)
    if a and a.version and a.version.program_id == program_id:
        return a.version, 'assigned'
    d = CurriculumVersion.query.filter_by(program_id=program_id, status='active', is_default_for_new=True).first()
    return (d, 'default') if d else (None, None)


def assignment_candidates(program, year_level=None, statuses=None):
    """Latest application of every student in the program, with their current curriculum."""
    sql = """
        SELECT a.applicant_id, a.student_user_id, a.first_name, a.last_name, a.middle_name, a.old_student_id,
               a.control_number, a.enrollment_year_level, a.application_status, a.academic_year,
               sc.curriculum_version_id AS current_version_id, cv.name AS current_version_name
        FROM applicants a
        JOIN (SELECT student_user_id, MAX(applicant_id) AS latest FROM applicants GROUP BY student_user_id) l
             ON l.latest = a.applicant_id
        LEFT JOIN student_curricula sc ON sc.student_user_id = a.student_user_id AND sc.ended_at IS NULL
        LEFT JOIN curriculum_versions cv ON cv.id = sc.curriculum_version_id
        WHERE TRIM(a.program_choice) = TRIM(:title)
    """
    params = {'title': program.title or ''}
    if year_level:
        sql += " AND a.enrollment_year_level = :yl"
        params['yl'] = year_level
    if statuses:
        keys = []
        for i, st in enumerate(statuses):
            keys.append(f':st{i}')
            params[f'st{i}'] = st
        sql += f" AND a.application_status IN ({', '.join(keys)})"
    sql += " ORDER BY a.last_name, a.first_name"
    return [dict(r._mapping) for r in db.session.execute(text(sql), params)]


def assign_students(version_id, student_user_ids, actor, reason=None):
    v = _lock_version(version_id)
    if v.status != 'active':
        raise CurriculumError('Students can only be assigned to an active curriculum.')
    ids = sorted({int(i) for i in student_user_ids if str(i).strip().isdigit()})
    if not ids:
        raise CurriculumError('Select at least one student.')
    program_title = v.program.title or ''
    # Only students whose latest application is for this curriculum's program.
    rows = db.session.execute(text("""
        SELECT a.student_user_id FROM applicants a
        JOIN (SELECT student_user_id, MAX(applicant_id) AS latest FROM applicants GROUP BY student_user_id) l
             ON l.latest = a.applicant_id
        WHERE a.student_user_id IN :ids AND TRIM(a.program_choice) = TRIM(:title)
    """).bindparams(bindparam('ids', expanding=True)), {'ids': ids, 'title': program_title}).fetchall()
    eligible = {r[0] for r in rows}
    rejected = [i for i in ids if i not in eligible]
    if rejected:
        raise CurriculumError(f'{len(rejected)} selected student(s) are not in {v.program.program_code or v.program_id}.')
    # Lock the students' current rows so concurrent assignments serialise.
    current = {c.student_user_id: c for c in StudentCurriculum.query.filter(
        StudentCurriculum.student_user_id.in_(ids), StudentCurriculum.ended_at.is_(None)).with_for_update().all()}
    now = datetime.now()
    assigned, unchanged = 0, 0
    for sid in ids:
        cur = current.get(sid)
        if cur and cur.curriculum_version_id == v.id:
            unchanged += 1
            continue
        if cur:
            cur.ended_at = now
        db.session.add(StudentCurriculum(student_user_id=sid, curriculum_version_id=v.id, assigned_by=actor.name,
                                         assigned_at=now, reason=(reason or '').strip()[:255] or None))
        assigned += 1
    audit(v.id, 'version', v.id, 'assign_students', {'assigned': assigned, 'unchanged': unchanged,
                                                      'student_user_ids': ids, 'reason': reason}, actor)
    return assigned, unchanged


# ---------------------------------------------------------------------------
# read models for the UI
# ---------------------------------------------------------------------------

def grouped_subjects(version):
    """[(year, semester, [subjects], total_units)] in term order."""
    terms = {}
    for s in version.subjects.all():
        terms.setdefault((s.year_level, s.semester), []).append(s)
    out = []
    for (y, sem) in sorted(terms):
        subs = sorted(terms[(y, sem)], key=lambda x: (x.sort_order, x.id))
        out.append((y, sem, subs, sum((Decimal(x.units) for x in subs), Decimal('0'))))
    return out


def version_summary(version):
    subs = version.subjects.all()
    return {
        'subjects': len(subs),
        'units': sum((Decimal(s.units) for s in subs), Decimal('0')),
        'students': students_on_version(version.id, current_only=True),
        'locked': lock_reason(version) is not None,
    }
