"""Registrar REGFORM screens (Phase 3).

URL prefix: /admin/registration (registrar + admin), plus /my-registration
for the logged-in student (own finalized forms only).
"""

from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort

from website import db
from website.models import (AcademicTerm, Applicant, RegistrationForm, RegistrationFormChange, Section, Program,
                            YEAR_LABELS, SEMESTER_LABELS)
from website.curriculum.service import Actor, term_label
from website.registration import service
from website.registration.service import RegformError, NeedsAcknowledgement, fmt_units

registration = Blueprint('registration', __name__)
student_registration = Blueprint('student_registration', __name__)

MANAGE_ROLES = ('admin', 'registrar')


def registrar_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('user_role') not in MANAGE_ROLES:
            flash('Only the registrar or an administrator can manage registration forms.', 'danger')
            return redirect(url_for('auth.admin'))
        return view(*args, **kwargs)
    return wrapped


def _actor():
    return Actor(session.get('user_name'), session.get('user_role'))


def _fail(e):
    db.session.rollback()
    msgs = e.messages if isinstance(e, RegformError) else [str(e)]
    flash(' '.join(msgs), 'danger')


def _active_term():
    return AcademicTerm.query.filter_by(is_active=True).first()


def _form_or_404(form_id):
    f = db.session.get(RegistrationForm, form_id)
    if f is None:
        abort(404)
    return f


@registration.app_template_filter('fmt_units')
def _fmt_units_filter(v):
    return fmt_units(v)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@registration.route('/')
@registrar_required
def index():
    terms = AcademicTerm.query.order_by(AcademicTerm.year_name.desc(), AcademicTerm.semester.desc()).all()
    active = _active_term()
    term_id = request.args.get('term_id', type=int) or (active.id if active else (terms[0].id if terms else None))
    status = request.args.get('status') or ''
    program_id = request.args.get('program') or ''
    q = RegistrationForm.query.filter_by(academic_term_id=term_id) if term_id else RegistrationForm.query.filter(db.false())
    if status:
        q = q.filter_by(status=status)
    if program_id:
        q = q.filter_by(program_id=program_id)
    forms = q.join(Applicant, Applicant.applicant_id == RegistrationForm.applicant_id) \
             .order_by(Applicant.last_name, Applicant.first_name).all()
    # Enrolling / enrolled students of the term with no form yet
    missing = []
    if term_id:
        have = {f.student_user_id for f in RegistrationForm.query.filter_by(academic_term_id=term_id).all()}
        cand = Applicant.query.filter(Applicant.application_status.in_(['Enrolling', 'Enrolled'])) \
                              .order_by(Applicant.last_name, Applicant.first_name).all()
        latest = {}
        for a in cand:
            if a.student_user_id not in latest or a.applicant_id > latest[a.student_user_id].applicant_id:
                latest[a.student_user_id] = a
        missing = [a for sid, a in latest.items() if sid not in have]
        if program_id:
            title = (db.session.get(Program, program_id).title or '').strip() if db.session.get(Program, program_id) else ''
            missing = [a for a in missing if (a.program_choice or '').strip() == title]
    return render_template('registration/index.html', forms=forms, missing=missing, terms=terms, term_id=term_id,
                           status=status, program_id=program_id, programs=Program.query.order_by(Program.program_code).all(),
                           years=YEAR_LABELS, semesters=SEMESTER_LABELS, active_page='registration_forms')


# ---------------------------------------------------------------------------
# one student
# ---------------------------------------------------------------------------

