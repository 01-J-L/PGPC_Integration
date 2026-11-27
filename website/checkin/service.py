"""Phase 5: enrollment approval checklist and registrar check-in.

Every path that makes a student 'Enrolled' for a term goes through `check_in`:
the review page's Approve button, the status dropdown and bulk status update.
Callers commit or roll back; nothing here commits.
"""

import json
from datetime import datetime

from sqlalchemy import text

from website import db
from website.models import (AcademicTerm, Applicant, RegistrationForm, Section, StudentUser,
                            EnrollmentChecklistRequirement, EnrollmentChecklistItem, EnrollmentCheckin,
                            EnrollmentAuditLog)

CHECKIN_ROLES = ('registrar', 'admin')
ADMISSION_ROLES = ('admin', 'registrar', 'accounting', 'cashier', 'guidance', 'osa', 'secretary')
KINDS = {'manual': 'Approved by an office', 'regform': 'Registration form finalized (automatic)',
         'payment': 'Enrollment payment policy (automatic)'}
APPLIES = {'all': 'All students', 'new': 'New students only', 'existing': 'Existing students only'}
CHECKIN_STATUSES = ('Enrolling', 'Enrolled')


class CheckinError(Exception):
    def __init__(self, *messages):
        self.messages = [m for m in messages if m]
        super().__init__('; '.join(self.messages))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _q(sql, **params):
    return db.session.execute(text(sql), params)


def _audit(action, actor, applicant=None, term_id=None, details=None, student_user_id=None):
    db.session.add(EnrollmentAuditLog(
        action=action, student_user_id=student_user_id or (applicant.student_user_id if applicant else None),
        applicant_id=applicant.applicant_id if applicant else None, academic_term_id=term_id,
        details=json.dumps(details, default=str, ensure_ascii=False) if details is not None else None,
        performed_by=actor.name, performed_role=actor.role, performed_at=datetime.now()))


def _raw_cursor():
    """A mysql-connector cursor on the SQLAlchemy session's own connection, so the
    existing raw-SQL helpers of auth.py run inside the same transaction."""
    return db.session.connection().connection.cursor(dictionary=True)


def active_term():
    return AcademicTerm.query.filter_by(is_active=True).first()


def student_kind(applicant):
    """'new' or 'existing' (enrollment_student_type, else the student account type)."""
    t = (applicant.enrollment_student_type or '').strip().lower()
    if t in ('new', 'existing'):
        return t
    su = db.session.get(StudentUser, applicant.student_user_id) if applicant.student_user_id else None
    return 'existing' if su and (su.student_type or '').lower() == 'old' else 'new'


def requirements(include_inactive=False):
    q = EnrollmentChecklistRequirement.query
    if not include_inactive:
        q = q.filter_by(is_active=True)
    return q.order_by(EnrollmentChecklistRequirement.sort_order, EnrollmentChecklistRequirement.id).all()


def active_checkin(student_user_id, term_id):
    return EnrollmentCheckin.query.filter_by(student_user_id=student_user_id, academic_term_id=term_id,
                                             active=True).first()


def is_checked_in(student_user_id, term_id):
    return active_checkin(student_user_id, term_id) is not None


def _form(student_user_id, term_id):
    return RegistrationForm.query.filter_by(student_user_id=student_user_id, academic_term_id=term_id).first()


def _applicant(applicant_id, lock=False):
    if lock:
        # serialises approvals / check-ins of one student
        _q("SELECT applicant_id FROM applicants WHERE applicant_id = :a FOR UPDATE", a=applicant_id)
    a = db.session.get(Applicant, applicant_id)
    if a is None:
        raise CheckinError('Student application not found.')
    if not a.student_user_id:
        raise CheckinError('This application is not linked to a student account.')
    return a


def _term(term_id):
    t = db.session.get(AcademicTerm, int(term_id)) if term_id else None
    if t is None:
        raise CheckinError('Academic term not found.')
    return t


# ---------------------------------------------------------------------------
# checklist read model
# ---------------------------------------------------------------------------

