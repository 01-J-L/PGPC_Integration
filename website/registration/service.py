"""Registration form (REGFORM) rules - Phase 3.

One form per student per academic term. The registrar builds it from the
student's curriculum (Phase 2):

* Regular students: the subjects of their year level and semester are filled
  in automatically; the registrar only assigns sections.
* Irregular students: the registrar adds/removes subjects one by one from the
  whole curriculum. Every addition is checked against the student's academic
  history, prerequisites, the unit limit, section availability/capacity and
  schedule conflicts.

Academic history source (decision pending Q6): the registrar grade record
`student_grades` (what the student portal and the registrar pages show).
Faculty class-record grades (`grades`) that were never posted are NOT counted.

Unit limit (decision pending Q7): the larger of the term's configured
max_units (academic_terms.max_units) and the curriculum load of the student's
own year/semester, so a regular load is never refused.

Already-passed subjects can never be added (no override until Q8 is decided).
Subject prerequisites that are not passed block the subject. Requirements the
system cannot verify (free-text notes, or a subject taken in a term whose
grade is not recorded yet) need an explicit registrar acknowledgement, which
is logged.
"""

import json
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from website import db
from website.models import (
    AcademicTerm, Applicant, CurriculumSubject, Program, RegistrationForm, RegistrationFormChange,
    RegistrationFormSubject, Section, TeachingLoad, Course, YEAR_LABELS, SEMESTER_LABELS,
)
from website.curriculum import service as curriculum_service
from website.curriculum.service import normalize_code, term_label, term_order

PASSED_REMARKS = {'PASSED', 'PASS', 'P'}
NOT_PASSED_REMARKS = {'FAILED', 'FAIL', 'F', 'INCOMPLETE', 'INC', 'DROPPED', 'DRP', 'DRP.', 'NO GRADE', 'NG', 'W', 'WITHDRAWN'}
DEFAULT_MAX_UNITS = Decimal('26')
DAY_ABBR = {'monday': 'MON', 'tuesday': 'TUE', 'wednesday': 'WED', 'thursday': 'THU', 'friday': 'FRI',
            'saturday': 'SAT', 'sunday': 'SUN'}


class RegformError(Exception):
    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


