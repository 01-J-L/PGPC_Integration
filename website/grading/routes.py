import os
import json
from datetime import datetime
from collections import defaultdict
from functools import wraps

from flask import (
    Blueprint, render_template, url_for, flash, redirect,
    request, session, abort, jsonify, current_app
)
from flask_login import login_user, current_user, logout_user, login_required
from sqlalchemy import func, text
from werkzeug.security import check_password_hash, generate_password_hash

from website import db
from website.models import (
    SystemUser, User, Subject, Grade, Program, Section,
    FacultyAssignment, GradingSettings, Applicant
)

grading_bp = Blueprint(
    'grading', __name__,
    template_folder='../templates/grading'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@grading_bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('faculty_auth.login'))

def check_prerequisites(student_id, subject_id):
    target_subject = Subject.query.get(subject_id)
    student = Applicant.query.get(student_id)
    if not target_subject or not student:
        return False, "Student or subject not found"
    if not target_subject.prerequisites.count():
        return True, "Allowed"
    for prereq in target_subject.prerequisites:
        passed = False
        grade_record = Grade.query.filter_by(
            student_id=student.applicant_id, subject_id=prereq.id
        ).first()
        if grade_record and grade_record.final_grade_point is not None:
            if grade_record.final_grade_point <= 3.0:
                passed = True
        if not passed:
            return False, f"Missing Prerequisite: {prereq.subject_code} - {prereq.subject_title}"
    return True, "Allowed"


def get_admin_program_scope():
    if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
        return []
    if current_user.department == 'CCBA':
        return ['BPA', 'BSCS', 'BSMA']
    elif current_user.department == 'COC':
        return ['BSCRIM']
    return None


def grading_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('faculty_auth.login'))
        return f(*args, **kwargs)
    return decorated_function


# ---------------------------------------------------------------------------
# Authentication Routes (unified - uses /admin-load login)
# ---------------------------------------------------------------------------

@grading_bp.route("/")
def home():
    return redirect(url_for('faculty_auth.login'))


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