def checklist(applicant, term_id, actor_role=None):
    """Every active checklist line for this student and term, with its state.

    Returns a dict: items, ready (all required lines met), blockers (labels),
    checkin (active EnrollmentCheckin or None), history, form, summary, policy."""
    from website.finance import service as finance
    kind = student_kind(applicant)
    form = _form(applicant.student_user_id, term_id)
    fin = finance.summary(applicant.student_user_id, term_id)
    pol_ok, pol_why = finance.check_policy(applicant.student_user_id, term_id, fin)
    policy = finance.get_policy()
    rows = {i.requirement_id: i for i in EnrollmentChecklistItem.query.filter_by(
        student_user_id=applicant.student_user_id, academic_term_id=term_id).all()}
    items, blockers = [], []
    for req in requirements():
        if req.applies_to != 'all' and req.applies_to != kind:
            continue
        row = rows.get(req.id)
        approved = bool(row and row.status == 'approved')
        if req.kind == 'regform':
            met = bool(form and form.status == 'finalized')
            detail = ('Finalized by %s, %s' % (form.finalized_by, form.finalized_at.strftime('%b %d, %Y %I:%M %p'))
                      if met and form.finalized_at else
                      ('Registration form is %s.' % form.status if form else 'No registration form for this term yet.'))
        elif req.kind == 'payment':
            met = bool(pol_ok)
            detail = pol_why
        else:
            met = approved
            detail = (row.notes or '') if approved else (
                'Revoked by %s: %s' % (row.revoked_by, row.revoke_reason) if row and row.status == 'revoked' else 'Waiting for approval.')
        # who may tick it: manual lines, and the clearance line while the policy is 'Accounting clearance'
        approvable = req.kind == 'manual' or (req.kind == 'payment' and policy['mode'] == 'clearance')
        items.append({
            'req': req, 'met': met, 'required': req.is_required, 'detail': detail, 'row': row,
            'approved': approved, 'approvable': approvable,
            'can_act': approvable and actor_role in req.roles,
            'by': row.approved_by if approved else None, 'at': row.approved_at if approved else None,
        })
        if req.is_required and not met:
            blockers.append(req.label)
    history = EnrollmentCheckin.query.filter_by(student_user_id=applicant.student_user_id, academic_term_id=term_id) \
        .order_by(EnrollmentCheckin.id.desc()).all()
    return {'items': items, 'ready': not blockers, 'blockers': blockers,
            'checkin': next((h for h in history if h.active), None), 'history': history,
            'form': form, 'summary': fin, 'policy': policy, 'policy_ok': pol_ok, 'policy_why': pol_why,
            'kind': kind}


# ---------------------------------------------------------------------------
# approving / revoking lines
# ---------------------------------------------------------------------------

def _requirement(code):
    req = EnrollmentChecklistRequirement.query.filter_by(code=code, is_active=True).first()
    if req is None:
        raise CheckinError('Unknown or inactive checklist item.')
    return req


def _check_can_act(actor, req, applicant, term_id):
    if actor.role not in req.roles:
        raise CheckinError('Only %s can approve "%s".' % (' or '.join(req.roles), req.label))
    if req.kind == 'regform':
        raise CheckinError('"%s" is met automatically when the registration form is finalized.' % req.label)
    if req.kind == 'payment':
        from website.finance import service as finance
        if finance.get_policy()['mode'] != 'clearance':
            raise CheckinError('The enrollment payment policy is not "Accounting clearance"; '
                               'this item is met automatically from the payments.')
    if is_checked_in(applicant.student_user_id, term_id):
        raise CheckinError('The student is already checked in for this term. Undo the check-in first.')


def _set_accounting_flag(applicant, term_id, value):
    """Keep the existing per-term Accounting flag (read by the finance policy and old screens) in step."""
    _q("""INSERT INTO student_enrollment_records (student_user_id, academic_term_id, is_approved_by_accounting, accounting_approval_date)
          VALUES (:s, :t, :v, :d)
          ON DUPLICATE KEY UPDATE is_approved_by_accounting = VALUES(is_approved_by_accounting),
                                  accounting_approval_date = VALUES(accounting_approval_date)""",
       s=applicant.student_user_id, t=term_id, v=1 if value else 0, d=datetime.now() if value else None)