class NeedsAcknowledgement(RegformError):
    """The action is allowed only after the registrar acknowledges these points."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def year_level_to_int(value):
    m = re.search(r'(\d)', str(value or ''))
    return int(m.group(1)) if m else None


def semester_to_int(value):
    s = str(value or '').lower()
    if 'summer' in s or 'midyear' in s:
        return 3
    if s.startswith('2') or 'second' in s:
        return 2
    if s.startswith('1') or 'first' in s:
        return 1
    return None


def _log(form_id, action, actor, subject_code=None, details=None):
    db.session.add(RegistrationFormChange(
        registration_form_id=form_id, action=action, subject_code=subject_code,
        details=json.dumps(details, default=str, ensure_ascii=False) if details is not None else None,
        performed_by=actor.name, performed_role=actor.role, performed_at=datetime.now()))


def section_label(section):
    code = section.program.program_code if section.program else ''
    return f"{code} {section.name}".strip()


def fmt_units(value):
    d = Decimal(str(value or 0))
    return str(int(d)) if d == d.to_integral_value() else str(d.quantize(Decimal('0.1')))


def _fmt_time(t):
    return t.strftime('%I:%M').lstrip('0') + t.strftime('%p').lower()[0]


# ---------------------------------------------------------------------------
# context: student, term, curriculum, history
# ---------------------------------------------------------------------------

class Context:
    """Everything needed to build or validate one student's REGFORM for a term."""

    def __init__(self, applicant_id, term_id):
        self.applicant = db.session.get(Applicant, applicant_id)
        if self.applicant is None:
            raise RegformError('Student application not found.')
        self.term = db.session.get(AcademicTerm, term_id)
        if self.term is None:
            raise RegformError('Academic term not found.')
        self.student_user_id = self.applicant.student_user_id
        title = (self.applicant.program_choice or '').strip()
        self.program = Program.query.filter(db.func.trim(Program.title) == title).first() if title else None
        if self.program is None:
            raise RegformError(f"The student's program '{title or '(none)'}' does not match any program.")
        self.semester = semester_to_int(self.term.semester)
        if self.semester is None:
            raise RegformError(f"Cannot tell the semester of term '{self.term.semester}'.")
        self.year_level = year_level_to_int(self.applicant.enrollment_year_level)
        if self.year_level is None:
            raise RegformError("The student's year level is not set. Set it on the application first.")
        self.curriculum, self.curriculum_source = curriculum_service.resolve_student_curriculum(
            self.student_user_id, self.program.program_id)
        if self.curriculum is None:
            raise RegformError(f"No curriculum applies to this student: assign one, or set a default curriculum for "
                               f"{self.program.program_code or self.program.program_id}.")
        self.subjects = self.curriculum.subjects.all()
        self.by_key = {s.code_key: s for s in self.subjects}
        self.history = academic_history(self.student_user_id, exclude_term=self.term)
        self.passed_units = sum((h['units'] for h in self.history.values() if h['status'] == 'passed'), Decimal('0'))
        own_term = [s for s in self.subjects if (s.year_level, s.semester) == (self.year_level, self.semester)]
        self.regular_subjects = own_term
        regular_load = sum((Decimal(s.units) for s in own_term), Decimal('0'))
        configured = Decimal(str(self.term.max_units)) if self.term.max_units else DEFAULT_MAX_UNITS
        self.max_units = max(configured, regular_load)

    # --- classification -------------------------------------------------
    def back_subjects(self):
        """Curriculum subjects of EARLIER terms that are not passed yet."""
        mine = term_order(self.year_level, self.semester)
        return [s for s in self.subjects
                if term_order(s.year_level, s.semester) < mine and self.history.get(s.code_key, {}).get('status') != 'passed']

    def suggested_classification(self):
        reasons = []
        back = self.back_subjects()
        if back:
            reasons.append('Back subject(s) from earlier terms: ' + ', '.join(s.subject_code for s in back[:12])
                           + (' ...' if len(back) > 12 else ''))
        back_keys = {s.code_key for s in back}
        unresolved = [h['code'] for k, h in self.history.items()
                      if h['status'] in ('failed', 'incomplete', 'dropped') and k not in back_keys]
        if unresolved:
            reasons.append('Failed/incomplete/dropped: ' + ', '.join(unresolved[:12]))
        if not self.regular_subjects:
            reasons.append(f'The curriculum has no subjects for {term_label(self.year_level, self.semester)}.')
        if reasons:
            return 'Irregular', '; '.join(reasons)
        if not any(term_order(s.year_level, s.semester) < term_order(self.year_level, self.semester) for s in self.subjects):
            return 'Regular', 'First term of the curriculum.'
        return 'Regular', 'Has passed every earlier curriculum subject.'


