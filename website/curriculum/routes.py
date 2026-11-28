"""Registrar Curriculum Management screens (Phase 2).

URL prefix: /admin/curriculum  (registered in website/__init__.py)
Access: admission-side 'admin' and 'registrar' accounts (same session keys as
the rest of the registrar module). Every POST is CSRF-protected app-wide.
"""

from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort

from website import db
from website.models import (CurriculumVersion, CurriculumSubject, CurriculumImportBatch, CurriculumAuditLog,
                            Program, SEMESTER_LABELS, YEAR_LABELS)
from website.curriculum import service, importer
from website.curriculum.service import CurriculumError, Actor

curriculum = Blueprint('curriculum', __name__)

MANAGE_ROLES = ('admin', 'registrar')


def registrar_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('user_role') not in MANAGE_ROLES:
            flash('Only the registrar or an administrator can manage curricula.', 'danger')
            return redirect(url_for('auth.admin'))
        return view(*args, **kwargs)
    return wrapped


def _actor():
    return Actor(session.get('user_name'), session.get('user_role'))


def _fail(e):
    db.session.rollback()
    for m in (e.messages if isinstance(e, CurriculumError) else [str(e)]):
        flash(m, 'danger')


def _version_or_404(version_id):
    v = db.session.get(CurriculumVersion, version_id)
    if v is None:
        abort(404)
    return v


def _programs():
    return Program.query.order_by(Program.program_code, Program.title).all()


@curriculum.app_template_filter('units')
def _units_filter(value):
    """3.0 -> '3', 1.5 -> '1.5' (Decimal or float)."""
    if value is None or value == '':
        return ''
    from decimal import Decimal
    d = Decimal(str(value))
    return str(int(d)) if d == d.to_integral_value() else str(d.quantize(Decimal('0.1')))


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

@curriculum.route('/')
@registrar_required
def index():
    programs = _programs()
    selected = request.args.get('program') or ''
    rows = []
    for p in programs:
        if selected and p.id != selected:
            continue
        versions = p.curriculum_versions.order_by(CurriculumVersion.effective_academic_year.desc(), CurriculumVersion.id.desc()).all()
        rows.append((p, [(v, service.version_summary(v)) for v in versions]))
    pending = (CurriculumImportBatch.query.filter_by(status='pending_review')
               .order_by(CurriculumImportBatch.id.desc()).limit(20).all())
    return render_template('curriculum/index.html', rows=rows, programs=programs, selected=selected,
                           pending_imports=pending, active_page='curriculum')


@curriculum.route('/versions/new', methods=['POST'])
@registrar_required
def create_version():
    f = request.form
    try:
        v = service.create_version(f.get('program_id'), f.get('name'), f.get('effective_academic_year'), _actor(),
                                   description=f.get('description'), max_year_level=f.get('max_year_level') or 4)
        db.session.commit()
    except CurriculumError as e:
        _fail(e)
        return redirect(url_for('curriculum.index', program=f.get('program_id') or ''))
    flash(f"Draft curriculum '{v.name}' created. Add subjects, then activate it.", 'success')
    return redirect(url_for('curriculum.version_detail', version_id=v.id))


# ---------------------------------------------------------------------------
# one version
# ---------------------------------------------------------------------------

@curriculum.route('/versions/<int:version_id>')
@registrar_required
def version_detail(version_id):
    v = _version_or_404(version_id)
    terms = service.grouped_subjects(v)
    all_subjects = [s for _, _, subs, _ in terms for s in subs]
    log = (CurriculumAuditLog.query.filter_by(curriculum_version_id=v.id)
           .order_by(CurriculumAuditLog.id.desc()).limit(50).all())
    edit_id = request.args.get('edit', type=int)
    return render_template('curriculum/version_detail.html', v=v, terms=terms, all_subjects=all_subjects,
                           summary=service.version_summary(v), lock_reason=service.lock_reason(v),
                           report=service.completeness(v), log=log, edit_id=edit_id,
                           semesters=SEMESTER_LABELS, years=YEAR_LABELS, active_page='curriculum')


