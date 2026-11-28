"""Accounting screens for enrollment assessments, verification and policy (Phase 4).

URL prefix: /accounting/enrollment-finance
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from urllib.parse import urlparse

from website import db
from website.models import AcademicTerm, EnrollmentAssessment, Applicant, YEAR_LABELS
from website.curriculum.service import Actor
from website.finance import service
from website.finance.service import FinanceError
from website.checkin.service import is_checked_in
from sqlalchemy import text

finance = Blueprint('finance', __name__)

VIEW_ROLES = ('cashier', 'accounting', 'admin')


@finance.app_template_filter('peso')
def _peso(value):
    return service.peso(value)


def _actor():
    return Actor(session.get('user_name'), session.get('user_role'))


def _back(default):
    ref = request.referrer or ''
    return redirect(ref if ref and urlparse(ref).netloc == request.host else default)


@finance.route('/')
def index():
    role = session.get('user_role')
    if role not in VIEW_ROLES:
        return redirect(url_for('auth.admin_login'))
    terms = AcademicTerm.query.order_by(AcademicTerm.year_name.desc(), AcademicTerm.semester.desc()).all()
    active = AcademicTerm.query.filter_by(is_active=True).first()
    term_id = request.args.get('term_id', type=int) or (active.id if active else (terms[0].id if terms else None))
    status = request.args.get('status') or ''
    rows = []
    if term_id:
        for ea in (EnrollmentAssessment.query.filter_by(academic_term_id=term_id)
                   .join(Applicant, Applicant.applicant_id == EnrollmentAssessment.applicant_id)
                   .order_by(Applicant.last_name, Applicant.first_name).all()):
            s = service.summary(ea.student_user_id, term_id)
            ok, why = service.check_policy(ea.student_user_id, term_id, s)
            label = s['verification_status'] if status in ('For Verification', 'Payment Verified') else s['payment_status']
            if status and label != status:
                continue
            rows.append({'ea': ea, 'applicant': db.session.get(Applicant, ea.applicant_id), 's': s, 'policy_ok': ok, 'policy_why': why,
                         'cleared': _cleared(ea.student_user_id, term_id), 'checked_in': is_checked_in(ea.student_user_id, term_id)})
    # down payments of the term that are not yet on a charge (the student may have no assessment yet)
    down = []
    if term_id:
        from website.models import DownPayment
        for dp in DownPayment.query.filter_by(academic_term_id=term_id, status='held').order_by(DownPayment.id).all():
            p = db.session.execute(text(
                "SELECT p.id, p.amount_paid, p.payment_date, p.receipt_number, p.cashier_username, pv.verified_by, "
                "(pr.id IS NOT NULL) AS reversed FROM payments p LEFT JOIN payment_verifications pv ON pv.payment_id = p.id "
                "LEFT JOIN payment_reversals pr ON pr.payment_id = p.id WHERE p.id = :p"), {'p': dp.payment_id}).mappings().first()
            if p and not p['reversed']:
                down.append({'dp': dp, 'p': p, 'applicant': db.session.get(Applicant, dp.applicant_id)})
    base = 'admin_base.html' if role == 'admin' else 'accounting_base.html'
    return render_template('finance/index.html', rows=rows, terms=terms, term_id=term_id, status=status,
                           policy=service.get_policy(), modes=service.POLICY_MODES, base_template=base,
                           can_edit_policy=role in ('accounting', 'admin'), years=YEAR_LABELS, active_page='enrollment_finance',
                           down=down)


def _cleared(student_user_id, term_id):
    return bool(db.session.execute(text("SELECT is_approved_by_accounting FROM student_enrollment_records "
                                        "WHERE student_user_id = :s AND academic_term_id = :t"),
                                   {'s': student_user_id, 't': term_id}).scalar())


@finance.route('/policy', methods=['POST'])
def policy():
    if session.get('user_role') not in ('accounting', 'admin'):
        flash('Only accounting or an administrator can change the enrollment payment policy.', 'danger')
        return redirect(url_for('finance.index'))
    try:
        service.save_policy(_actor(), request.form.get('mode'), request.form.get('min_down_payment'),
                            request.form.get('require_verification') == '1')
        db.session.commit()
        flash('Enrollment payment policy saved.', 'success')
    except FinanceError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return redirect(url_for('finance.index'))


@finance.route('/payments/<int:payment_id>/verify', methods=['POST'])
def verify(payment_id):
    if session.get('user_role') not in VIEW_ROLES:
        return redirect(url_for('auth.admin_login'))
    try:
        service.verify_payment(_actor(), payment_id, request.form.get('notes'))
        db.session.commit()
        flash(f'Payment #{payment_id} verified.', 'success')
    except FinanceError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return _back(url_for('finance.index'))


@finance.route('/forms/<int:form_id>/regenerate', methods=['POST'])
def regenerate(form_id):
    if session.get('user_role') != 'accounting':
        flash('Only accounting can regenerate an assessment.', 'danger')
        return _back(url_for('finance.index'))
    try:
        ea, rep = service.regenerate(form_id, _actor())
        db.session.commit()
        msg = f"Assessment updated: {len(rep['created'])} new charge(s), {len(rep['adopted'])} existing charge(s) linked."
        if rep['no_longer_matching']:
            msg += ' No longer matching the fee rules (adjust manually if needed): ' + ', '.join(rep['no_longer_matching'])
        flash(msg, 'success')
    except FinanceError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return _back(url_for('finance.index'))


# ---------------------------------------------------------------------------
# fee descriptions (the dropdown of the batch assessment form)
# ---------------------------------------------------------------------------

@finance.route('/fee-descriptions', methods=['POST'])
def add_fee_description():
    if session.get('user_role') not in VIEW_ROLES:
        return redirect(url_for('auth.admin_login'))
    try:
        fd, created = service.add_fee_description(_actor(), request.form.get('name'))
        db.session.commit()
        flash(f'"{fd.name}" {"added to" if created else "is back in"} the fee descriptions.', 'success')
    except FinanceError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return redirect(url_for('auth.create_batch_assessment') + '#fee-descriptions')


@finance.route('/fee-descriptions/<int:fd_id>/<action>', methods=['POST'])
def toggle_fee_description(fd_id, action):
    if session.get('user_role') not in VIEW_ROLES:
        return redirect(url_for('auth.admin_login'))
    try:
        fd = service.set_fee_description_active(_actor(), fd_id, action == 'show')
        db.session.commit()
        flash(f'"{fd.name}" is {"shown in" if fd.is_active else "hidden from"} the dropdown.', 'success')
    except FinanceError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return redirect(url_for('auth.create_batch_assessment') + '#fee-descriptions')