def academic_history(student_user_id, exclude_term=None):
    """{code_key: {'status','code','units','terms','grade'}} from the registrar grade
    record (student_grades) plus subjects on FINALIZED registration forms of other
    terms that have no grade yet ('in_progress')."""
    rows = db.session.execute(text("""
        SELECT s.subject_code, s.units, sg.grade, sg.remarks, sg.academic_year, sg.semester
        FROM student_grades sg JOIN subjects s ON s.id = sg.subject_id
        WHERE sg.student_user_id = :sid
        ORDER BY sg.academic_year, sg.semester, sg.id
    """), {'sid': student_user_id}).fetchall()
    out = {}
    for code, units, grade, remarks, ay, sem in rows:
        key = normalize_code(code)
        r = (remarks or '').strip().upper()
        if r in PASSED_REMARKS:
            status = 'passed'
        elif r in ('INCOMPLETE', 'INC'):
            status = 'incomplete'
        elif r in ('DROPPED', 'DRP', 'DRP.', 'W', 'WITHDRAWN'):
            status = 'dropped'
        elif r in NOT_PASSED_REMARKS:
            status = 'failed'
        elif grade is not None:
            status = 'passed' if Decimal(grade) <= Decimal('3.0') else 'failed'
        else:
            status = 'no_grade'
        h = out.setdefault(key, {'code': code, 'units': Decimal(units or 0), 'terms': [], 'status': status, 'grade': grade})
        h['terms'].append(f'{ay} {sem}')
        # Rows are in chronological order: the latest attempt wins, except that a
        # pass is never undone by a later non-pass record.
        if h['status'] != 'passed':
            h['status'], h['grade'] = status, grade
    # finalized forms of other terms whose subjects have no grade yet
    q = """SELECT l.code_key, l.subject_code, l.units, t.year_name, t.semester
           FROM registration_form_subjects l JOIN registration_forms f ON f.id = l.registration_form_id
           JOIN academic_terms t ON t.id = f.academic_term_id
           WHERE f.student_user_id = :sid AND f.status = 'finalized'"""
    params = {'sid': student_user_id}
    if exclude_term is not None:
        q += " AND f.academic_term_id <> :tid"
        params['tid'] = exclude_term.id
    for key, code, units, ay, sem in db.session.execute(text(q), params).fetchall():
        if key not in out:
            out[key] = {'code': code, 'units': Decimal(units or 0), 'terms': [f'{ay} {sem}'], 'status': 'in_progress', 'grade': None}
    return out


# ---------------------------------------------------------------------------
# sections & schedules
# ---------------------------------------------------------------------------

def _loads_for_term(term):
    return TeachingLoad.query.filter(TeachingLoad.school_year == term.year_name).all()


def _course_semesters():
    sems = {}
    for c in Course.query.all():
        sems.setdefault(normalize_code(c.course_code), set()).add(c.semester)
    return sems


def _sections_by_label():
    return {section_label(s).upper(): s for s in Section.query.all() if s.program is not None}


class Offerings:
    """Class schedules (teaching_loads) of the term, grouped by subject and section."""

    def __init__(self, term, semester):
        self.term = term
        course_sems = _course_semesters()
        labels = _sections_by_label()
        self.by_key = {}         # code_key -> {section_id: [loads]}
        for load in _loads_for_term(term):
            key = normalize_code(load.course_code)
            sems = course_sems.get(key)
            if sems and semester not in sems:
                continue   # the same school year also holds the other semester's classes
            sec = labels.get((load.section or '').strip().upper())
            if sec is None:
                continue
            self.by_key.setdefault(key, {}).setdefault(sec.id, []).append(load)
        self.sections = {s.id: s for s in labels.values()}

    def sections_for(self, code_key):
        return [self.sections[sid] for sid in self.by_key.get(code_key, {})]

    def loads(self, code_key, section_id):
        return self.by_key.get(code_key, {}).get(section_id, [])

    def schedule_text(self, code_key, section_id):
        loads = sorted(self.loads(code_key, section_id),
                       key=lambda l: (list(DAY_ABBR).index(l.day.lower()) if l.day and l.day.lower() in DAY_ABBR else 9, l.time_start))
        parts = []
        for l in loads:
            day = DAY_ABBR.get((l.day or '').lower(), (l.day or '')[:3].upper())
            parts.append(f"{day} {_fmt_time(l.time_start)}-{_fmt_time(l.time_end)}" + (f" {l.room}" if l.room else ''))
        return '; '.join(parts)

    def is_offered(self, code_key):
        return bool(self.by_key.get(code_key))


def find_conflicts(lines, offerings):
    """[(code_a, code_b, day)] for lines whose assigned sections meet at the same time."""
    slots = []
    for ln in lines:
        if not ln.section_id:
            continue
        for l in offerings.loads(ln.code_key, ln.section_id):
            slots.append((ln.subject_code, (l.day or '').lower(), l.time_start, l.time_end))
    out = set()
    for i in range(len(slots)):
        for j in range(i + 1, len(slots)):
            a, b = slots[i], slots[j]
            if a[0] != b[0] and a[1] == b[1] and a[2] < b[3] and b[2] < a[3]:
                out.add((min(a[0], b[0]), max(a[0], b[0]), a[1].title()))
    return sorted(out)


