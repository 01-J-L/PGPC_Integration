# app/routes.py
import openpyxl
from werkzeug.utils import secure_filename
import os
from flask import current_app, jsonify, send_from_directory
from flask import Blueprint, render_template, url_for, flash, redirect, request, session, abort
from app import db, bcrypt
from app.forms import RegistrationForm, StudentRegistrationForm, StudentLoginForm, FacultyLoginForm, OTPVerificationForm, AdminLoginForm, ProgramForm, SubjectForm, SettingsForm, AdminAssignmentForm
from app.models import User, Student, Subject, Grade, Program, Section, FacultyAssignment, SystemSettings
from flask_login import login_user, current_user, logout_user, login_required
from app.utils import generate_otp, send_otp_email
from functools import wraps
import re 
import pandas as pd
from sqlalchemy import func
from flask import jsonify
from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField
from wtforms.validators import DataRequired
import json
from collections import defaultdict
from datetime import datetime

def check_prerequisites(student_id, subject_id):
    target_subject = Subject.query.get(subject_id)
    student = Student.query.get(student_id)
    
    # If no prereq, allowed
    if not target_subject.prerequisites.count():
        return True, "Allowed"

    for prereq in target_subject.prerequisites:
        # Find if student has a grade for this prerequisite
        # We look for ANY record of this subject for this student
        # We need to find the specific subject_id of the prerequisite
        
        # Check if student passed the prerequisite
        # We check the 'final_grade_point' column in the Grade table
        passed = False
        grade_record = Grade.query.filter_by(student_id=student.id, subject_id=prereq.id).first()
        
        if grade_record and grade_record.final_grade_point is not None:
             if grade_record.final_grade_point <= 3.0: # Assuming 3.0 is passing
                 passed = True
        
        if not passed:
            return False, f"Missing Prerequisite: {prereq.code} - {prereq.description}"
            
    return True, "Allowed"

class FacultyAssignmentForm(FlaskForm):
    program = SelectField('Program', validators=[DataRequired()])
    year = SelectField('Year', validators=[DataRequired()], choices=[('', 'Select Year'), ('1', '1st'), ('2', '2nd'), ('3', '3rd'), ('4', '4th')])
    semester = SelectField('Semester', validators=[DataRequired()], choices=[('', 'Select Semester'), ('1', '1st'), ('2', '2nd')])
    section = SelectField('Section', validators=[DataRequired()])
    subject = SelectField('Subject', validators=[DataRequired()])
    submit = SubmitField('Add Class to My List')

def get_admin_program_scope():
    if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
        return []
    if current_user.department == 'CCBA':
        return ['BPA', 'BSCS', 'BSMA']
    elif current_user.department == 'COC':
        return ['BSCRIM']
    return None 

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('main.admin_login'))
        return f(*args, **kwargs)
    return decorated_function

main = Blueprint('main', __name__)


@main.route("/")
def home(): 
    return redirect(url_for('main.faculty_login'))



@main.route("/faculty", methods=['GET', 'POST'])
def faculty_login():
    if current_user.is_authenticated: return redirect(url_for('main.faculty_dashboard'))
    form = FacultyLoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data) and user.role == 'faculty':
            if not user.is_approved:
                flash('Account pending approval.', 'danger')
            else:
                login_user(user)
                return redirect(url_for('main.faculty_dashboard'))
        else: flash('Login Unsuccessful.', 'danger')
    return render_template('auth/faculty_login.html', title='Faculty Login', form=form)


@main.route("/logout")
def logout():
    logout_user()
    session.pop('student_id', None)
    return redirect(url_for('main.faculty_login'))



@main.route("/verify_otp", methods=['GET', 'POST'])
def verify_otp():
    if 'registration_data' not in session: 
        return redirect(url_for('main.faculty_login'))
        
    form = OTPVerificationForm()
    
    if form.validate_on_submit():
        if form.otp.data == session.get('otp'):
            reg_data = session.pop('registration_data')
            session.pop('otp', None)
            hashed_password = bcrypt.generate_password_hash(reg_data['password']).decode('utf-8')
            
            # Faculty Registration Only
            if reg_data['type'] == 'faculty':
                new_faculty = User(
                    name=reg_data['name'], 
                    email=reg_data['email'], 
                    password_hash=hashed_password, 
                    role='faculty', 
                    is_approved=False, 
                    department=reg_data.get('department')
                )
                db.session.add(new_faculty)
                db.session.commit()
                flash('Registration successful! Please wait for admin approval.', 'success')
                return redirect(url_for('main.faculty_login'))
                
    return render_template('auth/verify_otp.html', title='Verify OTP', form=form)

@main.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated and current_user.role in ('admin', 'superadmin'): return redirect(url_for('main.admin_dashboard'))
    form = AdminLoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()

        # FALLBACK / INITIAL SETUP (For first-time deployment)
        # The bootstrap password comes from the ADMIN_INITIAL_PASSWORD environment variable (.env),
        # mirroring the admission system. It only creates the account when no admin exists yet.
        initial_admin_password = os.getenv('ADMIN_INITIAL_PASSWORD')
        if not user and form.username.data == 'admin' and initial_admin_password and form.password.data == initial_admin_password:
            existing_admin = User.query.filter_by(role='admin').first()
            if not existing_admin:
                user = User(
                    username='admin',
                    full_name='System Administrator',
                    email='admin@school.com',
                    role='admin'
                )
                user.set_password(initial_admin_password)
                db.session.add(user)
                db.session.commit()
                flash('Initial Admin account created successfully. You are logged in.', 'success')
                login_user(user)
                return redirect(url_for('main.admin_dashboard'))

        if user and user.check_password(form.password.data) and user.role in ('admin', 'superadmin'):
            login_user(user)
            if user.role == 'superadmin':
                return redirect(url_for('main.admin_maintenance'))
            return redirect(url_for('main.admin_dashboard'))
        else: flash('Invalid credentials.', 'danger')
    return render_template('admin/login.html', title='Admin Login', form=form)