def approve_item(actor, applicant_id, term_id, code, notes=None):
    applicant = _applicant(applicant_id, lock=True)
    term = _term(term_id)
    req = _requirement(code)
    if req.applies_to != 'all' and req.applies_to != student_kind(applicant):
        raise CheckinError('"%s" does not apply to this student.' % req.label)
    _check_can_act(actor, req, applicant, term.id)
    row = EnrollmentChecklistItem.query.filter_by(student_user_id=applicant.student_user_id, academic_term_id=term.id,
                                                  requirement_id=req.id).with_for_update().first()
    if row and row.status == 'approved':
        raise CheckinError('"%s" is already approved.' % req.label)
    if row is None:
        row = EnrollmentChecklistItem(student_user_id=applicant.student_user_id, academic_term_id=term.id, requirement_id=req.id)
        db.session.add(row)
    row.status, row.notes = 'approved', ((notes or '').strip()[:500] or None)
    row.approved_by, row.approved_role, row.approved_at = actor.name, actor.role, datetime.now()
    row.revoked_by = row.revoked_at = row.revoke_reason = None
    if req.kind == 'payment':
        _set_accounting_flag(applicant, term.id, True)
    _audit('approve_item', actor, applicant, term.id, {'code': req.code, 'label': req.label, 'notes': row.notes})
    return row


def revoke_item(actor, applicant_id, term_id, code, reason):
    applicant = _applicant(applicant_id, lock=True)
    term = _term(term_id)
    req = _requirement(code)
    reason = (reason or '').strip()
    if not reason:
        raise CheckinError('Give a reason for revoking "%s".' % req.label)
    _check_can_act(actor, req, applicant, term.id)
    row = EnrollmentChecklistItem.query.filter_by(student_user_id=applicant.student_user_id, academic_term_id=term.id,
                                                  requirement_id=req.id).with_for_update().first()
    if not row or row.status != 'approved':
        raise CheckinError('"%s" is not approved.' % req.label)
    row.status = 'revoked'
    row.revoked_by, row.revoked_at, row.revoke_reason = actor.name, datetime.now(), reason[:500]
    if req.kind == 'payment':
        _set_accounting_flag(applicant, term.id, False)
    _audit('revoke_item', actor, applicant, term.id, {'code': req.code, 'label': req.label, 'reason': reason,
                                                      'approved_by': row.approved_by, 'approved_at': row.approved_at})
    return row


# ---------------------------------------------------------------------------
# check-in
# ---------------------------------------------------------------------------

