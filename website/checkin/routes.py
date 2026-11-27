"""Phase 5 screens: check-in queue, checklist actions, check-in / undo, checklist settings.

URL prefix: /admin/enrollment-checkin
"""

from urllib.parse import urlparse

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from website import db
from website.models import AcademicTerm, EnrollmentChecklistRequirement
from website.curriculum.service import Actor
from website.checkin import service
from website.checkin.service import CheckinError

checkin = Blueprint('checkin', __name__)


def _actor():
    return Actor(session.get('user_name'), session.get('user_role'))


def _back(default):
    ref = request.referrer or ''
    return redirect(ref if ref and urlparse(ref).netloc == request.host else default)


def _review_url(applicant_id, term_id=None):
    return url_for('auth.admin_enrollment_review_page', applicant_id=applicant_id, term_id=term_id) + '#checklist'


def _fail(e):
    db.session.rollback()
    flash(' '.join(e.messages), 'danger')


@checkin.route('/')
def queue():
    if session.get('user_role') not in service.CHECKIN_ROLES:
        return redirect(url_for('auth.admin_login'))
    term = service.active_term()
    only = request.args.get('state') or None
    rows = service.queue(term, only) if term else []
    return render_template('checkin/queue.html', rows=rows, term=term, state=only or '',
                           active_page='checkin_queue')


@checkin.route('/<int:applicant_id>/items/<code>/approve', methods=['POST'])
def approve_item(applicant_id, code):
    if session.get('user_role') not in service.ADMISSION_ROLES:
        return redirect(url_for('auth.admin_login'))
    try:
        row = service.approve_item(_actor(), applicant_id, request.form.get('term_id'), code, request.form.get('notes'))
        db.session.commit()
        flash('"%s" approved.' % row.requirement.label, 'success')
    except CheckinError as e:
        _fail(e)
    return _back(_review_url(applicant_id))


@checkin.route('/<int:applicant_id>/items/<code>/revoke', methods=['POST'])
def revoke_item(applicant_id, code):
    if session.get('user_role') not in service.ADMISSION_ROLES:
        return redirect(url_for('auth.admin_login'))
    try:
        row = service.revoke_item(_actor(), applicant_id, request.form.get('term_id'), code, request.form.get('reason'))
        db.session.commit()
        flash('"%s" revoked.' % row.requirement.label, 'success')
    except CheckinError as e:
        _fail(e)
    return _back(_review_url(applicant_id))


@checkin.route('/<int:applicant_id>/check-in', methods=['POST'])
def do_check_in(applicant_id):
    if session.get('user_role') not in service.CHECKIN_ROLES:
        return redirect(url_for('auth.admin_login'))
    term_id = request.form.get('term_id', type=int)
    try:
        res = service.check_in(_actor(), applicant_id, term_id, request.form.get('manual_section_id'))
        db.session.commit()
    except CheckinError as e:
        _fail(e)
        return _back(_review_url(applicant_id, term_id))
    notify_enrolled(res)
    flash('%s is checked in and officially enrolled.%s%s' % (
        res['name'], ' New student ID: %s.' % res['new_id'] if res['new_id'] else '', res['fee_note']), 'success')
    return _back(_review_url(applicant_id, term_id))


@checkin.route('/<int:applicant_id>/undo', methods=['POST'])
def undo(applicant_id):
    if session.get('user_role') not in service.CHECKIN_ROLES:
        return redirect(url_for('auth.admin_login'))
    term_id = request.form.get('term_id', type=int)
    try:
        service.undo_check_in(_actor(), applicant_id, term_id, request.form.get('reason'))
        db.session.commit()
        flash('Check-in withdrawn; the student is back to Enrolling.', 'success')
    except CheckinError as e:
        _fail(e)
    return _back(_review_url(applicant_id, term_id))


@checkin.route('/requirements', methods=['GET', 'POST'])
def requirements():
    if session.get('user_role') != 'admin':
        flash('Only an administrator can change the enrollment checklist.', 'danger')
        return redirect(url_for('checkin.queue') if session.get('user_role') == 'registrar' else url_for('auth.admin_login'))
    if request.method == 'POST':
        data = {k: request.form.get(k) for k in ('label', 'description', 'applies_to', 'is_required', 'is_active', 'sort_order')}
        data['approver_roles'] = request.form.getlist('approver_roles')
        try:
            req = service.save_requirement(_actor(), data, request.form.get('id') or None)
            db.session.commit()
            flash('Checklist item "%s" saved.' % req.label, 'success')
        except CheckinError as e:
            _fail(e)
        return redirect(url_for('checkin.requirements'))
    return render_template('checkin/requirements.html', reqs=service.requirements(include_inactive=True),
                           roles=service.ADMISSION_ROLES, kinds=service.KINDS, applies=service.APPLIES,
                           active_page='checkin_requirements')


def notify_enrolled(res):
    """Same 'Enrolled' e-mail the old Approve button sent. A mail failure never undoes a check-in."""
    try:
        from website.auth import send_application_status_email
        if res.get('email'):
            send_application_status_email(res['email'], res['name'], 'Enrolled', res['applicant_id'])
    except Exception as err:     # noqa: BLE001 - mail problems are logged only
        print(f"[WARN] enrollment e-mail not sent for applicant {res.get('applicant_id')}: {err}")