def _section_usage(term_id, section_id, code_key, exclude_form_id=None):
    q = """SELECT COUNT(*) FROM registration_form_subjects l JOIN registration_forms f ON f.id = l.registration_form_id
           WHERE f.academic_term_id = :t AND f.status <> 'cancelled' AND l.section_id = :s AND l.code_key = :k"""
    params = {'t': term_id, 's': section_id, 'k': code_key}
    if exclude_form_id:
        q += " AND f.id <> :f"
        params['f'] = exclude_form_id
    return db.session.execute(text(q), params).scalar() or 0


def valid_sections(ctx, offerings, cs):
    """Sections a curriculum subject can be taken in this term."""
    offered = offerings.sections_for(cs.code_key)
    if offered:
        return [s for s in offered if s.is_active]
    # Not scheduled yet: any active section of the student's program for that subject's year level.
    yl = YEAR_LABELS.get(cs.year_level)
    return Section.query.filter_by(program_id=ctx.program.program_id, year_level=yl, is_active=True).order_by(Section.name).all()


# ---------------------------------------------------------------------------
# eligibility of one curriculum subject
# ---------------------------------------------------------------------------

def evaluate_subject(ctx, cs, form=None, offerings=None):
    """{'errors': [...], 'acks': [...], 'info': [...]} for adding `cs` to the form."""
    errors, acks, info = [], [], []
    h = ctx.history.get(cs.code_key)
    if h and h['status'] == 'passed':
        errors.append(f"Already passed ({', '.join(h['terms'])}).")
    elif h and h['status'] == 'in_progress':
        acks.append(f"Taken in {', '.join(h['terms'])} and no grade is recorded yet.")
    elif h and h['status'] in ('failed', 'incomplete', 'dropped'):
        info.append(f"Previously {h['status']} ({', '.join(h['terms'])}).")
    if form is not None and any(l.code_key == cs.code_key for l in form.lines):
        errors.append('Already on this registration form.')
    for p in cs.prerequisites:
        if p.kind == 'subject' and p.prerequisite is not None:
            ph = ctx.history.get(p.prerequisite.code_key)
            if not ph or ph['status'] != 'passed':
                state = ph['status'].replace('_', ' ') if ph else 'not taken'
                errors.append(f"Prerequisite {p.prerequisite.subject_code} not passed ({state}).")
        elif p.kind == 'standing' and p.min_year_level:
            if ctx.year_level < p.min_year_level:
                errors.append(f"Requires {YEAR_LABELS.get(p.min_year_level)} standing (student is {YEAR_LABELS.get(ctx.year_level)}).")
        elif p.kind == 'units' and p.min_units is not None:
            if ctx.passed_units < Decimal(p.min_units):
                errors.append(f"Requires {p.label()} (student has {fmt_units(ctx.passed_units)} passed units).")
        elif p.kind == 'other':
            acks.append(f"Requirement to verify: {p.requirement_text}")
    if cs.year_level > ctx.year_level:
        info.append(f"From {YEAR_LABELS.get(cs.year_level)} of the curriculum (student is {YEAR_LABELS.get(ctx.year_level)}).")
    if cs.semester != ctx.semester:
        info.append(f"Scheduled in the curriculum for {SEMESTER_LABELS.get(cs.semester)}, not this term's semester.")
    if offerings is not None and not offerings.is_offered(cs.code_key):
        info.append('No class schedule for this subject in this term yet.')
    return {'errors': errors, 'acks': acks, 'info': info}


# ---------------------------------------------------------------------------
# form lifecycle
# ---------------------------------------------------------------------------

def get_form(student_user_id, term_id, lock=False):
    q = RegistrationForm.query.filter_by(student_user_id=student_user_id, academic_term_id=term_id)
    if lock:
        q = q.with_for_update()
    return q.first()


def _lock_form(form_id):
    f = RegistrationForm.query.filter_by(id=form_id).with_for_update().first()
    if f is None:
        raise RegformError('Registration form not found.')
    return f