@curriculum.route('/versions/<int:version_id>/update', methods=['POST'])
@registrar_required
def update_version(version_id):
    f = request.form
    try:
        service.update_version(version_id, _actor(), f.get('name'), f.get('effective_academic_year'),
                               f.get('description'), f.get('max_year_level') or 4)
        db.session.commit()
        flash('Curriculum details saved.', 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/subjects/add', methods=['POST'])
@registrar_required
def add_subject(version_id):
    f = request.form
    try:
        s = service.add_subject(version_id, _actor(), f.get('subject_code'), f.get('description'), f.get('units'),
                                f.get('year_level'), f.get('semester'), f.get('notes'))
        db.session.commit()
        flash(f'{s.subject_code} added to {service.term_label(s.year_level, s.semester)}.', 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id) + '#add-subject')


@curriculum.route('/subjects/<int:subject_id>/update', methods=['POST'])
@registrar_required
def update_subject(subject_id):
    s = db.session.get(CurriculumSubject, subject_id) or abort(404)
    version_id = s.curriculum_version_id
    f = request.form
    try:
        service.update_subject(subject_id, _actor(), f.get('subject_code'), f.get('description'), f.get('units'),
                               f.get('year_level'), f.get('semester'), f.get('notes'))
        db.session.commit()
        flash('Subject updated.', 'success')
        return redirect(url_for('curriculum.version_detail', version_id=version_id) + f'#subject-{subject_id}')
    except CurriculumError as e:
        _fail(e)
        return redirect(url_for('curriculum.version_detail', version_id=version_id, edit=subject_id) + f'#subject-{subject_id}')


@curriculum.route('/subjects/<int:subject_id>/delete', methods=['POST'])
@registrar_required
def delete_subject(subject_id):
    s = db.session.get(CurriculumSubject, subject_id) or abort(404)
    version_id, code = s.curriculum_version_id, s.subject_code
    try:
        service.delete_subject(subject_id, _actor())
        db.session.commit()
        flash(f'{code} removed from the curriculum.', 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/subjects/<int:subject_id>/prerequisites/add', methods=['POST'])
@registrar_required
def add_prerequisite(subject_id):
    s = db.session.get(CurriculumSubject, subject_id) or abort(404)
    version_id = s.curriculum_version_id
    f = request.form
    try:
        service.add_prerequisite(subject_id, _actor(), f.get('kind'), prerequisite_subject_id=f.get('prerequisite_subject_id'),
                                 min_year_level=f.get('min_year_level'), min_units=f.get('min_units'),
                                 requirement_text=f.get('requirement_text'))
        db.session.commit()
        flash(f'Prerequisite added to {s.subject_code}.', 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id) + f'#subject-{subject_id}')


@curriculum.route('/prerequisites/<int:prereq_id>/delete', methods=['POST'])
@registrar_required
def delete_prerequisite(prereq_id):
    from website.models import CurriculumPrerequisite
    p = db.session.get(CurriculumPrerequisite, prereq_id) or abort(404)
    version_id, subject_id = p.subject.curriculum_version_id, p.curriculum_subject_id
    try:
        service.delete_prerequisite(prereq_id, _actor())
        db.session.commit()
        flash('Prerequisite removed.', 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id) + f'#subject-{subject_id}')


