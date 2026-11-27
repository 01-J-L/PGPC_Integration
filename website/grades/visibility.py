"""Phase 7: what a student may see of the registrar's grades (student_grades).

A term's grades are visible to a student only when
  1. the term is RELEASED (a per-program override wins over the all-programs row), and
  2. the student has no active hold for that term (or for all terms), and
  3. if an administrator switched it on: the student has no unpaid balance for that term.
Staff screens are not affected. Callers commit or roll back.
"""

import json
from datetime import datetime

from sqlalchemy import text

from website import db
from website.models import GradeRelease, GradeHold, EnrollmentAuditLog, AcademicTerm, Program, StudentUser

ROLES = ('registrar', 'admin')
UNPAID_KEY = 'grade_hold_unpaid_balance'

MSG_NOT_RELEASED = 'Grades for this term are not yet released.'
MSG_HOLD = "Your grades for this term are on hold. Please see the Registrar's Office."
MSG_UNPAID = 'Your grades for this term are withheld until your account balance is settled with Accounting.'


class GradeVisibilityError(Exception):
    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


def _q(sql, **params):
    return db.session.execute(text(sql), params)


def norm(value):
    return (value or '').strip()


def term_label(academic_year, semester):
    return '%s - %s' % (norm(academic_year), norm(semester))