@grading_bp.route("/grading-admin/maintenance", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def admin_maintenance():
    if current_user.role != 'superadmin':
        flash('Only the superadministrator can manage maintenance mode.', 'danger')
        return redirect(url_for('grading.admin_dashboard'))

    if request.method == 'POST':
        action = request.form.get('action')
        new_value = '1' if action == 'enable' else '0'
        row = db.session.execute(
            text("SELECT setting_key FROM system_settings WHERE setting_key = 'maintenance_mode'")
        ).fetchone()
        if row:
            db.session.execute(
                text("UPDATE system_settings SET setting_value = :v WHERE setting_key = 'maintenance_mode'"),
                {'v': new_value}
            )
        else:
            db.session.execute(
                text("INSERT INTO system_settings (setting_key, setting_value) VALUES ('maintenance_mode', :v)"),
                {'v': new_value}
            )
        db.session.commit()
        flash(
            "Maintenance mode ENABLED. All systems are now offline." if new_value == '1'
            else "Maintenance mode DISABLED. All systems are back online.",
            "success"
        )
        return redirect(url_for('grading.admin_maintenance'))

    row = db.session.execute(
        text("SELECT setting_value FROM system_settings WHERE setting_key = 'maintenance_mode'")
    ).fetchone()
    maintenance_on = bool(row and row[0] == '1')
    return render_template(
        'grading/admin/admin_maintenance.html',
        title='System Maintenance', active_nav='maintenance',
        maintenance_on=maintenance_on
    )


@grading_bp.route("/grading-admin/dashboard")
@login_required
@grading_admin_required
def admin_dashboard():
    scope = get_admin_program_scope()

    pending_approvals = SystemUser.query.filter_by(role='faculty', can_edit=False).count()
    faculty_count = SystemUser.query.filter_by(role='faculty', can_edit=True).count()

    student_query = Applicant.query.join(Section).join(Program)
    if scope:
        student_query = student_query.filter(Program.program_code.in_(scope))
    student_count = student_query.count()

    dist_query = db.session.query(
        Program.program_code, func.count(Applicant.applicant_id)
    ).select_from(Program).join(Program.sections).join(Section.students if hasattr(Section, 'students') else Section.applicants, isouter=True)
    if scope:
        dist_query = dist_query.filter(Program.program_code.in_(scope))

    try:
        student_dist_results = dist_query.group_by(Program.program_code).all()
    except Exception:
        student_dist_results = []

    student_dist_labels = [row[0] for row in student_dist_results]
    student_dist_data = [row[1] for row in student_dist_results]

    all_students = student_query.all()
    deans_listers_total = 0
    presidents_listers_total = 0
    deans_listers_by_program = defaultdict(int)
    presidents_listers_by_program = defaultdict(int)

    for student in all_students:
        gwa = student.calculate_gwa(year=1, semester=1)
        if gwa > 0:
            grades_for_semester = [
                g for g in student.grades
                if g.subject.year_level == 1 and g.subject.semester == 1
            ]
            has_low_grade = any(
                g.final_grade > 2.25
                for g in grades_for_semester
                if g.final_grade is not None
            )
            if not has_low_grade:
                program_code = student.section.program.program_code if student.section and student.section.program else 'Unknown'
                if gwa <= 1.25:
                    presidents_listers_total += 1
                    presidents_listers_by_program[program_code] += 1
                elif gwa <= 1.75:
                    deans_listers_total += 1
                    deans_listers_by_program[program_code] += 1

    all_programs_query = Program.query.order_by(Program.program_code)
    if scope:
        all_programs_query = all_programs_query.filter(Program.program_code.in_(scope))
    all_program_codes = [p.program_code for p in all_programs_query.all()]
    pl_data = [presidents_listers_by_program.get(code, 0) for code in all_program_codes]
    dl_data = [deans_listers_by_program.get(code, 0) for code in all_program_codes]

    return render_template(
        'grading/admin/dashboard.html',
        title='Grading Admin Dashboard', active_nav='dashboard',
        pending_approvals=pending_approvals,
        student_count=student_count,
        faculty_count=faculty_count,
        deans_listers=deans_listers_total,
        presidents_listers=presidents_listers_total,
        student_dist_labels=json.dumps(student_dist_labels),
        student_dist_data=json.dumps(student_dist_data),
        honor_labels=json.dumps(all_program_codes),
        pl_data=json.dumps(pl_data),
        dl_data=json.dumps(dl_data)
    )


@grading_bp.route("/grading-admin/approvals")
@login_required
@grading_admin_required
def admin_approvals():
    faculty_to_approve = SystemUser.query.filter_by(role='faculty', can_edit=False).all()
    return render_template(
        'grading/admin/approvals.html',
        title='Faculty Approvals', faculty_list=faculty_to_approve,
        active_nav='approvals'
    )


@grading_bp.route("/grading-admin/approve_faculty/<int:user_id>", methods=['POST'])
@login_required
@grading_admin_required
def approve_faculty(user_id):
    user = SystemUser.query.get_or_404(user_id)
    user.can_edit = True
    db.session.commit()
    flash(f'{user.full_name} has been approved as a faculty member.', 'success')
    return redirect(url_for('grading.admin_approvals'))


@grading_bp.route("/grading-admin/student_records")
@login_required
@grading_admin_required
def student_records():
    scope = get_admin_program_scope()
    search_query = request.args.get('search', '')
    program_filter = request.args.get('program', '')
    section_filter = request.args.get('section', '')

    students_query = Applicant.query.join(Section).join(Program).order_by(Applicant.last_name)
    if scope:
        students_query = students_query.filter(Program.program_code.in_(scope))

    if search_query:
        search_term = f"%{search_query}%"
        students_query = students_query.filter(
            db.or_(
                Applicant.old_student_id.ilike(search_term),
                Applicant.first_name.ilike(search_term),
                Applicant.last_name.ilike(search_term)
            )
        )
    if program_filter:
        students_query = students_query.filter(Program.id == program_filter)
    if section_filter:
        sections = [s.strip().upper() for s in section_filter.split(',')]
        students_query = students_query.filter(func.upper(Section.name).in_(sections))

    programs_query = Program.query.order_by(Program.program_code)
    if scope:
        programs_query = programs_query.filter(Program.program_code.in_(scope))
    programs = programs_query.all()
    students = students_query.all()

    return render_template(
        'grading/admin/student_records.html',
        title="Student Records", active_nav='student_records',
        students=students, programs=programs,
        search_query=search_query, program_filter=program_filter,
        section_filter=section_filter
    )


@grading_bp.route("/grading-admin/student/<int:student_id>/grades")
@login_required
@grading_admin_required
def view_student_grades(student_id):
    student = Applicant.query.get_or_404(student_id)
    scope = get_admin_program_scope()
    if scope and student.section and student.section.program and student.section.program.program_code not in scope:
        return redirect(url_for('grading.student_records'))

    selected_year = request.args.get('year', 1, type=int)
    selected_semester = request.args.get('semester', 1, type=int)

    all_subjects = Subject.query.filter_by(
        program_id=student.section.program_id,
        year_level=selected_year, semester=selected_semester
    ).order_by(Subject.subject_title).all()

    grades = Grade.query.filter_by(student_id=student.applicant_id).all()
    grades_dict = {g.subject_id: g for g in grades}

    display_data = [{'subject': s, 'grade': grades_dict.get(s.id)} for s in all_subjects]

    total_points = total_units_graded = total_units_enrolled = 0
    for item in display_data:
        subject = item['subject']
        grade = item['grade']
        total_units_enrolled += float(subject.units)
        if grade and grade.final_grade_point is not None:
            total_points += grade.final_grade_point * float(subject.units)
            total_units_graded += float(subject.units)

    gwa = round(total_points / total_units_graded, 2) if total_units_graded == total_units_enrolled and total_units_enrolled > 0 else 0

    latin_honor = None
    if gwa > 0:
        has_low_grade = any(
            item['grade'] and item['grade'].final_grade_point and item['grade'].final_grade_point > 2.5
            for item in display_data
        )
        if not has_low_grade:
            if gwa <= 1.25:
                latin_honor = "President's Lister"
            elif gwa <= 1.75:
                latin_honor = "Dean's Lister"

    return render_template(
        'grading/admin/view_student_grades.html',
        title=f"Grades for {student.name}", student=student,
        display_data=display_data, gwa=gwa, latin_honor=latin_honor,
        selected_year=selected_year, selected_semester=selected_semester,
        active_nav='student_records'
    )


@grading_bp.route("/grading-admin/student/<int:student_id>/grades/print")
@login_required
@grading_admin_required
def print_student_grades(student_id):
    student = Applicant.query.get_or_404(student_id)
    scope = get_admin_program_scope()
    if scope and student.section and student.section.program and student.section.program.program_code not in scope:
        return "Unauthorized", 403

    selected_year = request.args.get('year', 1, type=int)
    selected_semester = request.args.get('semester', 1, type=int)

    all_subjects = Subject.query.filter_by(
        program_id=student.section.program_id,
        year_level=selected_year, semester=selected_semester
    ).order_by(Subject.subject_title).all()

    grades = Grade.query.filter_by(student_id=student.applicant_id).all()
    grades_dict = {g.subject_id: g for g in grades}

    display_data = [{'subject': s, 'grade': grades_dict.get(s.id)} for s in all_subjects]

    total_points = total_units_graded = total_units_enrolled = 0
    for item in display_data:
        subject = item['subject']
        grade = item['grade']
        total_units_enrolled += float(subject.units)
        if grade and grade.final_grade_point is not None:
            total_points += grade.final_grade_point * float(subject.units)
            total_units_graded += float(subject.units)

    gwa = round(total_points / total_units_graded, 2) if total_units_graded == total_units_enrolled and total_units_enrolled > 0 else 0

    latin_honor = None
    if gwa > 0:
        has_low_grade = any(
            item['grade'] and item['grade'].final_grade_point and item['grade'].final_grade_point > 2.5
            for item in display_data
        )
        if not has_low_grade:
            if gwa <= 1.25:
                latin_honor = "President's Lister"
            elif gwa <= 1.75:
                latin_honor = "Dean's Lister"

    return render_template(
        'grading/admin/print_student_grades.html',
        student=student, display_data=display_data, gwa=gwa,
        latin_honor=latin_honor, selected_year=selected_year,
        selected_semester=selected_semester,
        current_date=datetime.now().strftime("%Y-%m-%d")
    )


@grading_bp.route("/grading-admin/faculty_records")
@login_required
@grading_admin_required
def admin_faculty_records():
    dept_filter = request.args.get('department', '')
    query = SystemUser.query.filter_by(role='faculty')
    if dept_filter:
        query = query.filter(SystemUser.department == dept_filter)
    all_faculty = query.order_by(SystemUser.full_name).all()
    programs = Program.query.order_by(Program.program_code).all()

    return render_template(
        'grading/admin/faculty_records.html',
        title="Faculty Management", active_nav='faculty_records',
        faculties=all_faculty, programs=programs, selected_dept=dept_filter
    )


@grading_bp.route("/grading-admin/add_faculty", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def add_faculty():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        faculty_type = request.form.get('faculty_type', 'minor')
        program_id = request.form.get('program_id', '')

        department_name = "General Education"
        if faculty_type == 'major' and program_id:
            program = Program.query.get(program_id)
            if program:
                department_name = program.program_code

        base_username = (email or name).split('@')[0].lower()
        username = base_username
        suffix = 1
        while SystemUser.query.filter_by(username=username).first():
            username = f"{base_username}{suffix}"
            suffix += 1

        hashed = generate_password_hash(password)
        new_faculty = SystemUser(
            username=username, full_name=name, email=email,
            password=hashed, role='faculty', can_edit=True,
            department=department_name, system='grading'
        )
        db.session.add(new_faculty)
        db.session.commit()
        flash('Faculty member account created successfully.', 'success')
        return redirect(url_for('grading.admin_faculty_records'))

    programs = Program.query.order_by(Program.program_code).all()
    return render_template(
        'grading/admin/add_faculty.html',
        title="Add Faculty", active_nav='faculty_records', programs=programs
    )


@grading_bp.route("/grading-admin/faculty/<int:user_id>/assign", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def admin_assign_loads(user_id):
    faculty = SystemUser.query.get_or_404(user_id)

    programs = Program.query.order_by(Program.program_code).all()
    program_choices = [('', 'Select Program')] + [(str(p.program_id), p.program_code) for p in programs]
    year_choices = [('', 'Select Year'), ('1', '1st'), ('2', '2nd'), ('3', '3rd'), ('4', '4th')]
    semester_choices = [('', 'Select Semester'), ('1', '1st'), ('2', '2nd')]
    section_choices = [('', 'Select Section')]
    subject_choices = [('', 'Select Subject')]

    if request.method == 'POST':
        program_id = request.form.get('program', '')
        year = request.form.get('year', '')
        semester = request.form.get('semester', '')
        section_id = request.form.get('section', '')
        subject_id = request.form.get('subject', '')

        if program_id and year:
            year_label = {'1': '1st Year', '2': '2nd Year', '3': '3rd Year', '4': '4th Year'}.get(year)
            if year_label:
                sections = Section.query.filter_by(
                    program_id=program_id, year_level=year_label
                ).order_by(Section.name).all()
                section_choices = [('', 'Select Section')] + [(str(s.id), s.name) for s in sections]

        if program_id and year and semester:
            subjects = Subject.query.filter_by(
                program_id=program_id, year=int(year), semester=int(semester)
            ).order_by(Subject.subject_title).all()
            subject_choices = [('', 'Select Subject')] + [(str(s.id), s.subject_title) for s in subjects]

        if subject_id and section_id:
            exists = FacultyAssignment.query.filter_by(
                faculty_id=faculty.id, subject_id=int(subject_id), section_id=int(section_id)
            ).first()
            if exists:
                flash('This subject is already assigned to this faculty.', 'warning')
            else:
                new_assign = FacultyAssignment(
                    faculty_id=faculty.id,
                    subject_id=int(subject_id),
                    section_id=int(section_id)
                )
                db.session.add(new_assign)
                db.session.commit()
                flash('Subject assigned successfully.', 'success')
            return redirect(url_for('grading.admin_assign_loads', user_id=faculty.id))

    current_assignments = FacultyAssignment.query.filter_by(faculty_id=faculty.id).all()

    return render_template(
        'grading/admin/assign_loads.html',
        title=f"Assign Loads - {faculty.full_name}",
        faculty=faculty, current_assignments=current_assignments,
        active_nav='faculty_records',
        program_choices=program_choices, year_choices=year_choices,
        semester_choices=semester_choices, section_choices=section_choices,
        subject_choices=subject_choices
    )


@grading_bp.route("/grading-admin/remove_load/<int:assignment_id>", methods=['POST'])
@login_required
@grading_admin_required
def remove_faculty_load(assignment_id):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    faculty_id = assignment.faculty_id
    db.session.delete(assignment)
    db.session.commit()
    flash('Assignment removed.', 'success')
    return redirect(url_for('grading.admin_assign_loads', user_id=faculty_id))


@grading_bp.route("/grading-admin/faculty/delete/<int:user_id>", methods=['POST'])
@login_required
@grading_admin_required
def delete_faculty(user_id):
    faculty_to_delete = SystemUser.query.get_or_404(user_id)
    if faculty_to_delete.role != 'faculty':
        flash('This user is not a faculty member.', 'danger')
        return redirect(url_for('grading.admin_faculty_records'))

    FacultyAssignment.query.filter_by(faculty_id=user_id).delete()
    Grade.query.filter_by(faculty_id=user_id).delete()
    db.session.delete(faculty_to_delete)
    db.session.commit()
    flash(f'Faculty member {faculty_to_delete.full_name} and all associated records deleted.', 'success')
    return redirect(url_for('grading.admin_faculty_records'))


@grading_bp.route("/grading-admin/programs")
@login_required
@grading_admin_required
def manage_programs():
    programs = Program.query.order_by(Program.program_code).all()
    return render_template(
        'grading/admin/manage_programs.html',
        title="Manage Programs", active_nav='programs', programs=programs
    )


@grading_bp.route("/grading-admin/programs/add", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def add_program():
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        if code and name:
            program = Program(program_code=code, title=name, program_name=name)
            db.session.add(program)
            db.session.commit()
            flash(f'Program {code} added successfully!', 'success')
            return redirect(url_for('grading.manage_programs'))
    return render_template(
        'grading/admin/add_program.html',
        title="Add Program", active_nav='programs'
    )


@grading_bp.route("/grading-admin/programs/<program_id>")
@login_required
@grading_admin_required
def view_program_subjects(program_id):
    program = Program.query.get_or_404(program_id)
    subjects = Subject.query.filter_by(program_id=program.id).order_by(
        Subject.year_level, Subject.semester, Subject.subject_code
    ).all()
    return render_template(
        'grading/admin/view_program_subjects.html',
        title=f"{program.program_code} Subjects", active_nav='programs',
        program=program, subjects=subjects
    )


@grading_bp.route("/grading-admin/programs/<program_id>/add_subject", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def add_subject(program_id):
    program = Program.query.get_or_404(program_id)
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        description = request.form.get('description', '').strip()
        units = request.form.get('units', 3, type=int)
        year = request.form.get('year', 1, type=int)
        semester = request.form.get('semester', 1, type=int)
        subject = Subject(
            subject_code=code, subject_title=description,
            units=units, year_level=year, semester=semester,
            program_id=program.id
        )
        db.session.add(subject)
        db.session.commit()
        flash(f'Subject {subject.subject_code} added to {program.program_code}.', 'success')
        return redirect(url_for('grading.view_program_subjects', program_id=program.id))
    return render_template(
        'grading/admin/add_subject.html',
        title="Add Subject", active_nav='programs', program=program
    )


@grading_bp.route("/grading-admin/delete_program/<program_id>", methods=['POST'])
@login_required
@grading_admin_required
def delete_program(program_id):
    program = Program.query.get_or_404(program_id)
    try:
        db.session.execute(text("""
            DELETE FROM subject_prerequisites
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR prerequisite_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})
        db.session.execute(text("""
            DELETE FROM prerequisites
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR prerequisite_subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})
        db.session.execute(text("""
            DELETE FROM student_grades
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})
        db.session.execute(text("""
            DELETE FROM grades
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})
        db.session.execute(text("""
            DELETE FROM faculty_assignments
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR section_id IN (SELECT id FROM sections WHERE program_id = :pid)
        """), {'pid': program_id})
        db.session.execute(text("UPDATE subjects SET prerequisite_subject_id = NULL WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("DELETE FROM subjects WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("UPDATE applicants SET section_id = NULL WHERE section_id IN (SELECT id FROM sections WHERE program_id = :pid)"), {'pid': program_id})
        db.session.execute(text("DELETE FROM sections WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("DELETE FROM program_courses WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("DELETE FROM program_careers WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("DELETE FROM program_objectives WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(text("DELETE FROM courses WHERE program_id = :pid"), {'pid': program_id})
        db.session.delete(program)
        db.session.commit()
        flash(f'Program "{program.program_code}" and all associated data deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete program: {e}', 'danger')
    return redirect(url_for('grading.manage_programs'))


@grading_bp.route("/grading-admin/delete_subject/<int:subject_id>", methods=['POST'])
@login_required
@grading_admin_required
def delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    program_id = subject.program_id
    db.session.delete(subject)
    db.session.commit()
    flash('Subject deleted.', 'success')
    return redirect(url_for('grading.view_program_subjects', program_id=program_id))


@grading_bp.route("/grading-admin/settings", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def settings():
    system_settings = GradingSettings.query.first()
    if not system_settings:
        system_settings = GradingSettings(active_school_year='2024-2025', active_semester='1st Semester')
        db.session.add(system_settings)
        db.session.commit()

    if request.method == 'POST':
        system_settings.active_school_year = request.form.get('active_school_year', system_settings.active_school_year)
        system_settings.active_semester = request.form.get('active_semester', system_settings.active_semester)
        db.session.commit()
        flash('System settings updated successfully.', 'success')
        return redirect(url_for('grading.settings'))

    return render_template(
        'grading/admin/settings.html',
        title="System Settings", active_nav='settings',
        system_settings=system_settings
    )


@grading_bp.route("/grading-admin/student_records/print")
@login_required
@grading_admin_required
def print_student_records():
    scope = get_admin_program_scope()
    search_query = request.args.get('search', '')
    program_filter = request.args.get('program', '')
    section_filter = request.args.get('section', '')

    students_query = Applicant.query.join(Section).join(Program).order_by(Program.title, Section.name, Applicant.last_name)
    if scope:
        students_query = students_query.filter(Program.program_code.in_(scope))

    program_name = None
    if search_query:
        search_term = f"%{search_query}%"
        students_query = students_query.filter(
            db.or_(
                Applicant.old_student_id.ilike(search_term),
                Applicant.first_name.ilike(search_term),
                Applicant.last_name.ilike(search_term)
            )
        )
    if program_filter:
        students_query = students_query.filter(Program.id == program_filter)
        program = Program.query.get(program_filter)
        if program:
            program_name = program.title
    if section_filter:
        sections = [s.strip().upper() for s in section_filter.split(',')]
        students_query = students_query.filter(func.upper(Section.name).in_(sections))

    students = students_query.all()
    return render_template(
        'grading/admin/print_student_records.html',
        students=students, program_name=program_name,
        current_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@grading_bp.route("/grading-admin/class/<int:assignment_id>/add_student", methods=['POST'])
@login_required
@grading_admin_required
def admin_add_irregular_student(assignment_id):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    student_number = request.form.get('student_number')
    student = Applicant.query.filter_by(old_student_id=student_number).first()
    if not student:
        flash('Student ID not found.', 'danger')
        return redirect(url_for('grading.grade_management', assignment_id=assignment.id))

    existing_grade = Grade.query.filter_by(
        student_id=student.applicant_id,
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
    ).first()
    if existing_grade:
        flash('Student is already in this class list.', 'warning')
        return redirect(url_for('grading.grade_management', assignment_id=assignment.id))

    is_allowed, message = check_prerequisites(student.applicant_id, assignment.subject_id)
    if not is_allowed:
        flash(f'Cannot add student: {message}', 'danger')
        return redirect(url_for('grading.grade_management', assignment_id=assignment.id))

    new_grade = Grade(
        student_id=student.applicant_id,
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
    )
    db.session.add(new_grade)
    db.session.commit()
    flash(f'Irregular Student {student.name} successfully added to class.', 'success')
    return redirect(url_for('grading.grade_management', assignment_id=assignment.id))


# ---------------------------------------------------------------------------
# Faculty Routes
# ---------------------------------------------------------------------------

@grading_bp.route("/grades/dashboard")
@login_required
def faculty_dashboard():
    assignments = FacultyAssignment.query.filter_by(faculty_id=current_user.id).all()
    total_classes = len(assignments)
    total_students = 0
    program_counts = {}
    total_passed = 0
    total_failed = 0
    class_progress = []

    for assign in assignments:
        students = Applicant.query.filter_by(section_id=assign.section_id).all()
        count = len(students)
        total_students += count
        program_code = assign.section.program.program_code if assign.section and assign.section.program else 'Unknown'
        program_counts[program_code] = program_counts.get(program_code, 0) + count

        graded_count = 0
        for student in students:
            grade = Grade.query.filter_by(
                student_id=student.applicant_id,
                subject_id=assign.subject_id,
                faculty_id=current_user.id
            ).first()
            if grade and grade.final_grade_point is not None:
                graded_count += 1
                if grade.final_grade_point > 3.0:
                    total_failed += 1
                else:
                    total_passed += 1

        percentage = (graded_count / count * 100) if count > 0 else 0
        class_progress.append({
            'subject': assign.subject.subject_code,
            'section': f"{assign.section.program.program_code} {assign.section.name}",
            'percentage': round(percentage),
            'total': count,
            'graded': graded_count
        })

    program_labels = list(program_counts.keys())
    program_data = list(program_counts.values())

    return render_template(
        'grading/faculty/dashboard.html',
        title='Faculty Dashboard', active_nav='dashboard',
        total_classes=total_classes, total_students=total_students,
        program_labels=json.dumps(program_labels),
        program_data=json.dumps(program_data),
        total_passed=total_passed, total_failed=total_failed,
        class_progress=class_progress
    )


@grading_bp.route("/grades/manage_classes")
@login_required
def manage_classes():
    current_assignments = FacultyAssignment.query.filter_by(faculty_id=current_user.id).all()
    return render_template(
        'grading/faculty/manage_classes.html',
        title="My Class Load", active_nav='manage_classes',
        current_assignments=current_assignments
    )


@grading_bp.route("/grades/remove_assignment/<int:assignment_id>", methods=['GET'])
@login_required
def remove_assignment(assignment_id):
    assignment_to_remove = FacultyAssignment.query.get_or_404(assignment_id)
    if assignment_to_remove.faculty_id != current_user.id:
        flash('You do not have permission to remove this class.', 'danger')
        return redirect(url_for('grading.manage_classes'))
    db.session.delete(assignment_to_remove)
    db.session.commit()
    flash('Class removed from your list.', 'success')
    return redirect(url_for('grading.manage_classes'))


@grading_bp.route("/grades/grade_management", methods=['GET', 'POST'])
@login_required
def grade_management():
    assignments = FacultyAssignment.query.filter_by(faculty_id=current_user.id).all()
    assignment_id = request.args.get('assignment_id') or request.form.get('assignment_id')

    selected_assignment = None
    students = []
    grades_dict = {}

    if assignment_id:
        selected_assignment = FacultyAssignment.query.get(int(assignment_id))
        if selected_assignment and selected_assignment.faculty_id == current_user.id:
            regular_students = Applicant.query.filter_by(section_id=selected_assignment.section_id).all()
            irregular_grades = Grade.query.filter_by(
                subject_id=selected_assignment.subject_id, faculty_id=current_user.id
            ).all()
            irregular_student_ids = [g.student_id for g in irregular_grades]
            irregular_students = Applicant.query.filter(Applicant.applicant_id.in_(irregular_student_ids)).all()
            all_students_set = set(regular_students + irregular_students)
            students = sorted(list(all_students_set), key=lambda x: x.last_name)

            all_grades = Grade.query.filter_by(
                subject_id=selected_assignment.subject_id, faculty_id=current_user.id
            ).all()
            grades_dict = {g.student_id: g for g in all_grades}

    if request.method == 'POST' and selected_assignment:
        def get_max(name):
            val = request.form.get(name)
            return int(val) if val and val.strip() else 0

        selected_assignment.p_max_q1 = get_max('p_max_q1')
        selected_assignment.p_max_q2 = get_max('p_max_q2')
        selected_assignment.p_max_q3 = get_max('p_max_q3')
        selected_assignment.p_max_q4 = get_max('p_max_q4')
        selected_assignment.p_max_q5 = get_max('p_max_q5')
        selected_assignment.p_max_act1 = get_max('p_max_act1')
        selected_assignment.p_max_act2 = get_max('p_max_act2')
        selected_assignment.p_max_act3 = get_max('p_max_act3')
        selected_assignment.p_max_act4 = get_max('p_max_act4')
        selected_assignment.p_max_act5 = get_max('p_max_act5')
        selected_assignment.p_max_r = get_max('p_max_r')
        selected_assignment.p_max_exam = get_max('p_max_e')

        selected_assignment.m_max_q1 = get_max('m_max_q1')
        selected_assignment.m_max_q2 = get_max('m_max_q2')
        selected_assignment.m_max_q3 = get_max('m_max_q3')
        selected_assignment.m_max_q4 = get_max('m_max_q4')
        selected_assignment.m_max_q5 = get_max('m_max_q5')
        selected_assignment.m_max_act1 = get_max('m_max_act1')
        selected_assignment.m_max_act2 = get_max('m_max_act2')
        selected_assignment.m_max_act3 = get_max('m_max_act3')
        selected_assignment.m_max_act4 = get_max('m_max_act4')
        selected_assignment.m_max_act5 = get_max('m_max_act5')
        selected_assignment.m_max_r = get_max('m_max_r')
        selected_assignment.m_max_exam = get_max('m_max_e')

        selected_assignment.f_max_q1 = get_max('f_max_q1')
        selected_assignment.f_max_q2 = get_max('f_max_q2')
        selected_assignment.f_max_q3 = get_max('f_max_q3')
        selected_assignment.f_max_q4 = get_max('f_max_q4')
        selected_assignment.f_max_q5 = get_max('f_max_q5')
        selected_assignment.f_max_act1 = get_max('f_max_act1')
        selected_assignment.f_max_act2 = get_max('f_max_act2')
        selected_assignment.f_max_act3 = get_max('f_max_act3')
        selected_assignment.f_max_act4 = get_max('f_max_act4')
        selected_assignment.f_max_act5 = get_max('f_max_act5')
        selected_assignment.f_max_r = get_max('f_max_r')
        selected_assignment.f_max_exam = get_max('f_max_e')

        student_ids = request.form.getlist('student_id')
        student_names = {s.applicant_id: s.name for s in students}
        errors = []

        def field_label(field):
            term_map = {'p': 'Prelim', 'm': 'Midterm', 'f': 'Finals'}
            term, rest = field.split('_', 1)
            t = term_map.get(term, term.upper())
            if rest == 'recit':
                return f'{t} Recitation'
            if rest == 'exam':
                return f'{t} Exam'
            if rest.startswith('q') and rest[1:].isdigit():
                return f'{t} Quiz {rest[1:]}'
            if rest.startswith('act') and rest[3:].isdigit():
                return f'{t} Activity {rest[3:]}'
            return f'{t} {rest}'

        for sid in student_ids:
            sid_int = int(sid)
            student_name = student_names.get(sid_int, f'Student #{sid}')
            grade = grades_dict.get(sid_int)
            if not grade:
                grade = Grade(student_id=sid_int, subject_id=selected_assignment.subject_id, faculty_id=current_user.id)
                db.session.add(grade)

            def get_score(field, max_val):
                val = request.form.get(f'{field}_{sid}')
                if not val or not val.strip():
                    return 0.0
                try:
                    score = float(val)
                except ValueError:
                    errors.append(f'{student_name} — {field_label(field)}: "{val}" is not a valid number.')
                    return 0.0
                if score < 0:
                    errors.append(f'{student_name} — {field_label(field)}: {score} is negative.')
                elif max_val > 0 and score > max_val:
                    errors.append(f'{student_name} — {field_label(field)}: {score} exceeds max {max_val}.')
                return score

            grade.p_q1 = get_score('p_q1', selected_assignment.p_max_q1)
            grade.p_q2 = get_score('p_q2', selected_assignment.p_max_q2)
            grade.p_q3 = get_score('p_q3', selected_assignment.p_max_q3)
            grade.p_q4 = get_score('p_q4', selected_assignment.p_max_q4)
            grade.p_q5 = get_score('p_q5', selected_assignment.p_max_q5)
            grade.p_act1 = get_score('p_act1', selected_assignment.p_max_act1)
            grade.p_act2 = get_score('p_act2', selected_assignment.p_max_act2)
            grade.p_act3 = get_score('p_act3', selected_assignment.p_max_act3)
            grade.p_act4 = get_score('p_act4', selected_assignment.p_max_act4)
            grade.p_act5 = get_score('p_act5', selected_assignment.p_max_act5)
            grade.p_recit = get_score('p_recit', selected_assignment.p_max_r)
            grade.p_exam = get_score('p_exam', selected_assignment.p_max_exam)

            grade.m_q1 = get_score('m_q1', selected_assignment.m_max_q1)
            grade.m_q2 = get_score('m_q2', selected_assignment.m_max_q2)
            grade.m_q3 = get_score('m_q3', selected_assignment.m_max_q3)
            grade.m_q4 = get_score('m_q4', selected_assignment.m_max_q4)
            grade.m_q5 = get_score('m_q5', selected_assignment.m_max_q5)
            grade.m_act1 = get_score('m_act1', selected_assignment.m_max_act1)
            grade.m_act2 = get_score('m_act2', selected_assignment.m_max_act2)
            grade.m_act3 = get_score('m_act3', selected_assignment.m_max_act3)
            grade.m_act4 = get_score('m_act4', selected_assignment.m_max_act4)
            grade.m_act5 = get_score('m_act5', selected_assignment.m_max_act5)
            grade.m_recit = get_score('m_recit', selected_assignment.m_max_r)
            grade.m_exam = get_score('m_exam', selected_assignment.m_max_exam)

            grade.f_q1 = get_score('f_q1', selected_assignment.f_max_q1)
            grade.f_q2 = get_score('f_q2', selected_assignment.f_max_q2)
            grade.f_q3 = get_score('f_q3', selected_assignment.f_max_q3)
            grade.f_q4 = get_score('f_q4', selected_assignment.f_max_q4)
            grade.f_q5 = get_score('f_q5', selected_assignment.f_max_q5)
            grade.f_act1 = get_score('f_act1', selected_assignment.f_max_act1)
            grade.f_act2 = get_score('f_act2', selected_assignment.f_max_act2)
            grade.f_act3 = get_score('f_act3', selected_assignment.f_max_act3)
            grade.f_act4 = get_score('f_act4', selected_assignment.f_max_act4)
            grade.f_act5 = get_score('f_act5', selected_assignment.f_max_act5)
            grade.f_recit = get_score('f_recit', selected_assignment.f_max_r)
            grade.f_exam = get_score('f_exam', selected_assignment.f_max_exam)

            def calc_equiv(raw, max_score):
                if max_score <= 0:
                    return 0
                return (raw / max_score * 100)

            grade.prelim_grade = round(
                (calc_equiv(grade.p_quiz_total, selected_assignment.p_total_max_q) * 0.20) +
                (calc_equiv(grade.p_recit, selected_assignment.p_max_r) * 0.20) +
                (calc_equiv(grade.p_act_total, selected_assignment.p_total_max_a) * 0.10) +
                (calc_equiv(grade.p_exam, selected_assignment.p_max_exam) * 0.50)
            )

            grade.midterm_grade = round(
                (calc_equiv(grade.m_quiz_total, selected_assignment.m_total_max_q) * 0.20) +
                (calc_equiv(grade.m_recit, selected_assignment.m_max_r) * 0.10) +
                (calc_equiv(grade.m_act_total, selected_assignment.m_total_max_a) * 0.20) +
                (calc_equiv(grade.m_exam, selected_assignment.m_max_exam) * 0.50)
            )

            grade.final_term_grade = round(
                (calc_equiv(grade.f_quiz_total, selected_assignment.f_total_max_q) * 0.20) +
                (calc_equiv(grade.f_recit, selected_assignment.f_max_r) * 0.20) +
                (calc_equiv(grade.f_act_total, selected_assignment.f_total_max_a) * 0.10) +
                (calc_equiv(grade.f_exam, selected_assignment.f_max_exam) * 0.50)
            )

            grade.midterm_standing = round((grade.prelim_grade * 0.50) + (grade.midterm_grade * 0.50))
            grade.final_grade = round((grade.midterm_standing * 0.60) + (grade.final_term_grade * 0.40))

            score = grade.final_grade
            if score >= 96:
                pt = 1.00
            elif score >= 92:
                pt = 1.25
            elif score >= 88:
                pt = 1.50
            elif score >= 84:
                pt = 1.75
            elif score >= 80:
                pt = 2.00
            elif score >= 75:
                pt = 2.25
            elif score >= 70:
                pt = 2.50
            elif score >= 65:
                pt = 2.75
            elif score >= 60:
                pt = 3.00
            else:
                pt = 5.00
            grade.final_grade_point = pt

        if errors:
            db.session.rollback()
            for msg in errors:
                flash(msg, 'danger')
            flash('No grades were saved. Please correct the invalid entries above.', 'warning')
            return redirect(url_for('grading.grade_management', assignment_id=selected_assignment.id))

        db.session.commit()
        flash('Grades updated successfully.', 'success')
        return redirect(url_for('grading.grade_management', assignment_id=selected_assignment.id))

    return render_template(
        'grading/faculty/grade_management.html',
        title='Grade Management', active_nav='grades',
        assignments=assignments, selected_assignment=selected_assignment,
        students=students, grades_dict=grades_dict
    )


@grading_bp.route("/grades/print_report/<int:assignment_id>/<report_type>")
@login_required
def print_faculty_report(assignment_id, report_type):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    if assignment.faculty_id != current_user.id:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('grading.faculty_dashboard'))

    students = Applicant.query.filter_by(section_id=assignment.section_id).order_by(Applicant.last_name).all()
    grades = Grade.query.filter_by(subject_id=assignment.subject_id, faculty_id=current_user.id).all()
    grades_dict = {g.student_id: g for g in grades}

    student_data = []
    for s in students:
        g = grades_dict.get(s.applicant_id)
        percentage = 0
        grade_point = 5.00

        if g:
            if report_type == 'prelim':
                percentage = g.prelim_grade or 0
            elif report_type == 'midterm':
                percentage = g.midterm_grade or 0
            elif report_type == 'finals':
                percentage = g.final_term_grade or 0

            if percentage >= 96:
                grade_point = 1.00
            elif percentage >= 92:
                grade_point = 1.25
            elif percentage >= 88:
                grade_point = 1.50
            elif percentage >= 84:
                grade_point = 1.75
            elif percentage >= 80:
                grade_point = 2.00
            elif percentage >= 75:
                grade_point = 2.25
            elif percentage >= 70:
                grade_point = 2.50
            elif percentage >= 65:
                grade_point = 2.75
            elif percentage >= 60:
                grade_point = 3.00
            else:
                grade_point = 5.00

            if percentage == 0:
                grade_point = 0

        student_data.append({
            'name': s.name,
            'percentage': percentage,
            'grade_point': grade_point
        })

    titles = {
        'prelim': 'PRELIM GRADE',
        'midterm': 'MIDTERM GRADE',
        'finals': 'FINAL TERM GRADE'
    }

    return render_template(
        'grading/faculty/print_rog.html',
        assignment=assignment,
        students=student_data,
        report_title=titles.get(report_type, 'REPORT ON GRADES'),
        current_date=datetime.now().strftime("%Y-%m-%d")
    )


# ---------------------------------------------------------------------------
# AJAX Endpoints
# ---------------------------------------------------------------------------

@grading_bp.route('/get_sections_for_filters')
@login_required
def get_sections_for_filters():
    program_id = request.args.get('program_id', type=str)
    year = request.args.get('year', type=int)
    if not program_id or not year:
        return jsonify([])
    year_label = {1: '1st Year', 2: '2nd Year', 3: '3rd Year', 4: '4th Year'}.get(year)
    if not year_label:
        return jsonify([])
    sections = Section.query.filter_by(
        program_id=program_id, year_level=year_label
    ).order_by(Section.name).all()
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])


@grading_bp.route('/get_subjects_for_filters')
@login_required
def get_subjects_for_filters():
    program_id = request.args.get('program_id', type=str)
    year = request.args.get('year', type=int)
    semester = request.args.get('semester', type=int)
    if not all([program_id, year, semester]):
        return jsonify([])
    subjects = Subject.query.filter_by(
        program_id=program_id, year_level=year, semester=semester
    ).order_by(Subject.subject_title).all()
    return jsonify([{'id': s.id, 'code': s.subject_code, 'description': s.subject_title} for s in subjects])