def _require_draft(form):
    if form.status == 'finalized':
        raise RegformError('This registration form is finalized. Reopen it to make changes.')
    if form.status == 'cancelled':
        raise RegformError('This registration form is cancelled.')


def _recalc(form):
    form.total_units = sum((Decimal(l.units) for l in form.lines), Decimal('0'))


def _snapshot(form):
    return [{'code': l.subject_code, 'units': l.units, 'section': section_label(l.section) if l.section else None,
             'schedule': l.schedule_text} for l in form.lines]


def _new_line(form, cs, actor, source, section, offerings):
    ln = RegistrationFormSubject(
        curriculum_subject_id=cs.id, subject_code=cs.subject_code, code_key=cs.code_key,
        description=cs.description, units=cs.units, year_level=cs.year_level, semester=cs.semester,
        section_id=section.id if section else None,
        schedule_text=offerings.schedule_text(cs.code_key, section.id) if section else None,
        source=source, added_by=actor.name, added_at=datetime.now())
    # Append through the relationship only (it sets the FK and saves the row).
    # Adding it to the session first would let an autoflush load it into
    # form.lines and the append would list it twice.
    form.lines.append(ln)
    return ln


def _default_section(ctx, offerings, cs, block_section):
    options = valid_sections(ctx, offerings, cs)
    if block_section is not None and any(s.id == block_section.id for s in options):
        return block_section
    if block_section is not None and not offerings.is_offered(cs.code_key) and block_section.program_id == ctx.program.program_id:
        return block_section
    return options[0] if len(options) == 1 else None


def create_form(applicant_id, term_id, actor):
    """Create the student's form for the term (or return the existing one)."""
    ctx = Context(applicant_id, term_id)
    existing = get_form(ctx.student_user_id, term_id)
    if existing is not None:
        return existing, ctx, False
    classification, reason = ctx.suggested_classification()
    block = db.session.get(Section, ctx.applicant.section_id) if ctx.applicant.section_id else None
    form = RegistrationForm(
        student_user_id=ctx.student_user_id, applicant_id=ctx.applicant.applicant_id, academic_term_id=term_id,
        curriculum_version_id=ctx.curriculum.id, program_id=ctx.program.program_id, year_level=ctx.year_level,
        semester=ctx.semester, classification=classification, classification_reason=reason[:255],
        status='draft', section_id=block.id if block else None, max_units=ctx.max_units,
        created_by=actor.name, created_at=datetime.now())
    db.session.add(form)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()   # another registrar created it a moment ago
        return get_form(ctx.student_user_id, term_id), Context(applicant_id, term_id), False
    offerings = Offerings(ctx.term, ctx.semester)
    added = []
    if classification == 'Regular':
        for cs in ctx.regular_subjects:
            if ctx.history.get(cs.code_key, {}).get('status') == 'passed':
                continue
            _new_line(form, cs, actor, 'curriculum', _default_section(ctx, offerings, cs, block), offerings)
            added.append(cs.subject_code)
    else:
        # Suggest this term's subjects the student is eligible for; the registrar adjusts.
        for cs in ctx.regular_subjects:
            ev = evaluate_subject(ctx, cs, form)
            if not ev['errors'] and not ev['acks']:
                _new_line(form, cs, actor, 'curriculum', _default_section(ctx, offerings, cs, block), offerings)
                added.append(cs.subject_code)
    _recalc(form)
    _log(form.id, 'create', actor, details={'classification': classification, 'reason': reason,
                                            'curriculum': ctx.curriculum.name, 'curriculum_source': ctx.curriculum_source,
                                            'subjects': added, 'max_units': ctx.max_units})
    return form, ctx, True


def context_for(form):
    return Context(form.applicant_id, form.academic_term_id)


