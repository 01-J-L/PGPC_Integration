"""Phase 7: Grade Release screen (/admin/grade-release)."""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from website import db
from website.models import GradeHold, Program, Applicant
from website.curriculum.service import Actor
from website.grades import visibility
from website.grades.visibility import GradeVisibilityError

grade_release = Blueprint('grade_release', __name__)


def _actor():
    return Actor(session.get('user_name'), session.get('user_role'))


def _guard():
    return session.get('user_role') in visibility.ROLES


def _run(fn, ok_message):
    try:
        out = fn()
        db.session.commit()
        flash(ok_message(out) if callable(ok_message) else ok_message, 'success')
    except GradeVisibilityError as e:
        db.session.rollback()
        flash(' '.join(e.messages), 'danger')
    return redirect(url_for('grade_release.index'))


@grade_release.route('/')
def index():
    if not _guard():
        return redirect(url_for('auth.admin_login'))
    holds = []
    for h in GradeHold.query.order_by(GradeHold.lifted_at.isnot(None), GradeHold.placed_at.desc()).limit(200).all():
        a = Applicant.query.filter_by(student_user_id=h.student_user_id).order_by(Applicant.applicant_id.desc()).first()
        holds.append({'h': h, 'a': a})
    return render_template('grades/release.html', terms=visibility.overview(), holds=holds,
                           programs=Program.query.order_by(Program.title).all(),
                           unpaid_rule=visibility.unpaid_hold_enabled(), active_page='grade_release')


@grade_release.route('/release', methods=['POST'])
def release():
    if not _guard():
        return redirect(url_for('auth.admin_login'))
    released = request.form.get('action') == 'release'
    f = request.form
    return _run(lambda: visibility.set_release(_actor(), f.get('academic_year'), f.get('semester'), f.get('program_id'),
                                               released, f.get('reason')),
                lambda r: 'Grades for %s (%s) are now %s.' % (visibility.term_label(r.academic_year, r.semester),
                                                            r.program_key or 'all programs',
                                                            'visible to students' if r.is_released else 'withheld'))


@grade_release.route('/holds', methods=['POST'])
def place_hold():
    if not _guard():
        return redirect(url_for('auth.admin_login'))
    f = request.form
    term = (f.get('term') or '').split('|')
    ay, sem = (term + ['', ''])[:2] if term != [''] else ('', '')

    def go():
        suid = visibility.find_student(f.get('student'))
        if not suid:
            raise GradeVisibilityError('No student found for "%s". Use the student no., control no. or e-mail.' % (f.get('student') or ''))
        return visibility.place_hold(_actor(), suid, ay, sem, f.get('reason'))
    return _run(go, 'Hold placed.')


@grade_release.route('/holds/<int:hold_id>/lift', methods=['POST'])
def lift_hold(hold_id):
    if not _guard():
        return redirect(url_for('auth.admin_login'))
    return _run(lambda: visibility.lift_hold(_actor(), hold_id, request.form.get('reason')), 'Hold lifted.')


@grade_release.route('/settings', methods=['POST'])
def settings():
    if not _guard():
        return redirect(url_for('auth.admin_login'))
    on = request.form.get('unpaid_rule') == '1'
    return _run(lambda: visibility.set_unpaid_rule(_actor(), on),
                'Grades are now %s for students with an unpaid balance.' % ('withheld' if on else 'not withheld'))