def check_in(actor, applicant_id, term_id=None, manual_section_id=None):
    """Officially enroll the student for the ACTIVE term, once every required checklist line is met.

    Replaces the steps of the old Approve button (section, permanent student ID,
    fees, status 'Enrolled', per-term record) and records who checked the student
    in, with a snapshot of the checklist. Returns a dict for the caller's message/email."""
    if actor.role not in CHECKIN_ROLES:
        raise CheckinError('Only the registrar or an administrator can check a student in.')
    term = active_term()
    if term is None:
        raise CheckinError('There is no active academic term.')
    if term_id and int(term_id) != term.id:
        raise CheckinError('Check-in is only possible for the active term (%s %s).' % (term.year_name, term.semester))
    applicant = _applicant(applicant_id, lock=True)
    if applicant.application_status not in CHECKIN_STATUSES:
        raise CheckinError('Only students who are enrolling can be checked in (status: %s).' % applicant.application_status)
    if is_checked_in(applicant.student_user_id, term.id):
        raise CheckinError('%s %s is already checked in for this term.' % (applicant.first_name, applicant.last_name))

    cl = checklist(applicant, term.id, actor.role)
    if not cl['ready']:
        raise CheckinError('Not ready for check-in. Still needed: ' + '; '.join(cl['blockers']) + '.')

    form = cl['form']
    from website import auth as legacy
    cur = _raw_cursor()
    try:
        # A. section: explicit choice > the registration form's block section > keep / auto-assign
        section_id = None
        if manual_section_id not in (None, '', 'None'):
            sec = db.session.get(Section, int(manual_section_id))
            if sec is None or not sec.is_active:
                raise CheckinError('The selected section does not exist or is not active.')
            section_id = sec.id
        elif form is not None and form.section_id:
            section_id = form.section_id
        if section_id:
            cur.execute("UPDATE applicants SET section_id = %s, is_section_permanent = TRUE WHERE applicant_id = %s",
                        (section_id, applicant.applicant_id))
        elif not applicant.section_id:
            cur.execute("SELECT program_id FROM programs WHERE title = %s", (applicant.program_choice,))
            prog = cur.fetchone()
            if prog:
                legacy._assign_section_automatically(cur, applicant.applicant_id, prog['program_id'],
                                                     applicant.enrollment_year_level)

        # B. permanent student ID for new students (unchanged rule of the uploaded system)
        new_id = legacy._assign_permanent_student_id(cur, applicant.applicant_id)

        # C. fees: the Phase 4 assessment of the finalized form (idempotent); the old
        #    automatic fee step only when the registration form line is switched off
        fee_note = ''
        if form is not None and form.status == 'finalized':
            from website.finance import service as finance
            _, rep = finance.generate_for_form(form, actor)
            if rep['created']:
                fee_note = ' %d new charge(s) added.' % len(rep['created'])
        else:
            cur.execute("""SELECT student_user_id, first_name, last_name, email_address, program_choice,
                                  enrollment_year_level, enrollment_semester, enrollment_student_type, section_id,
                                  old_student_id, control_number, academic_year, permanent_address_city_municipality
                           FROM applicants WHERE applicant_id = %s""", (applicant.applicant_id,))
            added = legacy._auto_assign_fees(cur, applicant.applicant_id, cur.fetchone())
            if added:
                fee_note = ' %d new assessment(s) added to the ledger.' % added

        # D. status and the per-term record
        cur.execute("""UPDATE applicants SET application_status = 'Enrolled', original_enrollment_status = NULL,
                              last_updated_at = NOW() WHERE applicant_id = %s""", (applicant.applicant_id,))
        cur.execute("""INSERT INTO student_enrollment_records
                           (student_user_id, academic_term_id, year_level, enrollment_semester, enrollment_status)
                       VALUES (%s, %s, %s, %s, 'Enrolled')
                       ON DUPLICATE KEY UPDATE enrollment_status = 'Enrolled',
                           year_level = COALESCE(VALUES(year_level), year_level),
                           enrollment_semester = COALESCE(VALUES(enrollment_semester), enrollment_semester)""",
                    (applicant.student_user_id, term.id, applicant.enrollment_year_level, applicant.enrollment_semester))
    finally:
        cur.close()

    snapshot = [{'code': i['req'].code, 'label': i['req'].label, 'required': i['required'], 'met': i['met'],
                 'detail': i['detail'], 'by': i['by'], 'at': i['at']} for i in cl['items']]
    snapshot.append({'code': '_finance', 'total_assessment': cl['summary']['total_assessment'],
                     'remaining': cl['summary']['remaining'], 'policy': cl['policy']['mode']})
    ci = EnrollmentCheckin(student_user_id=applicant.student_user_id, applicant_id=applicant.applicant_id,
                           academic_term_id=term.id, registration_form_id=form.id if form else None, active=True,
                           checklist_snapshot=json.dumps(snapshot, default=str, ensure_ascii=False),
                           checked_in_by=actor.name, checked_in_role=actor.role, checked_in_at=datetime.now())
    db.session.add(ci)
    db.session.flush()
    db.session.expire_all()
    a = db.session.get(Applicant, applicant_id)
    _audit('check_in', actor, a, term.id, {'checkin_id': ci.id, 'new_student_id': new_id or None,
                                           'section_id': a.section_id, 'checked_in_by': actor.name})
    return {'checkin': ci, 'new_id': new_id, 'fee_note': fee_note, 'email': a.email_address,
            'name': '%s %s' % (a.first_name, a.last_name), 'applicant_id': a.applicant_id}