@curriculum.route('/versions/<int:version_id>/activate', methods=['POST'])
@registrar_required
def activate(version_id):
    try:
        v = service.activate_version(version_id, _actor(), confirm_incomplete=request.form.get('confirm_incomplete') == '1',
                                     note=request.form.get('note'))
        db.session.commit()
        flash(f"'{v.name}' is now active and can be assigned to students.", 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/archive', methods=['POST'])
@registrar_required
def archive(version_id):
    try:
        v, still = service.archive_version(version_id, _actor(), note=request.form.get('note'))
        db.session.commit()
        msg = f"'{v.name}' archived."
        if still:
            msg += f' {still} student(s) still follow it; their records keep this curriculum.'
        flash(msg, 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/set-default', methods=['POST'])
@registrar_required
def set_default(version_id):
    try:
        v = service.set_default_version(version_id, _actor())
        db.session.commit()
        flash(f"'{v.name}' is now the default curriculum for new {v.program.program_code or ''} students.", 'success')
    except CurriculumError as e:
        _fail(e)
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/clone', methods=['POST'])
@registrar_required
def clone(version_id):
    f = request.form
    try:
        new = service.clone_version(version_id, _actor(), f.get('name'), f.get('effective_academic_year'))
        db.session.commit()
        flash(f"Copied into a new draft '{new.name}'.", 'success')
        return redirect(url_for('curriculum.version_detail', version_id=new.id))
    except CurriculumError as e:
        _fail(e)
        return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/delete', methods=['POST'])
@registrar_required
def delete_version(version_id):
    v = _version_or_404(version_id)
    program_id, name = v.program_id, v.name
    try:
        service.delete_version(version_id, _actor())
        db.session.commit()
        flash(f"Draft '{name}' deleted.", 'success')
        return redirect(url_for('curriculum.index', program=program_id))
    except CurriculumError as e:
        _fail(e)
        return redirect(url_for('curriculum.version_detail', version_id=version_id))


@curriculum.route('/versions/<int:version_id>/link-legacy', methods=['POST'])
@registrar_required
def link_legacy(version_id):
    v = _version_or_404(version_id)
    n = service.link_legacy_subjects(v, _actor())
    db.session.commit()
    flash(f'{n} subject(s) linked to existing grading subjects with the same code.' if n
          else 'No new matches found in the existing subject list.', 'info')
    return redirect(url_for('curriculum.version_detail', version_id=version_id))


# ---------------------------------------------------------------------------
# students
# ---------------------------------------------------------------------------

STUDENT_STATUSES = ['Enrolled', 'Enrolling', 'Eligible for Enrollment', 'Passed', 'Not Enrolled', 'Dropped']


@curriculum.route('/versions/<int:version_id>/students', methods=['GET', 'POST'])
@registrar_required
def students(version_id):
    v = _version_or_404(version_id)
    if request.method == 'POST':
        try:
            assigned, unchanged = service.assign_students(version_id, request.form.getlist('student_user_id'), _actor(),
                                                          reason=request.form.get('reason'))
            db.session.commit()
            flash(f'{assigned} student(s) assigned to {v.name}.' + (f' {unchanged} already had it.' if unchanged else ''), 'success')
        except CurriculumError as e:
            _fail(e)
        return redirect(url_for('curriculum.students', version_id=version_id,
                                year_level=request.form.get('year_level', ''), status=request.form.get('status', '')))
    year_level = request.args.get('year_level') or ''
    status = request.args.get('status') or ''
    candidates = service.assignment_candidates(v.program, year_level or None, [status] if status else None)
    return render_template('curriculum/version_students.html', v=v, candidates=candidates, year_level=year_level,
                           status=status, statuses=STUDENT_STATUSES, years=YEAR_LABELS, active_page='curriculum')


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

NEW_FIELDS = ('new_name', 'new_effective_academic_year', 'new_max_year_level', 'new_description')


@curriculum.route('/import', methods=['GET', 'POST'])
@registrar_required
def import_upload():
    if request.method == 'POST':
        program = db.session.get(Program, request.form.get('program_id') or '')
        file = request.files.get('file')
        if program is None:
            flash('Choose the program the file belongs to.', 'danger')
            return redirect(url_for('curriculum.import_upload'))
        if not file or not file.filename:
            flash('Choose a plan of study (.docx) or registration form (.xlsx).', 'danger')
            return redirect(url_for('curriculum.import_upload'))
        try:
            batch, previous = importer.stage_upload(program, file.filename, file.read(), _actor())
            db.session.commit()
        except (importer.ImportFormatError, CurriculumError) as e:
            _fail(e)
            return redirect(url_for('curriculum.import_upload'))
        if previous:
            flash(f'This exact file was already uploaded for {program.program_code} (import #{previous.id}, '
                  f'{previous.status.replace("_", " ")}). Review carefully to avoid importing it twice.', 'warning')
        return redirect(url_for('curriculum.import_review', batch_id=batch.id))
    batches = CurriculumImportBatch.query.order_by(CurriculumImportBatch.id.desc()).limit(50).all()
    return render_template('curriculum/import_upload.html', programs=_programs(), batches=batches, active_page='curriculum')


@curriculum.route('/import/<int:batch_id>', methods=['GET', 'POST'])
@registrar_required
def import_review(batch_id):
    batch = db.session.get(CurriculumImportBatch, batch_id) or abort(404)
    parsed, edits = importer.load_batch(batch)
    versions = (CurriculumVersion.query.filter_by(program_id=batch.program_id)
                .order_by(CurriculumVersion.id.desc()).all())
    editable = [x for x in versions if service.lock_reason(x) is None]
    target_id = request.values.get('target_version_id', type=int) or (batch.target_version_id or None)
    mode = request.values.get('mode') or ('existing' if target_id else ('new' if parsed['kind'] == 'plan_of_study' or not editable else 'existing'))
    if mode == 'existing' and not target_id and editable:
        target_id = editable[0].id
    target = next((x for x in versions if x.id == target_id), None) if mode == 'existing' else None

    if request.method == 'POST' and batch.status == 'pending_review':
        edits = importer.edits_from_form(request.form, parsed)
        importer.save_edits(batch, edits, new_fields={k: request.form.get(k, '') for k in NEW_FIELDS})
        if request.form.get('action') == 'commit':
            try:
                new_version = None
                if mode == 'new':
                    new_version = {'name': request.form.get('new_name'), 'description': request.form.get('new_description'),
                                   'effective_academic_year': request.form.get('new_effective_academic_year'),
                                   'max_year_level': request.form.get('new_max_year_level') or 4}
                version, result = importer.commit(batch, _actor(), target_version=target, new_version=new_version, edits=edits)
                db.session.commit()
                flash(f"Imported into '{version.name}': {result['created']} new subject(s), {result['replaced']} updated, "
                      f"{result['kept']} unchanged, {result['prerequisites']} prerequisite(s).", 'success')
                return redirect(url_for('curriculum.version_detail', version_id=version.id))
            except CurriculumError as e:
                _fail(e)
                batch = db.session.get(CurriculumImportBatch, batch_id)
                importer.save_edits(batch, edits, new_fields={k: request.form.get(k, '') for k in NEW_FIELDS})
                db.session.commit()
        else:
            db.session.commit()
            flash('Review updated.', 'info')
        return redirect(url_for('curriculum.import_review', batch_id=batch_id, mode=mode,
                                target_version_id=target.id if target else ''))

    rows, summary = importer.analyse(parsed, target, edits)
    return render_template('curriculum/import_review.html', batch=batch, parsed=parsed, rows=rows, summary=summary,
                           versions=versions, editable=editable, target=target, mode=mode,
                           semesters=SEMESTER_LABELS, years=YEAR_LABELS,
                           new_fields=importer.load_new_fields(batch), active_page='curriculum')


@curriculum.route('/import/<int:batch_id>/discard', methods=['POST'])
@registrar_required
def import_discard(batch_id):
    batch = db.session.get(CurriculumImportBatch, batch_id) or abort(404)
    if batch.status != 'pending_review':
        flash('This import was already processed.', 'warning')
    else:
        batch.status = 'discarded'
        batch.decided_by, batch.decided_at = _actor().name, db.func.now()
        db.session.commit()
        flash('Import discarded. Nothing was changed.', 'info')
    return redirect(url_for('curriculum.import_upload'))