def set_classification(form_id, actor, classification, reason):
    form = _lock_form(form_id)
    _require_draft(form)
    if classification not in ('Regular', 'Irregular'):
        raise RegformError('Choose Regular or Irregular.')
    reason = (reason or '').strip()
    if not reason:
        raise RegformError('Give a reason for the classification change.')
    if classification == form.classification:
        return form
    before = form.classification
    if classification == 'Regular':
        # Back to the plain curriculum load: rebuild the lines.
        ctx = context_for(form)
        offerings = Offerings(ctx.term, ctx.semester)
        old = _snapshot(form)
        for ln in list(form.lines):
            form.lines.remove(ln)
            db.session.delete(ln)
        db.session.flush()
        block = form.section
        for cs in ctx.regular_subjects:
            if ctx.history.get(cs.code_key, {}).get('status') == 'passed':
                continue
            _new_line(form, cs, actor, 'curriculum', _default_section(ctx, offerings, cs, block), offerings)
        _recalc(form)
        _log(form.id, 'rebuild_regular', actor, details={'before': old, 'after': _snapshot(form)})
    form.classification, form.classification_reason = classification, reason[:255]
    _log(form.id, 'classification', actor, details={'before': before, 'after': classification, 'reason': reason})
    return form


def add_subject(form_id, actor, curriculum_subject_id, section_id=None, acknowledge=False, remarks=None):
    form = _lock_form(form_id)
    _require_draft(form)
    if form.classification != 'Irregular':
        raise RegformError('A regular student takes exactly the curriculum subjects of the term. '
                           'Mark the student Irregular (with a reason) to customize the subjects.')
    cs = db.session.get(CurriculumSubject, curriculum_subject_id) if curriculum_subject_id else None
    if cs is None or cs.curriculum_version_id != form.curriculum_version_id:
        raise RegformError("That subject is not part of the student's curriculum.")
    ctx = context_for(form)
    offerings = Offerings(ctx.term, ctx.semester)
    ev = evaluate_subject(ctx, cs, form, offerings)
    if ev['errors']:
        raise RegformError(f'{cs.subject_code} cannot be added:', *ev['errors'])
    if ev['acks'] and not acknowledge:
        raise NeedsAcknowledgement(f'{cs.subject_code} needs your confirmation:', *ev['acks'])
    new_total = Decimal(form.total_units or 0) + Decimal(cs.units)
    if new_total > Decimal(form.max_units):
        raise RegformError(f'Adding {cs.subject_code} ({fmt_units(cs.units)} units) would bring the load to {fmt_units(new_total)} units; '
                           f'the limit for this student is {fmt_units(Decimal(form.max_units))}.')
    section = _validate_section(ctx, offerings, form, cs, section_id) if section_id else _default_section(ctx, offerings, cs, form.section)
    ln = _new_line(form, cs, actor, 'back_subject' if term_order(cs.year_level, cs.semester) < term_order(form.year_level, form.semester) else 'added',
                   section, offerings)
    ln.remarks = (remarks or '').strip()[:255] or None
    conflicts = find_conflicts(form.lines, offerings)
    if conflicts:
        raise RegformError('Schedule conflict: ' + '; '.join(f'{a} and {b} on {d}' for a, b, d in conflicts))
    _recalc(form)
    _log(form.id, 'add', actor, cs.subject_code, {'units': cs.units, 'section': section_label(section) if section else None,
                                                  'acknowledged': ev['acks'] if acknowledge else [], 'info': ev['info']})
    return ln


def remove_subject(form_id, actor, line_id):
    form = _lock_form(form_id)
    _require_draft(form)
    if form.classification != 'Irregular':
        raise RegformError('Mark the student Irregular (with a reason) to remove curriculum subjects.')
    ln = next((l for l in form.lines if l.id == int(line_id)), None)
    if ln is None:
        raise RegformError('Subject not on this form.')
    snap = {'units': ln.units, 'section': section_label(ln.section) if ln.section else None}
    form.lines.remove(ln)
    db.session.delete(ln)
    _recalc(form)
    _log(form.id, 'remove', actor, ln.subject_code, snap)