@main.route("/admin/maintenance", methods=['GET', 'POST'])
@login_required
@admin_required
def admin_maintenance():
    if current_user.role != 'superadmin':
        flash('Only the superadministrator can manage maintenance mode.', 'danger')
        return redirect(url_for('main.admin_dashboard'))

    from sqlalchemy import text
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
        if new_value == '1':
            flash("Maintenance mode ENABLED. All systems are now offline for regular users.", "success")
        else:
            flash("Maintenance mode DISABLED. All systems are back online.", "success")
        return redirect(url_for('main.admin_maintenance'))

    row = db.session.execute(
        text("SELECT setting_value FROM system_settings WHERE setting_key = 'maintenance_mode'")
    ).fetchone()
    maintenance_on = bool(row and row[0] == '1')
    return render_template('admin/admin_maintenance.html', title='System Maintenance', active_nav='maintenance', maintenance_on=maintenance_on)

@main.route("/admin/dashboard")
@login_required
@admin_required
def admin_dashboard():
    scope = get_admin_program_scope() # Get ['BSCS', 'BPA', etc] or None

    # 1. Stats Queries with Scope
    pending_approvals = User.query.filter_by(role='faculty', is_approved=False).count()
    faculty_count = User.query.filter_by(role='faculty', is_approved=True).count()
    
    # Filter students based on scope
    student_query = Student.query.join(Section).join(Program)
    if scope:
        student_query = student_query.filter(Program.code.in_(scope))
    student_count = student_query.count()
    
    # 2. Charts Data with Scope
    dist_query = db.session.query(Program.code, func.count(Student.id)).select_from(Program).join(Program.sections).join(Section.students)
    if scope:
        dist_query = dist_query.filter(Program.code.in_(scope))
    student_dist_results = dist_query.group_by(Program.code).all()

    student_dist_labels = [row[0] for row in student_dist_results]
    student_dist_data = [row[1] for row in student_dist_results]
    
    # 3. Honors Calculation (Filtered)
    # Note: This calculates honors for ALL students in scope. 
    # Optimization: In production, this should be cached or pre-calculated.
    all_students = student_query.all() # Uses the filtered query
    deans_listers_total = 0
    presidents_listers_total = 0
    
    deans_listers_by_program = defaultdict(int)
    presidents_listers_by_program = defaultdict(int)

    for student in all_students:
        gwa = student.calculate_gwa(year=1, semester=1)
        if gwa > 0:
            grades_for_semester = [g for g in student.grades if g.subject.year == 1 and g.subject.semester == 1]
            has_low_grade = any(g.final_grade > 2.25 for g in grades_for_semester if g.final_grade is not None)
            
            if not has_low_grade:
                program_code = student.section.program.code
                if gwa <= 1.25:
                    presidents_listers_total += 1
                    presidents_listers_by_program[program_code] += 1
                elif gwa <= 1.75:
                    deans_listers_total += 1
                    deans_listers_by_program[program_code] += 1
    
    # Prepare chart data for honors
    # Filter the list of all programs to only those in scope
    all_programs_query = Program.query.order_by(Program.code)
    if scope:
        all_programs_query = all_programs_query.filter(Program.code.in_(scope))
    
    all_program_codes = [p.code for p in all_programs_query.all()]
    pl_data = [presidents_listers_by_program.get(code, 0) for code in all_program_codes]
    dl_data = [deans_listers_by_program.get(code, 0) for code in all_program_codes]

    return render_template(
        'admin/dashboard.html', 
        title='Admin Dashboard', 
        active_nav='dashboard',
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

@main.route("/admin/approvals")
@login_required
@admin_required
def admin_approvals():
    faculty_to_approve = User.query.filter_by(role='faculty', is_approved=False).all()
    return render_template('admin/approvals.html', title='Faculty Approvals', faculty_list=faculty_to_approve, active_nav='approvals')

@main.route("/admin/approve_faculty/<int:user_id>", methods=['POST'])
@login_required
@admin_required
def approve_faculty(user_id):
    user = User.query.get_or_404(user_id)
    user.is_approved = True
    db.session.commit()
    flash(f'{user.name} has been approved as a faculty member.', 'success')
    return redirect(url_for('main.admin_approvals'))

@main.route("/faculty/dashboard")
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
        students = Student.query.filter_by(section_id=assign.section_id).all()
        count = len(students)
        total_students += count
        program_code = assign.section.program.code
        program_counts[program_code] = program_counts.get(program_code, 0) + count
        
        graded_count = 0
        for student in students:
            grade = Grade.query.filter_by(student_id=student.id, subject_id=assign.subject_id, faculty_id=current_user.id).first()
            
            # --- FIX: Use new model attributes ---
            # Check if 'final_grade_point' exists (indicates grading is complete)
            if grade and grade.final_grade_point is not None:
                graded_count += 1
                
                # Check pass/fail using the Grade Point (3.0 is usually the cutoff)
                if grade.final_grade_point > 3.0:
                    total_failed += 1
                else:
                    total_passed += 1
        
        percentage = (graded_count / count * 100) if count > 0 else 0
        class_progress.append({
            'subject': assign.subject.code,
            'section': f"{assign.section.program.code} {assign.section.name}",
            'percentage': round(percentage),
            'total': count,
            'graded': graded_count
        })

    program_labels = list(program_counts.keys())
    program_data = list(program_counts.values())

    return render_template(
        'faculty/dashboard.html', 
        title='Faculty Dashboard', 
        active_nav='dashboard',
        total_classes=total_classes,
        total_students=total_students,
        program_labels=json.dumps(program_labels),
        program_data=json.dumps(program_data),
        total_passed=total_passed,
        total_failed=total_failed,
        class_progress=class_progress
    )

@main.route("/faculty/manage_classes")
@login_required
def manage_classes():
    # Faculty can only VIEW their list now
    current_assignments = FacultyAssignment.query.filter_by(faculty_id=current_user.id).all()
    return render_template(
        'faculty/manage_classes.html',
        title="My Class Load",
        active_nav='manage_classes',
        current_assignments=current_assignments
    )


@main.route("/admin/faculty/<int:user_id>/assign", methods=['GET', 'POST'])
@login_required
@admin_required
def admin_assign_loads(user_id):
    faculty = User.query.get_or_404(user_id)
    
    # Ensure Deans only assign to their dept (Optional security)
    # if current_user.department and faculty.department != current_user.department: ...
    
    form = AdminAssignmentForm()
    # Populate Programs
    form.program.choices = [('', 'Select Program')] + [(str(p.id), p.code) for p in Program.query.order_by(Program.code).all()]

    # Dynamic Choices Logic (for POST requests to validate)
    if request.method == 'POST':
        program_id = form.program.data
        year = form.year.data
        if program_id and year:
            year_label = {1: '1st Year', 2: '2nd Year', 3: '3rd Year', 4: '4th Year'}.get(int(year))
            sections = []
            if year_label:
                sections = db.session.query(Section).filter(
                    Section.program_id == program_id,
                    Section.year_level == year_label
                ).order_by(Section.name).all()
            form.section.choices = [(str(s.id), s.name) for s in sections]
        
        if program_id and year and form.semester.data:
            subjects = Subject.query.filter_by(
                program_id=program_id, year=int(year), semester=int(form.semester.data)
            ).order_by(Subject.description).all()
            form.subject.choices = [(str(s.id), s.description) for s in subjects]

    if form.validate_on_submit():
        subject_id = int(form.subject.data)
        section_id = int(form.section.data)
        
        # Check if already assigned
        exists = FacultyAssignment.query.filter_by(
            faculty_id=faculty.id, subject_id=subject_id, section_id=section_id
        ).first()
        
        if exists:
            flash('This subject is already assigned to this faculty.', 'warning')
        else:
            new_assign = FacultyAssignment(
                faculty_id=faculty.id,
                subject_id=subject_id,
                section_id=section_id
            )
            db.session.add(new_assign)
            db.session.commit()
            flash('Subject assigned successfully.', 'success')
        return redirect(url_for('main.admin_assign_loads', user_id=faculty.id))

    current_assignments = FacultyAssignment.query.filter_by(faculty_id=faculty.id).all()
    
    return render_template(
        'admin/assign_loads.html',
        title=f"Assign Loads - {faculty.name}",
        faculty=faculty,
        form=form,
        current_assignments=current_assignments,
        active_nav='faculty_records'
    )

@main.route("/admin/remove_load/<int:assignment_id>", methods=['POST'])
@login_required
@admin_required
def remove_faculty_load(assignment_id):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    faculty_id = assignment.faculty_id
    db.session.delete(assignment)
    db.session.commit()
    flash('Assignment removed.', 'success')
    return redirect(url_for('main.admin_assign_loads', user_id=faculty_id))

@main.route("/faculty/remove_assignment/<int:assignment_id>", methods=['GET'])
@login_required
def remove_assignment(assignment_id):
    assignment_to_remove = FacultyAssignment.query.get_or_404(assignment_id)
    if assignment_to_remove.faculty_id != current_user.id:
        flash('You do not have permission to remove this class.', 'danger')
        return redirect(url_for('main.manage_classes'))
    
    db.session.delete(assignment_to_remove)
    db.session.commit()
    flash('Class removed from your list.', 'success')
    return redirect(url_for('main.manage_classes'))

@main.route("/faculty/grade_management", methods=['GET', 'POST'])
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
            # Load students (Regular + Irregular logic remains the same)
            regular_students = Student.query.filter_by(section_id=selected_assignment.section_id).all()
            irregular_grades = Grade.query.filter_by(subject_id=selected_assignment.subject_id, faculty_id=current_user.id).all()
            irregular_student_ids = [g.student_id for g in irregular_grades]
            irregular_students = Student.query.filter(Student.id.in_(irregular_student_ids)).all()
            all_students_set = set(regular_students + irregular_students)
            students = sorted(list(all_students_set), key=lambda x: x.last_name)
            
            all_grades = Grade.query.filter_by(subject_id=selected_assignment.subject_id, faculty_id=current_user.id).all()
            grades_dict = {g.student_id: g for g in all_grades}

    if request.method == 'POST' and selected_assignment:
        # 1. CAPTURE MAX SCORES
        def get_max(name): 
            val = request.form.get(name)
            return int(val) if val and val.strip() else 0
        
        # PRELIM MAX
        selected_assignment.p_max_q1 = get_max('p_max_q1'); selected_assignment.p_max_q2 = get_max('p_max_q2'); selected_assignment.p_max_q3 = get_max('p_max_q3')
        selected_assignment.p_max_q4 = get_max('p_max_q4'); selected_assignment.p_max_q5 = get_max('p_max_q5')
        
        selected_assignment.p_max_act1 = get_max('p_max_act1'); selected_assignment.p_max_act2 = get_max('p_max_act2'); selected_assignment.p_max_act3 = get_max('p_max_act3')
        selected_assignment.p_max_act4 = get_max('p_max_act4'); selected_assignment.p_max_act5 = get_max('p_max_act5')
        
        selected_assignment.p_max_r = get_max('p_max_r'); selected_assignment.p_max_exam = get_max('p_max_e')

        # MIDTERM MAX
        selected_assignment.m_max_q1 = get_max('m_max_q1'); selected_assignment.m_max_q2 = get_max('m_max_q2'); selected_assignment.m_max_q3 = get_max('m_max_q3')
        selected_assignment.m_max_q4 = get_max('m_max_q4'); selected_assignment.m_max_q5 = get_max('m_max_q5')

        selected_assignment.m_max_act1 = get_max('m_max_act1'); selected_assignment.m_max_act2 = get_max('m_max_act2'); selected_assignment.m_max_act3 = get_max('m_max_act3')
        selected_assignment.m_max_act4 = get_max('m_max_act4'); selected_assignment.m_max_act5 = get_max('m_max_act5')
        
        selected_assignment.m_max_r = get_max('m_max_r'); selected_assignment.m_max_exam = get_max('m_max_e')

        # FINALS MAX
        selected_assignment.f_max_q1 = get_max('f_max_q1'); selected_assignment.f_max_q2 = get_max('f_max_q2'); selected_assignment.f_max_q3 = get_max('f_max_q3')
        selected_assignment.f_max_q4 = get_max('f_max_q4'); selected_assignment.f_max_q5 = get_max('f_max_q5')

        selected_assignment.f_max_act1 = get_max('f_max_act1'); selected_assignment.f_max_act2 = get_max('f_max_act2'); selected_assignment.f_max_act3 = get_max('f_max_act3')
        selected_assignment.f_max_act4 = get_max('f_max_act4'); selected_assignment.f_max_act5 = get_max('f_max_act5')
        
        selected_assignment.f_max_r = get_max('f_max_r'); selected_assignment.f_max_exam = get_max('f_max_e')

        # 2. SAVE STUDENT SCORES (validated against max scores)
        student_ids = request.form.getlist('student_id')
        student_names = {s.id: s.name for s in students}
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
            student_name = student_names.get(int(sid), f'Student #{sid}')
            grade = grades_dict.get(int(sid))
            if not grade:
                grade = Grade(student_id=int(sid), subject_id=selected_assignment.subject_id, faculty_id=current_user.id)
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

            # Prelim
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

            # Midterm
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

            # Finals
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

            # --- CALCULATION LOGIC (Updated for 5 columns) ---
            def calc_equiv(raw, max_score):
                if max_score <= 0: return 0 
                return (raw / max_score * 100)

            # PRELIM
            grade.prelim_grade = round(
                (calc_equiv(grade.p_quiz_total, selected_assignment.p_total_max_q) * 0.20) +
                (calc_equiv(grade.p_recit, selected_assignment.p_max_r) * 0.20) +
                (calc_equiv(grade.p_act_total, selected_assignment.p_total_max_a) * 0.10) +
                (calc_equiv(grade.p_exam, selected_assignment.p_max_exam) * 0.50)
            )

            # MIDTERM
            grade.midterm_grade = round(
                (calc_equiv(grade.m_quiz_total, selected_assignment.m_total_max_q) * 0.20) +
                (calc_equiv(grade.m_recit, selected_assignment.m_max_r) * 0.10) +
                (calc_equiv(grade.m_act_total, selected_assignment.m_total_max_a) * 0.20) +
                (calc_equiv(grade.m_exam, selected_assignment.m_max_exam) * 0.50)
            )

            # FINAL
            grade.final_term_grade = round(
                (calc_equiv(grade.f_quiz_total, selected_assignment.f_total_max_q) * 0.20) +
                (calc_equiv(grade.f_recit, selected_assignment.f_max_r) * 0.20) +
                (calc_equiv(grade.f_act_total, selected_assignment.f_total_max_a) * 0.10) +
                (calc_equiv(grade.f_exam, selected_assignment.f_max_exam) * 0.50)
            )

            # SUMMARIES
            grade.midterm_standing = round((grade.prelim_grade * 0.50) + (grade.midterm_grade * 0.50))
            grade.final_grade = round((grade.midterm_standing * 0.60) + (grade.final_term_grade * 0.40))

            # TRANSMUTATION
            score = grade.final_grade
            if score >= 96: pt = 1.00
            elif score >= 92: pt = 1.25
            elif score >= 88: pt = 1.50
            elif score >= 84: pt = 1.75
            elif score >= 80: pt = 2.00
            elif score >= 75: pt = 2.25
            elif score >= 70: pt = 2.50
            elif score >= 65: pt = 2.75
            elif score >= 60: pt = 3.00
            else: pt = 5.00
            grade.final_grade_point = pt

        if errors:
            db.session.rollback()
            for msg in errors:
                flash(msg, 'danger')
            flash('No grades were saved. Please correct the invalid entries above.', 'warning')
            return redirect(url_for('main.grade_management', assignment_id=selected_assignment.id))

        db.session.commit()
        flash('Grades updated successfully.', 'success')
        return redirect(url_for('main.grade_management', assignment_id=selected_assignment.id))

    return render_template('faculty/grade_management.html', title='Grade Management', active_nav='grades', assignments=assignments, selected_assignment=selected_assignment, students=students, grades_dict=grades_dict)

@main.route('/get_sections_for_filters')
@login_required
def get_sections_for_filters():
    program_id = request.args.get('program_id', type=str)
    year = request.args.get('year', type=int)
    if not program_id or not year: return jsonify([])
    year_label = {1: '1st Year', 2: '2nd Year', 3: '3rd Year', 4: '4th Year'}.get(year)
    if not year_label: return jsonify([])
    sections = db.session.query(Section).filter(
        Section.program_id == program_id,
        Section.year_level == year_label
    ).order_by(Section.name).all()
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])

