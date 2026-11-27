"""Phase 6: PGPC-YY-NNNN enrollment control numbers.

* YY   = the first year of the term's school year (2026-2027 -> 26).
* NNNN = one counter per YY shared by every program, at least four digits
         (PGPC-26-0001 ... PGPC-26-9999, then PGPC-26-10000; never wraps).
* A number is issued when the registrar checks the student in, once per student
  and term. Undoing and redoing the check-in keeps the same number; numbers are
  never deleted, reused or renumbered.
* The counter row is locked (SELECT ... FOR UPDATE) while a number is taken, and
  unique keys on the number, on (counter, seq) and on (student, term) make a
  duplicate impossible even if two registrars click at the same moment.
Callers commit or roll back.
"""

import json
import re
from datetime import datetime

from sqlalchemy import text

from website import db
from website.models import ControlNumber, ControlNumberSequence, EnrollmentAuditLog, EnrollmentCheckin, Applicant, RegistrationForm

PREFIX = 'PGPC'
_SY = re.compile(r'^\s*(\d{4})\s*-\s*(\d{4})\s*$')


class ControlNumberError(Exception):
    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


def _q(sql, **params):
    return db.session.execute(text(sql), params)


def year_code(school_year):
    m = _SY.match(school_year or '')
    if not m or int(m.group(2)) != int(m.group(1)) + 1:
        raise ControlNumberError('The school year "%s" is not in the form 2026-2027, so no control number can be made.' % school_year)
    return m.group(1)[2:]


def format_number(yy, seq):
    return '%s-%s-%04d' % (PREFIX, yy, seq)


def for_student_term(student_user_id, term_id):
    return ControlNumber.query.filter_by(student_user_id=student_user_id, academic_term_id=term_id).first()


def _audit(action, actor, details, applicant_id=None, student_user_id=None, term_id=None):
    db.session.add(EnrollmentAuditLog(action=action, student_user_id=student_user_id, applicant_id=applicant_id,
                                      academic_term_id=term_id, performed_by=actor.name, performed_role=actor.role,
                                      details=json.dumps(details, default=str, ensure_ascii=False), performed_at=datetime.now()))


_LOCK_SQL = "SELECT id, last_number FROM control_number_sequences WHERE prefix = :p AND year_code = :y FOR UPDATE"


def _locked_sequence(yy, school_year):
    """The year's counter row, locked for this transaction.

    A missing counter is created first in its own short, committed statement
    (an empty counter is harmless). Doing INSERT IGNORE + FOR UPDATE inside the
    caller's transaction deadlocks two concurrent registrars: both take the
    shared duplicate-key lock and then wait for each other's exclusive lock."""
    exists = _q("SELECT 1 FROM control_number_sequences WHERE prefix = :p AND year_code = :y", p=PREFIX, y=yy).first()
    if not exists:
        with db.engine.begin() as conn:
            conn.execute(text("INSERT IGNORE INTO control_number_sequences (prefix, year_code, school_year, last_number, updated_by) "
                              "VALUES (:p, :y, :sy, 0, 'system')"), {'p': PREFIX, 'y': yy, 'sy': school_year})
    row = _q(_LOCK_SQL, p=PREFIX, y=yy).fetchone()
    if row is None:
        raise ControlNumberError('The control-number counter for %s could not be created.' % school_year)
    return row


def issue(actor, applicant, term, form=None, source='check_in'):
    """The student's control number for the term: the existing one, or the next
    number of the year's counter. Returns (ControlNumber, newly_issued)."""
    existing = for_student_term(applicant.student_user_id, term.id)
    if existing is not None:
        if form is not None and form.control_number != existing.number:
            form.control_number = existing.number
        return existing, False
    yy = year_code(term.year_name)
    seq_row = _locked_sequence(yy, term.year_name)
    seq = int(seq_row[1]) + 1
    number = format_number(yy, seq)
    _q("UPDATE control_number_sequences SET last_number = :n, updated_by = :u WHERE id = :i", n=seq, u=actor.name, i=seq_row[0])
    cn = ControlNumber(number=number, sequence_id=seq_row[0], seq=seq, student_user_id=applicant.student_user_id,
                       applicant_id=applicant.applicant_id, academic_term_id=term.id,
                       registration_form_id=form.id if form is not None else None,
                       issued_by=actor.name, issued_role=actor.role, issued_at=datetime.now(), source=source)
    db.session.add(cn)
    if form is not None:
        form.control_number = number
    db.session.flush()
    _audit('control_number', actor, {'number': number, 'source': source, 'registration_form_id': cn.registration_form_id},
           applicant.applicant_id, applicant.student_user_id, term.id)
    return cn, True