def _audit(action, actor, details, student_user_id=None):
    db.session.add(EnrollmentAuditLog(action=action, student_user_id=student_user_id, performed_by=actor.name,
                                      performed_role=actor.role, performed_at=datetime.now(),
                                      details=json.dumps(details, default=str, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

def student_program(student_user_id):
    return _q("""SELECT p.program_id FROM applicants a JOIN programs p ON p.title = a.program_choice
                 WHERE a.student_user_id = :s ORDER BY a.applicant_id DESC LIMIT 1""", s=student_user_id).scalar()


def release_row(academic_year, semester, program_id=None):
    """The row that decides the term for this program: the program override, else the all-programs row."""
    rows = GradeRelease.query.filter(GradeRelease.academic_year == norm(academic_year),
                                     GradeRelease.semester == norm(semester),
                                     GradeRelease.program_key.in_(['', program_id or ''])).all()
    by_key = {r.program_key: r for r in rows}
    return by_key.get(program_id or '') if program_id and program_id in by_key else by_key.get('')


def is_released(academic_year, semester, program_id=None):
    row = release_row(academic_year, semester, program_id)
    return bool(row and row.is_released)


def active_holds(student_user_id):
    return GradeHold.query.filter_by(student_user_id=student_user_id, lifted_at=None).order_by(GradeHold.placed_at).all()


def unpaid_hold_enabled():
    return _q("SELECT setting_value FROM system_settings WHERE setting_key = :k", k=UNPAID_KEY).scalar() == '1'


def _unpaid(student_user_id, academic_year, semester):
    term = AcademicTerm.query.filter_by(year_name=norm(academic_year), semester=norm(semester)).first()
    if term is None:
        return False
    from website.finance import service as finance
    s = finance.summary(student_user_id, term.id)
    return s['assessment'] is not None and s['remaining'] > 0


def visibility(student_user_id, academic_year, semester, program_id=None, holds=None, unpaid_rule=None):
    """(visible, message) for one student and term."""
    program_id = program_id if program_id is not None else student_program(student_user_id)
    if not is_released(academic_year, semester, program_id):
        return False, MSG_NOT_RELEASED
    holds = active_holds(student_user_id) if holds is None else holds
    for h in holds:
        if not h.academic_year or (h.academic_year == norm(academic_year) and h.semester == norm(semester)):
            return False, MSG_HOLD
    if (unpaid_hold_enabled() if unpaid_rule is None else unpaid_rule) and _unpaid(student_user_id, academic_year, semester):
        return False, MSG_UNPAID
    return True, None


_GRADES_SQL = """SELECT sg.academic_year, sg.semester, s.subject_code, s.subject_title, s.units, s.year_level,
                        sg.grade, sg.remarks
                 FROM student_grades sg JOIN subjects s ON sg.subject_id = s.id
                 WHERE sg.student_user_id = :s
                 ORDER BY sg.academic_year DESC, sg.semester ASC, s.subject_code ASC"""


def student_terms(student_user_id):
    """Every term the student has grades in, newest first:
    [{'label', 'academic_year', 'semester', 'visible', 'message', 'grades': [...] (empty when hidden)}]"""
    program_id = student_program(student_user_id)
    holds = active_holds(student_user_id)
    unpaid_rule = unpaid_hold_enabled()
    terms, order = {}, []
    for r in _q(_GRADES_SQL, s=student_user_id).mappings().all():
        key = (norm(r['academic_year']), norm(r['semester']))
        if key not in terms:
            visible, message = visibility(student_user_id, key[0], key[1], program_id, holds, unpaid_rule)
            terms[key] = {'label': term_label(*key), 'academic_year': key[0], 'semester': key[1],
                          'visible': visible, 'message': message, 'grades': []}
            order.append(key)
        if terms[key]['visible']:
            terms[key]['grades'].append(dict(r))
    return [terms[k] for k in order]


def visible_rows(student_user_id):
    return [g for t in student_terms(student_user_id) for g in t['grades']]


# ---------------------------------------------------------------------------
# registrar / admin actions
# ---------------------------------------------------------------------------

def _check_role(actor):
    if actor.role not in ROLES:
        raise GradeVisibilityError('Only the registrar or an administrator can change grade visibility.')


def set_release(actor, academic_year, semester, program_id=None, released=True, reason=None):
    _check_role(actor)
    ay, sem, prog = norm(academic_year), norm(semester), norm(program_id)
    if not ay or not sem:
        raise GradeVisibilityError('Choose the school year and semester.')
    if prog and db.session.get(Program, prog) is None:
        raise GradeVisibilityError('Unknown program.')
    reason = norm(reason)
    if not released and not reason:
        raise GradeVisibilityError('Give a reason for withholding the grades.')
    row = GradeRelease.query.filter_by(academic_year=ay, semester=sem, program_key=prog).with_for_update().first()
    before = None if row is None else bool(row.is_released)
    if row is None:
        row = GradeRelease(academic_year=ay, semester=sem, program_key=prog)
        db.session.add(row)
    row.is_released = bool(released)
    if released:
        row.released_by, row.released_at = actor.name, datetime.now()
        row.reason = reason[:500] or None
    else:
        row.withheld_by, row.withheld_at, row.reason = actor.name, datetime.now(), reason[:500]
    db.session.flush()
    _audit('grade_release' if released else 'grade_withhold', actor,
           {'academic_year': ay, 'semester': sem, 'program': prog or 'all', 'before': before, 'reason': reason or None})
    return row


def place_hold(actor, student_user_id, academic_year, semester, reason):
    _check_role(actor)
    if db.session.get(StudentUser, student_user_id) is None:
        raise GradeVisibilityError('Student not found.')
    ay, sem, reason = norm(academic_year), norm(semester), norm(reason)
    if bool(ay) != bool(sem):
        raise GradeVisibilityError('Choose both the school year and the semester, or "All terms".')
    if not reason:
        raise GradeVisibilityError('Give a reason for the hold.')
    h = GradeHold(student_user_id=student_user_id, academic_year=ay, semester=sem, reason=reason[:500],
                  placed_by=actor.name, placed_role=actor.role, placed_at=datetime.now())
    db.session.add(h)
    db.session.flush()
    _audit('grade_hold', actor, {'hold_id': h.id, 'term': term_label(ay, sem) if ay else 'all terms', 'reason': reason},
           student_user_id)
    return h


def lift_hold(actor, hold_id, reason):
    _check_role(actor)
    h = db.session.get(GradeHold, int(hold_id))
    if h is None or h.lifted_at is not None:
        raise GradeVisibilityError('Hold not found or already lifted.')
    reason = norm(reason)
    if not reason:
        raise GradeVisibilityError('Give a reason for lifting the hold.')
    h.lifted_by, h.lifted_at, h.lift_reason = actor.name, datetime.now(), reason[:500]
    _audit('grade_hold_lift', actor, {'hold_id': h.id, 'reason': reason}, h.student_user_id)
    return h


def set_unpaid_rule(actor, enabled):
    if actor.role != 'admin':
        raise GradeVisibilityError('Only an administrator can change the unpaid-balance rule.')
    _q("""INSERT INTO system_settings (setting_key, setting_value) VALUES (:k, :v)
          ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)""", k=UNPAID_KEY, v='1' if enabled else '0')
    _audit('grade_unpaid_rule', actor, {'enabled': bool(enabled)})


def find_student(query):
    """Student account id from a student no., control no., e-mail or applicant id."""
    q = norm(query)
    if not q:
        return None
    return _q("""SELECT a.student_user_id FROM applicants a LEFT JOIN student_users su ON su.id = a.student_user_id
                 WHERE a.student_user_id IS NOT NULL
                   AND (a.old_student_id = :q OR a.control_number = :q OR su.email = :q OR a.email_address = :q
                        OR CAST(a.applicant_id AS CHAR) = :q)
                 ORDER BY a.applicant_id DESC LIMIT 1""", q=q).scalar()


def overview():
    """Every term that has grades or exists as an academic term, with its release state."""
    counts = {(norm(r[0]), norm(r[1])): (r[2], r[3]) for r in _q(
        "SELECT academic_year, semester, COUNT(*), COUNT(DISTINCT student_user_id) FROM student_grades "
        "GROUP BY academic_year, semester").fetchall()}
    keys = set(counts) | {(norm(t.year_name), norm(t.semester)) for t in AcademicTerm.query.all()}
    releases = {}
    for r in GradeRelease.query.all():
        releases.setdefault((r.academic_year, r.semester), []).append(r)
    out = []
    for ay, sem in sorted(keys, key=lambda k: (k[0], k[1]), reverse=True):
        rows = releases.get((ay, sem), [])
        general = next((r for r in rows if r.program_key == ''), None)
        out.append({'academic_year': ay, 'semester': sem, 'label': term_label(ay, sem),
                    'grades': counts.get((ay, sem), (0, 0))[0], 'students': counts.get((ay, sem), (0, 0))[1],
                    'general': general, 'released': bool(general and general.is_released),
                    'overrides': sorted([r for r in rows if r.program_key], key=lambda r: r.program_key)})
    return out