def _validate_section(ctx, offerings, form, cs, section_id):
    try:
        sid = int(section_id)
    except (TypeError, ValueError):
        raise RegformError('Choose a section.')
    options = {s.id: s for s in valid_sections(ctx, offerings, cs)}
    if sid not in options:
        raise RegformError(f'That section does not offer {cs.subject_code} this term.')
    # Lock the section row so two registrars cannot both take the last seat.
    sec = Section.query.filter_by(id=sid).with_for_update().first()
    if not sec.is_active:
        raise RegformError(f'Section {section_label(sec)} is inactive.')
    used = _section_usage(form.academic_term_id, sid, cs.code_key, exclude_form_id=form.id)
    if sec.max_students and used >= sec.max_students:
        raise RegformError(f'{section_label(sec)} is full for {cs.subject_code} ({used}/{sec.max_students}).')
    return sec


def assign_section(form_id, actor, line_id, section_id):
    form = _lock_form(form_id)
    _require_draft(form)
    ln = next((l for l in form.lines if l.id == int(line_id)), None)
    if ln is None:
        raise RegformError('Subject not on this form.')
    ctx = context_for(form)
    offerings = Offerings(ctx.term, ctx.semester)
    before = section_label(ln.section) if ln.section else None
    if section_id in (None, ''):
        ln.section_id, ln.schedule_text = None, None
    else:
        sec = _validate_section(ctx, offerings, form, ln.curriculum_subject, section_id)
        ln.section_id, ln.section = sec.id, sec
        ln.schedule_text = offerings.schedule_text(ln.code_key, sec.id)
    db.session.flush()
    conflicts = find_conflicts(form.lines, offerings)
    if conflicts:
        raise RegformError('Schedule conflict: ' + '; '.join(f'{a} and {b} on {d}' for a, b, d in conflicts))
    _log(form.id, 'section', actor, ln.subject_code, {'before': before, 'after': section_label(ln.section) if ln.section else None})
    return ln


def assign_block_section(form_id, actor, section_id):
    """Set the home section and put every subject it offers into it."""
    form = _lock_form(form_id)
    _require_draft(form)
    ctx = context_for(form)
    offerings = Offerings(ctx.term, ctx.semester)
    sec = Section.query.filter_by(id=int(section_id), program_id=form.program_id).with_for_update().first() if section_id else None
    if sec is None or not sec.is_active:
        raise RegformError("Choose an active section of the student's program.")
    before = _snapshot(form)
    moved = []
    for ln in form.lines:
        options = {s.id for s in valid_sections(ctx, offerings, ln.curriculum_subject)}
        if sec.id in options:
            used = _section_usage(form.academic_term_id, sec.id, ln.code_key, exclude_form_id=form.id)
            if sec.max_students and used >= sec.max_students:
                raise RegformError(f'{section_label(sec)} is full for {ln.subject_code} ({used}/{sec.max_students}).')
            ln.section_id, ln.section = sec.id, sec
            ln.schedule_text = offerings.schedule_text(ln.code_key, sec.id)
            moved.append(ln.subject_code)
    db.session.flush()
    conflicts = find_conflicts(form.lines, offerings)
    if conflicts:
        raise RegformError('Schedule conflict: ' + '; '.join(f'{a} and {b} on {d}' for a, b, d in conflicts))
    form.section_id, form.section = sec.id, sec
    _log(form.id, 'block_section', actor, details={'section': section_label(sec), 'subjects': moved,
                                                   'before': before})
    return sec, moved