@main.route('/get_subjects_for_filters')
@login_required
def get_subjects_for_filters():
    program_id = request.args.get('program_id', type=str)
    year = request.args.get('year', type=int)
    semester = request.args.get('semester', type=int)
    
    if not all([program_id, year, semester]): 
        return jsonify([])
        
    subjects = Subject.query.filter_by(
        program_id=program_id, year=year, semester=semester
    ).order_by(Subject.description).all()
    
    # --- THIS IS THE FIX: Added 'code': s.code ---
    return jsonify([{'id': s.id, 'code': s.code, 'description': s.description} for s in subjects])


@main.route("/admin/student_records")
@login_required
@admin_required
def student_records():
    scope = get_admin_program_scope() # SCOPE FILTER

    search_query = request.args.get('search', '')
    program_filter = request.args.get('program', '')
    section_filter = request.args.get('section', '')
    
    students_query = Student.query.join(Section).join(Program).order_by(Student.last_name)
    
    # Apply Admin Scope
    if scope:
        students_query = students_query.filter(Program.code.in_(scope))

    if search_query:
        search_term = f"%{search_query}%"
        students_query = students_query.filter(
            db.or_(
                Student.student_number.ilike(search_term),
                Student.first_name.ilike(search_term),
                Student.last_name.ilike(search_term)
            )
        )
    if program_filter:
        students_query = students_query.filter(Program.id == program_filter)
    if section_filter:
        sections = [s.strip().upper() for s in section_filter.split(',')]
        students_query = students_query.filter(func.upper(Section.name).in_(sections))
    
    # Filter Programs Dropdown based on scope
    programs_query = Program.query.order_by(Program.code)
    if scope:
        programs_query = programs_query.filter(Program.code.in_(scope))
    programs = programs_query.all()
    
    students = students_query.all()
    return render_template('admin/student_records.html',
                           title="Student Records",
                           active_nav='student_records',
                           students=students,
                           programs=programs,
                           search_query=search_query,
                           program_filter=program_filter,
                           section_filter=section_filter)