# ---------------------------------------------------------------------------
# administration
# ---------------------------------------------------------------------------

def sequences():
    rows = []
    for s in ControlNumberSequence.query.order_by(ControlNumberSequence.year_code.desc()).all():
        stats = _q("SELECT COUNT(*), MAX(seq), MIN(seq) FROM control_numbers WHERE sequence_id = :i", i=s.id).fetchone()
        rows.append({'seq': s, 'issued': stats[0] or 0, 'max_issued': stats[1] or 0, 'min_issued': stats[2],
                     'next': format_number(s.year_code, s.last_number + 1)})
    return rows


def set_next(actor, school_year, next_number):
    """Set the next number to issue for a school year (e.g. continue from a paper register).
    It can never go back to a number already issued."""
    if actor.role != 'admin':
        raise ControlNumberError('Only an administrator can change the control-number counter.')
    yy = year_code(school_year)
    try:
        nxt = int(str(next_number).strip())
    except (TypeError, ValueError):
        raise ControlNumberError('Enter the next number as a whole number, e.g. 1434.')
    if nxt < 1 or nxt > 999999:
        raise ControlNumberError('The next number must be between 1 and 999999.')
    row = _locked_sequence(yy, school_year.strip())
    issued_max = _q("SELECT COALESCE(MAX(seq), 0) FROM control_numbers WHERE sequence_id = :i", i=row[0]).scalar()
    if nxt <= issued_max:
        raise ControlNumberError('%s has already been issued. The next number must be above %d.'
                                 % (format_number(yy, issued_max), issued_max))
    before = int(row[1])
    _q("UPDATE control_number_sequences SET last_number = :n, school_year = :sy, updated_by = :u WHERE id = :i",
       n=nxt - 1, sy=school_year.strip(), u=actor.name, i=row[0])
    _audit('control_sequence', actor, {'year_code': yy, 'last_number_before': before, 'last_number_after': nxt - 1,
                                       'next': format_number(yy, nxt)})
    return format_number(yy, nxt)


def backfill(actor, term):
    """Issue numbers to students checked in for the term before Phase 6, in check-in order."""
    if actor.role != 'admin':
        raise ControlNumberError('Only an administrator can issue numbers in bulk.')
    done = []
    pending = (EnrollmentCheckin.query.filter_by(academic_term_id=term.id, active=True)
               .order_by(EnrollmentCheckin.checked_in_at, EnrollmentCheckin.id).all())
    for ci in pending:
        if for_student_term(ci.student_user_id, term.id):
            continue
        applicant = db.session.get(Applicant, ci.applicant_id)
        form = db.session.get(RegistrationForm, ci.registration_form_id) if ci.registration_form_id else None
        cn, _ = issue(actor, applicant, term, form, source='backfill')
        done.append(cn.number)
    return done


def issued(term_id=None, search=None, limit=500):
    q = ControlNumber.query
    if term_id:
        q = q.filter_by(academic_term_id=term_id)
    if search:
        like = '%' + search.strip() + '%'
        q = q.join(Applicant, Applicant.applicant_id == ControlNumber.applicant_id).filter(
            db.or_(ControlNumber.number.like(like), Applicant.last_name.like(like), Applicant.first_name.like(like),
                   Applicant.old_student_id.like(like)))
    rows = q.order_by(ControlNumber.sequence_id.desc(), ControlNumber.seq.desc()).limit(limit).all()
    return [{'cn': r, 'applicant': db.session.get(Applicant, r.applicant_id),
             'active': EnrollmentCheckin.query.filter_by(student_user_id=r.student_user_id,
                                                         academic_term_id=r.academic_term_id, active=True).first() is not None}
            for r in rows]