def validate_for_finalize(form, ctx=None):
    ctx = ctx or context_for(form)
    offerings = Offerings(ctx.term, ctx.semester)
    errors, warnings = [], []
    if not form.lines:
        errors.append('The form has no subjects.')
    if Decimal(form.total_units or 0) > Decimal(form.max_units):
        errors.append(f'Total units {fmt_units(form.total_units)} exceed the limit of {fmt_units(Decimal(form.max_units))}.')
    for ln in form.lines:
        cs = ln.curriculum_subject
        h = ctx.history.get(cs.code_key)
        if h and h['status'] == 'passed':
            errors.append(f'{ln.subject_code} is already passed.')
        for p in cs.prerequisites:
            if p.kind == 'subject' and p.prerequisite is not None and ctx.history.get(p.prerequisite.code_key, {}).get('status') != 'passed':
                errors.append(f'{ln.subject_code}: prerequisite {p.prerequisite.subject_code} is not passed.')
        if not ln.section_id:
            errors.append(f'{ln.subject_code} has no section.')
        elif not offerings.loads(ln.code_key, ln.section_id):
            warnings.append(f'{ln.subject_code}: no class schedule yet in {section_label(ln.section)} (prints as TBA).')
    for a, b, d in find_conflicts(form.lines, offerings):
        errors.append(f'Schedule conflict: {a} and {b} on {d}.')
    if form.classification == 'Regular':
        expected = {s.code_key for s in ctx.regular_subjects if ctx.history.get(s.code_key, {}).get('status') != 'passed'}
        if {l.code_key for l in form.lines} != expected:
            errors.append('A regular form must list exactly the curriculum subjects of the term.')
    return errors, warnings


def finalize(form_id, actor):
    form = _lock_form(form_id)
    _require_draft(form)
    ctx = context_for(form)
    errors, warnings = validate_for_finalize(form, ctx)
    if errors:
        raise RegformError('The form cannot be finalized yet:', *errors)
    # Refresh schedule snapshots so the printed form shows the current timetable.
    offerings = Offerings(ctx.term, ctx.semester)
    for ln in form.lines:
        ln.schedule_text = offerings.schedule_text(ln.code_key, ln.section_id) or None
    form.status, form.finalized_by, form.finalized_at = 'finalized', actor.name, datetime.now()
    home_set = False
    if form.section_id and not ctx.applicant.section_id:
        ctx.applicant.section_id = form.section_id   # first home section for the student
        home_set = True
    # Phase 4: the term assessment is generated from the finalized form, in the
    # same transaction (a failure rolls the finalize back as well).
    from website.finance import service as finance
    ea, fee_report = finance.generate_for_form(form, actor)
    if not fee_report['rules_matched']:
        warnings.append('No fee rule of Accounting matches this student (program/year/semester/residency); '
                        'the assessment is 0 until Accounting sets up the fees and regenerates it.')
    _log(form.id, 'finalize', actor, details={'subjects': _snapshot(form), 'total_units': form.total_units,
                                              'warnings': warnings, 'home_section_set': home_set,
                                              'assessment_total': ea.total_assessment})
    return form, warnings


def reopen(form_id, actor, reason):
    form = _lock_form(form_id)
    if form.status != 'finalized':
        raise RegformError('Only a finalized form can be reopened.')
    from website.checkin.service import is_checked_in
    if is_checked_in(form.student_user_id, form.academic_term_id):
        raise RegformError('The student is already checked in for this term. Undo the check-in before changing the registration form.')
    reason = (reason or '').strip()
    if not reason:
        raise RegformError('Give a reason for reopening the form.')
    form.status = 'draft'
    _log(form.id, 'reopen', actor, details={'reason': reason, 'subjects': _snapshot(form)})
    return form


# ---------------------------------------------------------------------------
# read model for the screen
# ---------------------------------------------------------------------------

def available_subjects(ctx, form):
    """Every curriculum subject not on the form, with its eligibility."""
    offerings = Offerings(ctx.term, ctx.semester)
    on_form = {l.code_key for l in form.lines}
    rows = []
    for cs in ctx.subjects:
        if cs.code_key in on_form:
            continue
        ev = evaluate_subject(ctx, cs, form, offerings)
        status = 'blocked' if ev['errors'] else ('confirm' if ev['acks'] else 'eligible')
        h = ctx.history.get(cs.code_key)
        rows.append({'subject': cs, 'eval': ev, 'status': status, 'history': h,
                     'sections': valid_sections(ctx, offerings, cs), 'offered': offerings.is_offered(cs.code_key),
                     'back': term_order(cs.year_level, cs.semester) < term_order(form.year_level, form.semester)})
    rows.sort(key=lambda r: (r['status'] == 'blocked', not r['back'], term_order(r['subject'].year_level, r['subject'].semester),
                             r['subject'].sort_order))
    return rows, offerings