@main.route("/admin/student/<int:student_id>/grades")
@login_required
@admin_required
def view_student_grades(student_id):
    student = Student.query.get_or_404(student_id)
    scope = get_admin_program_scope()
    if scope and student.section.program.code not in scope:
        return redirect(url_for('main.student_records'))

    selected_year = request.args.get('year', 1, type=int)
    selected_semester = request.args.get('semester', 1, type=int)

    all_subjects = Subject.query.filter_by(program_id=student.section.program_id, year=selected_year, semester=selected_semester).order_by(Subject.description).all()
    grades = Grade.query.filter_by(student_id=student.id).all()
    grades_dict = {g.subject_id: g for g in grades}

    display_data = []
    for subject in all_subjects:
        display_data.append({'subject': subject, 'grade': grades_dict.get(subject.id)})

    # --- GWA LOGIC UPDATE ---
    total_points = 0
    total_units_graded = 0
    total_units_enrolled = 0

    for item in display_data:
        subject = item['subject']
        grade = item['grade']
        total_units_enrolled += float(subject.units)

        if grade and grade.final_grade_point is not None:
            total_points += grade.final_grade_point * float(subject.units)
            total_units_graded += float(subject.units)
    
    # Strict Rule: All subjects must be graded
    if total_units_graded == total_units_enrolled and total_units_enrolled > 0:
        gwa = round(total_points / total_units_graded, 2)
    else:
        gwa = 0

    latin_honor = None
    if gwa > 0:
        has_low_grade = False
        for item in display_data:
            g = item['grade']
            if g and g.final_grade_point and g.final_grade_point > 2.5:
                has_low_grade = True; break
        if not has_low_grade:
            if gwa <= 1.25: latin_honor = "President's Lister"
            elif gwa <= 1.75: latin_honor = "Dean's Lister"

    return render_template('admin/view_student_grades.html', title=f"Grades for {student.name}", student=student, display_data=display_data, gwa=gwa, latin_honor=latin_honor, selected_year=selected_year, selected_semester=selected_semester, active_nav='student_records')