def undo_check_in(actor, applicant_id, term_id, reason):
    """Withdraw a check-in (e.g. done by mistake). The check-in row, student ID,
    section and charges stay; the student returns to 'Enrolling'."""
    if actor.role not in CHECKIN_ROLES:
        raise CheckinError('Only the registrar or an administrator can undo a check-in.')
    reason = (reason or '').strip()
    if not reason:
        raise CheckinError('Give a reason for undoing the check-in.')
    applicant = _applicant(applicant_id, lock=True)
    term = _term(term_id)
    ci = EnrollmentCheckin.query.filter_by(student_user_id=applicant.student_user_id, academic_term_id=term.id,
                                           active=True).with_for_update().first()
    if ci is None:
        raise CheckinError('The student is not checked in for this term.')
    ci.active = None
    ci.undone_by, ci.undone_role, ci.undone_at, ci.undo_reason = actor.name, actor.role, datetime.now(), reason[:500]
    if applicant.application_status == 'Enrolled':
        applicant.application_status = 'Enrolling'
        _q("UPDATE applicants SET last_updated_at = NOW() WHERE applicant_id = :a", a=applicant.applicant_id)
    _q("UPDATE student_enrollment_records SET enrollment_status = 'Enrolling' WHERE student_user_id = :s AND academic_term_id = :t",
       s=applicant.student_user_id, t=term.id)
    _audit('undo_check_in', actor, applicant, term.id, {'checkin_id': ci.id, 'reason': reason})
    return ci


# ---------------------------------------------------------------------------
# registrar queue
# ---------------------------------------------------------------------------

def queue(term, only=None):
    """Students enrolling in (or checked in for) the term, with checklist progress."""
    rows = []
    apps = Applicant.query.filter(Applicant.application_status.in_(CHECKIN_STATUSES),
                                  Applicant.student_user_id.isnot(None)) \
        .order_by(Applicant.last_name, Applicant.first_name).all()
    for a in apps:
        ci = active_checkin(a.student_user_id, term.id)
        if a.application_status == 'Enrolled' and ci is None and _form(a.student_user_id, term.id) is None:
            continue          # enrolled in an earlier term and not (yet) enrolling for this one
        cl = checklist(a, term.id)
        state = 'checked_in' if ci else ('ready' if cl['ready'] else 'pending')
        if only and state != only:
            continue
        rows.append({'applicant': a, 'cl': cl, 'state': state, 'checkin': ci,
                     'done': sum(1 for i in cl['items'] if i['met']), 'total': len(cl['items'])})
    return rows


# ---------------------------------------------------------------------------
# administrator: checklist lines
# ---------------------------------------------------------------------------

def save_requirement(actor, data, requirement_id=None):
    if actor.role != 'admin':
        raise CheckinError('Only an administrator can change the enrollment checklist.')
    label = (data.get('label') or '').strip()
    if not label:
        raise CheckinError('Enter a label.')
    roles = [r for r in data.get('approver_roles', []) if r in ADMISSION_ROLES]
    applies = data.get('applies_to') or 'all'
    if applies not in APPLIES:
        raise CheckinError('Choose who the item applies to.')
    if requirement_id:
        req = db.session.get(EnrollmentChecklistRequirement, int(requirement_id))
        if req is None:
            raise CheckinError('Checklist item not found.')
        before = {'label': req.label, 'roles': req.approver_roles, 'applies_to': req.applies_to,
                  'required': req.is_required, 'active': req.is_active}
    else:
        req, before = EnrollmentChecklistRequirement(kind='manual'), None
        code = ''.join(c if c.isalnum() else '_' for c in label.lower())[:30].strip('_') or 'item'
        base, n = code, 2
        while EnrollmentChecklistRequirement.query.filter_by(code=code).first():
            code, n = '%s_%d' % (base, n), n + 1
        req.code = code
        db.session.add(req)
    if req.kind != 'regform' and not roles:
        raise CheckinError('Choose at least one role that approves this item.')
    req.label = label[:150]
    req.description = (data.get('description') or '').strip()[:500] or None
    if req.kind != 'regform':
        req.approver_roles = ','.join(roles)
    req.applies_to = applies
    req.is_required = bool(data.get('is_required'))
    req.is_active = bool(data.get('is_active'))
    try:
        req.sort_order = int(data.get('sort_order') or req.sort_order or 100)
    except ValueError:
        raise CheckinError('Order must be a whole number.')
    db.session.flush()
    _audit('requirement', actor, None, None, {'id': req.id, 'code': req.code, 'before': before,
                                              'after': {'label': req.label, 'roles': req.approver_roles,
                                                        'applies_to': req.applies_to, 'required': req.is_required,
                                                        'active': req.is_active}})
    return req