@registration.route('/student/<int:applicant_id>')
@registrar_required
def student(applicant_id):
    applicant = db.session.get(Applicant, applicant_id) or abort(404)
    terms = AcademicTerm.query.order_by(AcademicTerm.year_name.desc(), AcademicTerm.semester.desc()).all()
    active = _active_term()
    term_id = request.args.get('term_id', type=int) or (active.id if active else None)
    if term_id:
        form = service.get_form(applicant.student_user_id, term_id)
        if form is not None:
            return redirect(url_for('registration.form', form_id=form.id))
    ctx, ctx_error, suggestion = None, None, None
    if term_id:
        try:
            ctx = service.Context(applicant_id, term_id)
            suggestion = ctx.suggested_classification()
        except RegformError as e:
            ctx_error = ' '.join(e.messages)
    history_forms = RegistrationForm.query.filter_by(student_user_id=applicant.student_user_id) \
                                          .order_by(RegistrationForm.academic_term_id.desc()).all()
    return render_template('registration/start.html', applicant=applicant, terms=terms, term_id=term_id, ctx=ctx,
                           ctx_error=ctx_error, suggestion=suggestion, history_forms=history_forms,
                           years=YEAR_LABELS, semesters=SEMESTER_LABELS, active_page='registration_forms')


@registration.route('/student/<int:applicant_id>/create', methods=['POST'])
@registrar_required
def create(applicant_id):
    term_id = request.form.get('term_id', type=int)
    try:
        form, _, created = service.create_form(applicant_id, term_id, _actor())
        db.session.commit()
    except RegformError as e:
        _fail(e)
        return redirect(url_for('registration.student', applicant_id=applicant_id, term_id=term_id or ''))
    if created:
        flash(f'{form.classification} registration form created with {len(form.lines)} subject(s).', 'success')
    return redirect(url_for('registration.form', form_id=form.id))


@registration.route('/forms/<int:form_id>')
@registrar_required
def form(form_id):
    f = _form_or_404(form_id)
    ctx, ctx_error = None, None
    available, offerings, sections_by_line, errors, warnings = [], None, {}, [], []
    try:
        ctx = service.context_for(f)
        available, offerings = service.available_subjects(ctx, f)
        for ln in f.lines:
            sections_by_line[ln.id] = service.valid_sections(ctx, offerings, ln.curriculum_subject)
        if f.status == 'draft':
            errors, warnings = service.validate_for_finalize(f, ctx)
    except RegformError as e:
        ctx_error = ' '.join(e.messages)
    block_options = Section.query.filter_by(program_id=f.program_id, is_active=True).order_by(Section.year_level, Section.name).all()
    log = RegistrationFormChange.query.filter_by(registration_form_id=f.id).order_by(RegistrationFormChange.id.desc()).limit(60).all()
    pending_ack = session.pop('regform_ack', None)
    from website.finance import service as finance
    term_summary = finance.summary(f.student_user_id, f.academic_term_id)
    policy_ok, policy_why = finance.check_policy(f.student_user_id, f.academic_term_id, term_summary)
    return render_template('registration/form.html', f=f, ctx=ctx, ctx_error=ctx_error, available=available,
                           sections_by_line=sections_by_line, block_options=block_options, errors=errors,
                           warnings=warnings, log=log, pending_ack=pending_ack, term_label=term_label,
                           term_summary=term_summary, policy_ok=policy_ok, policy_why=policy_why,
                           years=YEAR_LABELS, semesters=SEMESTER_LABELS, section_label=service.section_label,
                           active_page='registration_forms')


def _back(form_id, anchor=''):
    return redirect(url_for('registration.form', form_id=form_id) + anchor)


@registration.route('/forms/<int:form_id>/add', methods=['POST'])
@registrar_required
def add(form_id):
    cs_id = request.form.get('curriculum_subject_id', type=int)
    try:
        ln = service.add_subject(form_id, _actor(), cs_id, section_id=request.form.get('section_id') or None,
                                 acknowledge=request.form.get('acknowledge') == '1', remarks=request.form.get('remarks'))
        db.session.commit()
        flash(f'{ln.subject_code} added.', 'success')
    except NeedsAcknowledgement as e:
        db.session.rollback()
        session['regform_ack'] = {'curriculum_subject_id': cs_id, 'section_id': request.form.get('section_id') or '',
                                  'messages': e.messages}
    except RegformError as e:
        _fail(e)
    return _back(form_id, '#selected')