@main.route("/admin/student/<int:student_id>/grades/print")
@login_required
@admin_required
def print_student_grades(student_id):
    student = Student.query.get_or_404(student_id)
    scope = get_admin_program_scope()
    if scope and student.section.program.code not in scope: return "Unauthorized", 403

    selected_year = request.args.get('year', 1, type=int)
    selected_semester = request.args.get('semester', 1, type=int)

    all_subjects = Subject.query.filter_by(program_id=student.section.program_id, year=selected_year, semester=selected_semester).order_by(Subject.description).all()
    grades = Grade.query.filter_by(student_id=student.id).all()
    grades_dict = {g.subject_id: g for g in grades}

    display_data = []
    for subject in all_subjects:
        display_data.append({'subject': subject, 'grade': grades_dict.get(subject.id)})

    # --- GWA LOGIC UPDATE ---
    total_points = 0
    total_units_graded = 0
    total_units_enrolled = 0

    for item in display_data:
        subject = item['subject']
        grade = item['grade']
        total_units_enrolled += float(subject.units)
        if grade and grade.final_grade_point is not None:
            total_points += grade.final_grade_point * float(subject.units)
            total_units_graded += float(subject.units)
    
    if total_units_graded == total_units_enrolled and total_units_enrolled > 0:
        gwa = round(total_points / total_units_graded, 2)
    else:
        gwa = 0

    latin_honor = None
    if gwa > 0:
        has_low_grade = False
        for item in display_data:
            g = item['grade']
            if g and g.final_grade_point and g.final_grade_point > 2.5:
                has_low_grade = True; break
        if not has_low_grade:
            if gwa <= 1.25: latin_honor = "President's Lister"
            elif gwa <= 1.75: latin_honor = "Dean's Lister"

    return render_template('admin/print_student_grades.html', student=student, display_data=display_data, gwa=gwa, latin_honor=latin_honor, selected_year=selected_year, selected_semester=selected_semester, current_date=datetime.now().strftime("%Y-%m-%d"))

@main.route("/admin/faculty_records")
@login_required
@admin_required
def admin_faculty_records():
    # Get the selected department from the URL, default to empty string
    dept_filter = request.args.get('department', '')

    # Start the query for all faculty members
    query = User.query.filter_by(role='faculty')

    # Apply filter if a department is selected
    if dept_filter:
        query = query.filter(User.department == dept_filter)

    # Execute query
    all_faculty = query.order_by(User.name).all()
    
    # Fetch programs to populate the dropdown options
    programs = Program.query.order_by(Program.code).all()

    return render_template(
        'admin/faculty_records.html', 
        title="Faculty Management", 
        active_nav='faculty_records', 
        faculties=all_faculty,
        programs=programs,
        selected_dept=dept_filter
    )

@main.route("/admin/programs")
@login_required
@admin_required
def manage_programs():
    programs = Program.query.order_by(Program.code).all()
    return render_template('admin/manage_programs.html', title="Manage Programs", active_nav='programs', programs=programs)

@main.route("/admin/programs/add", methods=['GET', 'POST'])
@login_required
@admin_required
def add_program():
    form = ProgramForm()
    if form.validate_on_submit():
        program = Program(code=form.code.data.upper(), name=form.name.data)
        db.session.add(program)
        db.session.commit()
        flash(f'Program {program.code} added successfully!', 'success')
        return redirect(url_for('main.manage_programs'))
    return render_template('admin/add_program.html', title="Add Program", active_nav='programs', form=form)

@main.route("/admin/programs/<program_id>")
@login_required
@admin_required
def view_program_subjects(program_id):
    program = Program.query.get_or_404(program_id)
    subjects = Subject.query.filter_by(program_id=program.id).order_by(Subject.year, Subject.semester, Subject.code).all()
    return render_template('admin/view_program_subjects.html', title=f"{program.code} Subjects", active_nav='programs', program=program, subjects=subjects)

@main.route("/admin/programs/<program_id>/add_subject", methods=['GET', 'POST'])
@login_required
@admin_required
def add_subject(program_id):
    program = Program.query.get_or_404(program_id)
    form = SubjectForm()
    if form.validate_on_submit():
        subject = Subject(
            code=form.code.data.upper(),
            description=form.description.data,
            units=form.units.data,
            year=form.year.data,
            semester=form.semester.data,
            program_id=program.id
        )
        db.session.add(subject)
        db.session.commit()
        flash(f'Subject {subject.code} added to {program.code}.', 'success')
        return redirect(url_for('main.view_program_subjects', program_id=program.id))
    return render_template('admin/add_subject.html', title="Add Subject", active_nav='programs', form=form, program=program)