@registration.route('/forms/<int:form_id>/lines/<int:line_id>/remove', methods=['POST'])
@registrar_required
def remove(form_id, line_id):
    try:
        service.remove_subject(form_id, _actor(), line_id)
        db.session.commit()
        flash('Subject removed.', 'success')
    except RegformError as e:
        _fail(e)
    return _back(form_id, '#selected')


@registration.route('/forms/<int:form_id>/lines/<int:line_id>/section', methods=['POST'])
@registrar_required
def line_section(form_id, line_id):
    try:
        ln = service.assign_section(form_id, _actor(), line_id, request.form.get('section_id'))
        db.session.commit()
        flash(f'{ln.subject_code}: section updated.', 'success')
    except RegformError as e:
        _fail(e)
    return _back(form_id, '#selected')


@registration.route('/forms/<int:form_id>/block-section', methods=['POST'])
@registrar_required
def block_section(form_id):
    try:
        sec, moved = service.assign_block_section(form_id, _actor(), request.form.get('section_id'))
        db.session.commit()
        flash(f'Home section set to {service.section_label(sec)}; {len(moved)} subject(s) placed in it.', 'success')
    except (RegformError, ValueError) as e:
        _fail(e)
    return _back(form_id)


@registration.route('/forms/<int:form_id>/classification', methods=['POST'])
@registrar_required
def classification(form_id):
    try:
        f = service.set_classification(form_id, _actor(), request.form.get('classification'), request.form.get('reason'))
        db.session.commit()
        flash(f'Student marked {f.classification}.', 'success')
    except RegformError as e:
        _fail(e)
    return _back(form_id)


@registration.route('/forms/<int:form_id>/finalize', methods=['POST'])
@registrar_required
def finalize(form_id):
    try:
        f, warnings = service.finalize(form_id, _actor())
        db.session.commit()
        flash('Registration form finalized.' + (' Note: ' + ' '.join(warnings) if warnings else ''), 'success')
    except RegformError as e:
        _fail(e)
    return _back(form_id)


@registration.route('/forms/<int:form_id>/reopen', methods=['POST'])
@registrar_required
def reopen(form_id):
    try:
        service.reopen(form_id, _actor(), request.form.get('reason'))
        db.session.commit()
        flash('Registration form reopened for changes. The change is recorded.', 'info')
    except RegformError as e:
        _fail(e)
    return _back(form_id)


@registration.route('/forms/<int:form_id>/print')
@registrar_required
def print_form(form_id):
    f = _form_or_404(form_id)
    return _render_print(f)


def _render_print(f):
    from datetime import datetime
    return render_template('registration/print.html', f=f, a=f.applicant, years=YEAR_LABELS, semesters=SEMESTER_LABELS,
                           section_label=service.section_label, printed_at=datetime.now())


# ---------------------------------------------------------------------------
# student portal: own finalized forms
# ---------------------------------------------------------------------------

@student_registration.route('/my-registration')
def my_forms():
    sid = session.get('student_id')
    if not sid:
        return redirect(url_for('auth.student_login_page'))
    forms = RegistrationForm.query.filter_by(student_user_id=sid, status='finalized') \
                                  .order_by(RegistrationForm.academic_term_id.desc()).all()
    if not forms:
        flash('You have no finalized registration form yet.', 'info')
        return redirect(url_for('views.application_status_page'))
    return redirect(url_for('student_registration.my_form', form_id=forms[0].id))


@student_registration.route('/my-registration/<int:form_id>')
def my_form(form_id):
    sid = session.get('student_id')
    if not sid:
        return redirect(url_for('auth.student_login_page'))
    f = db.session.get(RegistrationForm, form_id)
    # Ownership + only finalized forms are visible to students.
    if f is None or f.student_user_id != sid or f.status != 'finalized':
        abort(404)
    return _render_print(f)