@main.route("/admin/delete_program/<program_id>", methods=['POST'])
@login_required
@admin_required
def delete_program(program_id):
    program = Program.query.get_or_404(program_id)

    # Block deletion if user accounts (shared `users` table) still reference this program
    referencing_users = db.session.execute(
        db.text("SELECT COUNT(*) FROM users WHERE program_id = :pid"), {'pid': program_id}
    ).scalar()
    if referencing_users:
        flash(f'Cannot delete program "{program.program_code or program_id.upper()}". It is still assigned to {referencing_users} user account(s). Reassign or remove those accounts first.', 'danger')
        return redirect(url_for('main.manage_programs'))

    try:
        # Delete in dependency order to satisfy foreign key constraints.
        # All tables below are in the shared database, so raw SQL is used for
        # tables that are not mapped by this app's ORM models.
        db.session.execute(db.text("""
            DELETE FROM subject_prerequisites
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR prerequisite_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})

        db.session.execute(db.text("""
            DELETE FROM prerequisites
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR prerequisite_subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})

        db.session.execute(db.text("""
            DELETE FROM student_grades
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})

        db.session.execute(db.text("""
            DELETE FROM grades
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
        """), {'pid': program_id})

        db.session.execute(db.text("""
            DELETE FROM faculty_assignments
            WHERE subject_id IN (SELECT id FROM subjects WHERE program_id = :pid)
               OR section_id IN (SELECT id FROM sections WHERE program_id = :pid)
        """), {'pid': program_id})

        # Break self-referential prerequisite links before deleting subjects
        db.session.execute(db.text("""
            UPDATE subjects SET prerequisite_subject_id = NULL WHERE program_id = :pid
        """), {'pid': program_id})

        db.session.execute(db.text("DELETE FROM subjects WHERE program_id = :pid"), {'pid': program_id})

        # Detach applicant admission records from the program's sections instead of deleting them
        db.session.execute(db.text("""
            UPDATE applicants SET section_id = NULL
            WHERE section_id IN (SELECT id FROM sections WHERE program_id = :pid)
        """), {'pid': program_id})

        db.session.execute(db.text("DELETE FROM sections WHERE program_id = :pid"), {'pid': program_id})

        # Program content managed by the main website app (shared database)
        db.session.execute(db.text("DELETE FROM program_courses WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(db.text("DELETE FROM program_careers WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(db.text("DELETE FROM program_objectives WHERE program_id = :pid"), {'pid': program_id})
        db.session.execute(db.text("DELETE FROM courses WHERE program_id = :pid"), {'pid': program_id})

        db.session.delete(program)
        db.session.commit()
        flash(f'Program "{program.program_code or program_id.upper()}" and all associated data (subjects, sections, courses, etc.) have been deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete program "{program_id.upper()}": {e}', 'danger')

    return redirect(url_for('main.manage_programs'))

@main.route("/admin/delete_subject/<int:subject_id>", methods=['POST'])
@login_required
@admin_required
def delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    program_id = subject.program_id
    db.session.delete(subject)
    db.session.commit()
    flash('Subject deleted.', 'success')
    return redirect(url_for('main.view_program_subjects', program_id=program_id))

@main.route("/admin/add_faculty", methods=['GET', 'POST'])
@login_required
@admin_required
def add_faculty():
    form = RegistrationForm()
    if form.validate_on_submit():
        # Validate Major/Minor Logic
        department_name = "General Education" # Default
        
        if form.faculty_type.data == 'major':
            if not form.program_id.data:
                flash('Please select a Program/Department for Major Subject Instructors.', 'danger')
                return render_template('admin/add_faculty.html', title="Add Faculty", form=form, active_nav='faculty_records')
            
            program = Program.query.get(form.program_id.data)
            if program:
                department_name = program.code

        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        base_username = (form.email.data or form.name.data).split('@')[0].lower()
        username = base_username
        suffix = 1
        while User.query.filter_by(username=username).first():
            username = f"{base_username}{suffix}"
            suffix += 1
        new_faculty = User(
            username=username,
            name=form.name.data, 
            email=form.email.data,
            password_hash=hashed_password, 
            role='faculty', 
            is_approved=True, # Auto-approved since Dean created it
            department=department_name
        )
        db.session.add(new_faculty)
        db.session.commit()
        flash('Faculty member account created successfully.', 'success')
        return redirect(url_for('main.admin_faculty_records'))
        
    return render_template('admin/add_faculty.html', title="Add Faculty", form=form, active_nav='faculty_records')

@main.route("/admin/faculty/delete/<int:user_id>", methods=['POST'])
@login_required
@admin_required
def delete_faculty(user_id):
    faculty_to_delete = User.query.get_or_404(user_id)
    # Ensure we don't accidentally delete an admin
    if faculty_to_delete.role != 'faculty':
        flash('This user is not a faculty member and cannot be deleted from this page.', 'danger')
        return redirect(url_for('main.admin_faculty_records'))
    

    FacultyAssignment.query.filter_by(faculty_id=user_id).delete()

    Grade.query.filter_by(faculty_id=user_id).delete()
    
    # 3. Now it is safe to delete the user
    db.session.delete(faculty_to_delete)
    db.session.commit()
    
    flash(f'Faculty member {faculty_to_delete.name} and all their associated records (classes & grades) have been deleted.', 'success')
    return redirect(url_for('main.admin_faculty_records'))
    
    FacultyAssignment.query.filter_by(faculty_id=user_id).delete()
    
    db.session.delete(faculty_to_delete)
    db.session.commit()
    flash(f'Faculty member {faculty_to_delete.name} and all their class assignments have been deleted.', 'success')
    return redirect(url_for('main.admin_faculty_records'))

@main.route("/admin/student_records/print")
@login_required
@admin_required
def print_student_records():
    scope = get_admin_program_scope() # SCOPE FILTER

    search_query = request.args.get('search', '')
    program_filter = request.args.get('program', '')
    section_filter = request.args.get('section', '')
    
    students_query = Student.query.join(Section).join(Program).order_by(Program.name, Section.name, Student.last_name)

    # Apply Admin Scope
    if scope:
        students_query = students_query.filter(Program.code.in_(scope))

    program_name = None
    if search_query:
        search_term = f"%{search_query}%"
        students_query = students_query.filter(
            db.or_(
                Student.student_number.ilike(search_term),
                Student.first_name.ilike(search_term),
                Student.last_name.ilike(search_term)
            )
        )
    if program_filter:
        students_query = students_query.filter(Program.id == program_filter)
        program = Program.query.get(program_filter)
        if program:
            program_name = program.name
    if section_filter:
        sections = [s.strip().upper() for s in section_filter.split(',')]
        students_query = students_query.filter(func.upper(Section.name).in_(sections))

    students = students_query.all()

    return render_template(
        'admin/print_student_records.html',
        students=students,
        program_name=program_name,
        current_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@main.route("/admin/settings", methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    # Fetch the settings row (there should only be one)
    system_settings = SystemSettings.query.first()
    
    # Create default settings if none exist (First time run)
    if not system_settings:
        system_settings = SystemSettings(active_school_year='2024-2025', active_semester='1st Semester')
        db.session.add(system_settings)
        db.session.commit()

    form = SettingsForm(obj=system_settings)

    if form.validate_on_submit():
        system_settings.active_school_year = form.active_school_year.data
        system_settings.active_semester = form.active_semester.data
        db.session.commit()
        flash('System settings updated successfully.', 'success')
        return redirect(url_for('main.settings'))
        
    return render_template('admin/settings.html', title="System Settings", active_nav='settings', form=form)

@main.route("/faculty/print_report/<int:assignment_id>/<report_type>")
@login_required
def print_faculty_report(assignment_id, report_type):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    
    if assignment.faculty_id != current_user.id:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.faculty_dashboard'))

    students = Student.query.filter_by(section_id=assignment.section_id).order_by(Student.last_name).all()
    grades = Grade.query.filter_by(subject_id=assignment.subject_id, faculty_id=current_user.id).all()
    grades_dict = {g.student_id: g for g in grades}

    # Prepare data list for template
    student_data = []
    for s in students:
        g = grades_dict.get(s.id)
        
        # Calculate percentage and grade point based on type
        percentage = 0
        grade_point = 5.00 # Default
        
        if g:
            if report_type == 'prelim':
                percentage = g.prelim_grade or 0
            elif report_type == 'midterm':
                percentage = g.midterm_grade or 0
            elif report_type == 'finals':
                # For finals report, usually we show Final Term Grade
                percentage = g.final_term_grade or 0
            
            # Transmutation Logic (Reused)
            if percentage >= 96: grade_point = 1.00
            elif percentage >= 92: grade_point = 1.25
            elif percentage >= 88: grade_point = 1.50
            elif percentage >= 84: grade_point = 1.75
            elif percentage >= 80: grade_point = 2.00
            elif percentage >= 75: grade_point = 2.25
            elif percentage >= 70: grade_point = 2.50
            elif percentage >= 65: grade_point = 2.75
            elif percentage >= 60: grade_point = 3.00
            else: grade_point = 5.00
            
            # If no grade encoded yet, show 0/0
            if percentage == 0: grade_point = 0

        student_data.append({
            'name': s.name,
            'percentage': percentage,
            'grade_point': grade_point
        })

    # Determine Title
    titles = {
        'prelim': 'PRELIM GRADE',
        'midterm': 'MIDTERM GRADE',
        'finals': 'FINAL TERM GRADE'
    }
    
    return render_template(
        'faculty/print_rog.html',
        assignment=assignment,
        students=student_data,
        report_title=titles.get(report_type, 'REPORT ON GRADES'),
        current_date=datetime.now().strftime("%Y-%m-%d")
    )


@main.route("/admin/class/<int:assignment_id>/add_student", methods=['POST'])
@login_required
@admin_required
def admin_add_irregular_student(assignment_id):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    student_number = request.form.get('student_number')
    
    student = Student.query.filter_by(student_number=student_number).first()
    if not student:
        flash('Student ID not found.', 'danger')
        return redirect(url_for('main.grade_management', assignment_id=assignment.id)) # Redirect back to class view

    # Check if already enrolled in this specific class
    existing_grade = Grade.query.filter_by(
        student_id=student.id, 
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
    ).first()

    if existing_grade:
        flash('Student is already in this class list.', 'warning')
        return redirect(url_for('main.grade_management', assignment_id=assignment.id))

    # --- PREREQUISITE CHECK ---
    is_allowed, message = check_prerequisites(student.id, assignment.subject_id)
    if not is_allowed:
        flash(f'Cannot add student: {message}', 'danger')
        return redirect(url_for('main.grade_management', assignment_id=assignment.id))

    # If allowed, create a Grade entry (Effective Enrollment)
    new_grade = Grade(
        student_id=student.id,
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
        # All scores default to 0
    )
    db.session.add(new_grade)
    db.session.commit()

    flash(f'Irregular Student {student.name} successfully added to class.', 'success')
    return redirect(url_for('main.grade_management', assignment_id=assignment.id))