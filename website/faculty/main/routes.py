# app/main/routes.py

from flask import Blueprint, render_template, flash, redirect, url_for, jsonify, request, session, current_app
from flask_login import login_required, logout_user, current_user
from website import db, mail
from website.faculty.models import User, TeachingLoad, Program, Course, TimeSlot, SubstitutionRequest, Notification, SystemSetting, Section, Room, ProgramCourse, Subject, FacultyAssignment, Applicant, Grade
from website.faculty.auth.forms import AdminAddUserForm, UserFilterForm

from website.faculty.main.forms import ScheduleForm, DeleteScheduleForm, AddTimeSlotForm, SubstitutionRequestForm, SystemSettingsForm, EditProfileForm, ProgramForm, FileUploadForm, SchoolYearAnalyticsForm, SectionGridScheduleForm, EditScheduleForm, CourseForm, CreateSectionForm, DeleteSectionForm, RoomForm, UpdateRateForm, UploadProofForm

from datetime import datetime, time, timedelta
from collections import defaultdict
from sqlalchemy import func, and_, or_, text
from sqlalchemy.orm import joinedload
import pandas as pd
from flask_mail import Message
import json
from flask import send_file, send_from_directory
import io
import re
import os
from werkzeug.utils import secure_filename

main = Blueprint('faculty_main', __name__, template_folder='templates')


@main.app_template_filter('section_display_name')
def section_display_name(section_name, program_code):
    if section_name and program_code and section_name.strip().lower().startswith(program_code.lower()):
        return section_name.strip()[len(program_code):].strip()
    return section_name


DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

DEPARTMENT_MAP = {
    'Dean - CCBA': ['BSCS', 'BSMA', 'BPA'],
    'Dean - COC': ['BSCRIM']
}

# The system admin outranks the CCBA & COC admins (deans): full access to
# both the faculty system and the grading system, across every department.
FACULTY_ADMIN_ROLES = ('admin', 'superadmin')
FACULTY_MANAGER_ROLES = ('dean', 'admin', 'superadmin')


def is_faculty_admin():
    return current_user.is_authenticated and current_user.role in FACULTY_ADMIN_ROLES


def can_manage_faculty_system():
    return current_user.is_authenticated and current_user.role in FACULTY_MANAGER_ROLES


def _natural_sort_key(value):
    """Split text/number pairs so 'Room 10' sorts after 'Room 9'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'([0-9]+)', str(value))]


def _fix_time(t):
    """Treat midnight as 12:00 (end-of-day) instead of 00:00 on schedule grids."""
    return t.replace(hour=12) if t.hour == 0 else t


def parse_time_input(value, default=None):
    """Parse a time from either a native picker ("19:00") or a typed value.

    Accepts 24-hour and 12-hour strings so the native time pickers and any
    older/bookmarked forms both work.
    """
    if not value:
        return default
    raw = re.sub(r"\s+", " ", str(value).strip().upper().replace(".", ""))
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return default


def academic_term_status():
    """Return (ended, message) for the active academic term.

    The term is closed once today is past the stored school-year end date or the
    semester end date. A missing or unreadable date is treated as still open so
    a half-configured term does not lock scheduling out.
    """
    today = datetime.now().date()
    checks = (
        ('school_year_end', 'The current School Year has ended.'),
        ('semester_end_date', 'The current Semester has ended.'),
    )
    for key, message in checks:
        row = SystemSetting.query.filter_by(key=key).first()
        if not row or not row.value or not row.value.strip():
            continue
        try:
            end_date = datetime.strptime(row.value.strip(), '%Y-%m-%d').date()
        except ValueError:
            continue
        if today > end_date:
            return True, message
    return False, None


def term_closed_response(endpoint='faculty_main.manage_section_schedule_hub', **values):
    """Redirect with an explanation when the active term has already ended."""
    ended, message = academic_term_status()
    if not ended:
        return None
    flash(f'{message} Update the academic term in System Settings before assigning loads.', 'error')
    return redirect(url_for(endpoint, **values))


def get_faculty_program_scope():
    """Programs the current admin/dean may manage.

    CCBA/COC accounts only see their own department; the system admin
    (no department) outranks them and sees every program.
    """
    if not current_user.is_authenticated or current_user.role not in FACULTY_MANAGER_ROLES:
        return []
    if current_user.department == 'CCBA':
        return DEPARTMENT_MAP.get('Dean - CCBA', [])
    if current_user.department == 'COC':
        return DEPARTMENT_MAP.get('Dean - COC', [])
    if current_user.role in FACULTY_ADMIN_ROLES:
        return [code for codes in DEPARTMENT_MAP.values() for code in codes]
    return []


def send_schedule_update_email(faculty, schedule_details, action):
    """Sends an email to faculty about a schedule change."""
    
    if action == 'added':
        subject = 'A New Class Has Been Added to Your Schedule'
        action_text = 'A new class has been added to your teaching load:'
    elif action == 'updated':
        subject = 'Your Teaching Schedule Has Been Updated'
        action_text = 'A class in your teaching load has been updated to the following:'
    elif action == 'deleted':
        subject = 'A Class Has Been Removed From Your Schedule'
        action_text = 'The following class has been removed from your teaching load:'
    else:
        return # Do nothing if action is unknown

    try:
        msg = Message(subject,
                      recipients=[faculty.email])
        
        # Create a formatted HTML body for the email
        msg.html = render_template(
            'faculty/email/schedule_update.html', 
            faculty=faculty, 
            details=schedule_details, 
            action_text=action_text
        )
        
        mail.send(msg)
    except Exception as e:
        # If email fails, flash a warning to the dean but don't crash
        flash(f"Warning: The schedule was saved, but the notification email to {faculty.email} could not be sent. Error: {e}", 'warning')


def send_substitution_notification_emails(substitution_request):
    """Emails AND creates in-app notifications for all deans and other faculty."""
    load = substitution_request.teaching_load
    
    # --- START: MODIFIED DEAN NOTIFICATION LOGIC ---
    deans = User.query.filter_by(role='dean').all()
    for dean in deans:
        # Create in-app notification for the dean
        dean_message = (f"{load.teacher.full_name} has requested a substitute for their "
                        f"{load.course_code} class on {substitution_request.date_of_absence.strftime('%B %d, %Y')}.")
        dean_notification = Notification(user_id=dean.id, message=dean_message, substitution_request_id=substitution_request.id)
        db.session.add(dean_notification)

        # Send email to the dean
        try:
            msg = Message('Faculty Substitution Request', recipients=[dean.email])
            msg.html = render_template('faculty/email/substitution_notice_dean.html', request=substitution_request, load=load)
            mail.send(msg)
        except Exception as e:
            print(f"Failed to send email to dean {dean.email}: {e}")
    # --- END: MODIFIED DEAN NOTIFICATION LOGIC ---

    # --- START: MODIFIED FACULTY NOTIFICATION LOGIC ---
    other_faculty = User.query.filter(User.id != load.user_id, User.role == 'faculty', User.status == 'active').all()
    for faculty in other_faculty:
        # Create in-app notification for other faculty
        faculty_message = (f"An opportunity to substitute for {load.teacher.full_name}'s "
                           f"{load.course_code} class is available on {substitution_request.date_of_absence.strftime('%B %d, %Y')}.")
        faculty_notification = Notification(user_id=faculty.id, message=faculty_message, substitution_request_id=substitution_request.id)
        db.session.add(faculty_notification)

        # Send email to other faculty
        try:
            msg = Message('Opportunity to Substitute a Class', recipients=[faculty.email])
            msg.html = render_template('faculty/email/substitution_opportunity.html', request=substitution_request, load=load)
            mail.send(msg)
        except Exception as e:
            print(f"Failed to send email to faculty {faculty.email}: {e}")
    # --- END: MODIFIED FACULTY NOTIFICATION LOGIC ---
    
    # Commit all the new Notification objects to the database at once
    db.session.commit()

@main.route('/')
@main.route('/dashboard')
@login_required
def dashboard():
    # --- School Year and Semester logic ---
    current_semester_setting = SystemSetting.query.filter_by(key='current_semester').first()
    current_semester = int(current_semester_setting.value) if current_semester_setting else 1
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value if school_year_setting else None

    # --- IMPROVED EXPIRATION CHECK (Checks Both) ---
    term_ended, expiration_alert = academic_term_status()
    # -----------------------------

    if can_manage_faculty_system():
        filter_form = UserFilterForm(request.args)
        
        allowed_program_codes = get_faculty_program_scope()

        department_choices = [('', 'All Departments' if is_faculty_admin() else 'All My Departments')]
        if current_user.department == 'CCBA':
            department_choices.append(('Dean - CCBA', 'Dean - CCBA'))
        elif current_user.department == 'COC':
            department_choices.append(('Dean - COC', 'Dean - COC'))
        elif is_faculty_admin():
            # System admin (no department) outranks CCBA & COC: both available.
            department_choices.extend([(dept, dept) for dept in DEPARTMENT_MAP])

        if allowed_program_codes:
            program_depts = db.session.query(Program.program_code)\
                                    .filter(Program.program_code.in_(allowed_program_codes))\
                                    .distinct().all()
            department_choices.extend([(dept[0], dept[0]) for dept in program_depts])
        
        filter_form.department.choices = department_choices

        USER_DEPT_TO_FILTER_MAP = {'CCBA': 'Dean - CCBA', 'COC': 'Dean - COC'}
        default_department = USER_DEPT_TO_FILTER_MAP.get(current_user.department)
        selected_department = request.args.get('department', default_department)
        
        requested_program_codes = []
        if selected_department:
            if selected_department in DEPARTMENT_MAP:
                requested_program_codes = DEPARTMENT_MAP[selected_department]
            else:
                requested_program_codes = [selected_department]
        
        program_codes_to_query = [code for code in requested_program_codes if code in allowed_program_codes]

        if not selected_department:
             program_codes_to_query = allowed_program_codes

        is_aggregate_view = (not selected_department) or (selected_department in DEPARTMENT_MAP)
        base_count_query = db.session.query(User).filter(User.role == 'faculty', User.status == 'active')

        if is_aggregate_view:
            total_faculty = base_count_query.outerjoin(Program, User.program_id == Program.id).filter(
                or_(
                    Program.program_code.in_(allowed_program_codes), 
                    User.expertise_type == 'GENED',                  
                    User.expertise_type == 'Minor'                   
                )
            ).count()
        else:
            total_faculty = base_count_query.join(Program, User.program_id == Program.id).filter(
                Program.program_code.in_(program_codes_to_query)
            ).count()

        chart_faculty_query = db.session.query(User).filter(User.role == 'faculty', User.status == 'active')
        faculty_by_dept_query = chart_faculty_query.join(Program, User.program_id == Program.id)\
                                                  .with_entities(Program.program_code, func.count(User.id))\
                                                  .group_by(Program.program_code)
        
        if program_codes_to_query:
            faculty_by_dept_query = faculty_by_dept_query.filter(Program.program_code.in_(program_codes_to_query))
        else:
            faculty_by_dept_query = faculty_by_dept_query.filter(Program.program_code.in_(allowed_program_codes))

        faculty_by_dept_results = faculty_by_dept_query.order_by(func.count(User.id).desc()).all()
        dept_labels = [dept if dept else 'N/A' for dept, count in faculty_by_dept_results]
        dept_counts = [count for dept, count in faculty_by_dept_results]
        
        if is_aggregate_view:
            gened_count = db.session.query(User).filter(
                or_(User.expertise_type == 'GENED', User.expertise_type == 'Minor'),
                User.role == 'faculty', User.status == 'active'
            ).count()
            if gened_count > 0:
                dept_labels.append("GENED")
                dept_counts.append(gened_count)

        pending_approvals_count = User.query.filter_by(status='pending').count()
        pending_approvals_list = User.query.filter_by(status='pending').order_by(User.full_name).limit(5).all()
        
        absence_reports = 0
        recent_sub_requests = []
        if active_school_year:
            absence_reports = SubstitutionRequest.query.join(TeachingLoad, SubstitutionRequest.teaching_load_id == TeachingLoad.id)\
                                                    .filter(TeachingLoad.school_year == active_school_year, SubstitutionRequest.status != 'completed').count()
            recent_sub_requests = SubstitutionRequest.query.join(TeachingLoad, SubstitutionRequest.teaching_load_id == TeachingLoad.id)\
                                                    .filter(TeachingLoad.school_year == active_school_year).order_by(SubstitutionRequest.timestamp.desc()).limit(5).all()
        
        course_codes_in_sem = db.session.query(Course.course_code).filter(Course.semester == current_semester).distinct().scalar_subquery()
        
        base_schedule_query = TeachingLoad.query.filter(
            TeachingLoad.course_code.in_(course_codes_in_sem), 
            TeachingLoad.school_year == active_school_year,
            TeachingLoad.day.in_(DAYS_OF_WEEK),
            TeachingLoad.is_published == True 
        )

        if program_codes_to_query:
            section_filters_selected = [TeachingLoad.section.like(f"{code} %") for code in program_codes_to_query]
            base_schedule_query = base_schedule_query.filter(or_(*section_filters_selected))
        else:
            section_filters_base = [TeachingLoad.section.like(f"{code} %") for code in allowed_program_codes]
            base_schedule_query = base_schedule_query.filter(or_(*section_filters_base))

        active_schedules = base_schedule_query.count()
        
        schedules_by_day_results = base_schedule_query.with_entities(
            TeachingLoad.day, func.count(TeachingLoad.id)
        ).group_by(TeachingLoad.day).all()

        now = datetime.now()
        current_day = now.strftime('%A')
        current_time = now.time()
        
        available_faculty = []
        if now.weekday() < 6 and active_school_year:
            if is_aggregate_view:
                avail_faculty_query = db.session.query(User).outerjoin(Program, User.program_id == Program.id).filter(
                    User.role == 'faculty', User.status == 'active',
                    or_(Program.program_code.in_(allowed_program_codes), User.expertise_type == 'GENED', User.expertise_type == 'Minor')
                )
            else:
                avail_faculty_query = db.session.query(User).join(Program, User.program_id == Program.id).filter(
                    User.role == 'faculty', User.status == 'active',
                    Program.program_code.in_(program_codes_to_query)
                )
                
            all_relevant_faculty = avail_faculty_query.order_by(User.full_name).all()

            if all_relevant_faculty:
                active_faculty_ids = [f.id for f in all_relevant_faculty]
                busy_faculty_ids_query = db.session.query(TeachingLoad.user_id).filter(
                    TeachingLoad.user_id.in_(active_faculty_ids), TeachingLoad.day == current_day,
                    TeachingLoad.time_start <= current_time, TeachingLoad.time_end > current_time,
                    TeachingLoad.course_code.in_(course_codes_in_sem),
                    TeachingLoad.school_year == active_school_year
                ).distinct()
                busy_ids = {id for id, in busy_faculty_ids_query.all()}
                available_faculty = [f for f in all_relevant_faculty if f.id not in busy_ids]

        schedules_by_day_map = dict(schedules_by_day_results)
        day_labels = DAYS_OF_WEEK
        day_counts = [schedules_by_day_map.get(day, 0) for day in day_labels]

        chart_data = {'dept_labels': dept_labels, 'dept_counts': dept_counts, 'day_labels': day_labels, 'day_counts': day_counts}
        filter_form.department.data = selected_department
        # --- END DEAN LOGIC ---

        return render_template('faculty/dashboard.html', 
                               title='Dashboard', 
                               expiration_alert=expiration_alert, # <--- PASSED HERE
                               total_faculty=total_faculty, 
                               pending_approvals=pending_approvals_count, 
                               active_schedules=active_schedules, 
                               absence_reports=absence_reports, 
                               pending_approvals_list=pending_approvals_list, 
                               recent_sub_requests=recent_sub_requests, 
                               available_faculty=available_faculty, 
                               current_time_str=now.strftime('%I:%M %p'), 
                               chart_data=chart_data, 
                               filter_form=filter_form, 
                               selected_department=selected_department)
    
    else: # Faculty view
        course_codes_in_sem = db.session.query(Course.course_code).filter(Course.semester == current_semester).distinct().scalar_subquery()
        total_classes = db.session.query(func.count(TeachingLoad.id)).filter(
            TeachingLoad.user_id == current_user.id, 
            TeachingLoad.course_code.in_(course_codes_in_sem),
            TeachingLoad.school_year == active_school_year,
            TeachingLoad.is_published == True
        ).scalar() or 0
        course_units_subquery = db.session.query(Course.course_code, func.avg(Course.units).label('units')).filter(Course.semester == current_semester).group_by(Course.course_code).subquery()
        total_units_query = db.session.query(func.sum(course_units_subquery.c.units))\
                                    .select_from(TeachingLoad)\
                                    .join(course_units_subquery, TeachingLoad.course_code == course_units_subquery.c.course_code)\
                                    .filter(TeachingLoad.user_id == current_user.id, TeachingLoad.school_year == active_school_year, TeachingLoad.is_published == True).scalar()
        total_units = float(total_units_query) if total_units_query is not None else 0.0

        # Subjects the grading admin assigned to this faculty for the current
        # semester that are not already part of a scheduled teaching load, so
        # assigning a subject is reflected here instead of showing 0/0.
        scheduled_course_codes = {code for code, in db.session.query(TeachingLoad.course_code).filter(
            TeachingLoad.user_id == current_user.id,
            TeachingLoad.school_year == active_school_year,
            TeachingLoad.is_published == True
        ).all()}
        assigned_subjects = db.session.query(Subject).join(
            FacultyAssignment, FacultyAssignment.subject_id == Subject.id
        ).filter(
            FacultyAssignment.faculty_id == grading_faculty_id(),
            Subject.semester == current_semester
        ).all()
        extra_subjects = [s for s in assigned_subjects if s.subject_code not in scheduled_course_codes]
        total_classes += len(extra_subjects)
        total_units += sum(float(s.units or 0) for s in extra_subjects)
        pending_sub_requests = 0
        if active_school_year:
            pending_sub_requests = Notification.query.join(SubstitutionRequest, Notification.substitution_request_id == SubstitutionRequest.id)\
                                                    .join(TeachingLoad, SubstitutionRequest.teaching_load_id == TeachingLoad.id)\
                                                    .filter(Notification.user_id == current_user.id, Notification.is_read == False, 
                                                            TeachingLoad.school_year == active_school_year).count()
        today_schedule_data = []
        now = datetime.now()
        current_day = now.strftime('%A') 
        if now.weekday() < 6 and active_school_year:
            today_schedules_query = db.session.query(TeachingLoad).filter(
                TeachingLoad.user_id == current_user.id, 
                TeachingLoad.day == current_day, 
                TeachingLoad.course_code.in_(course_codes_in_sem),
                TeachingLoad.school_year == active_school_year,
                TeachingLoad.is_published == True
            ).order_by(TeachingLoad.time_start).all()
            today_schedule_data = [{'time': f"{load.time_start.strftime('%I:%M %p')} - {load.time_end.strftime('%I:%M %p')}", 'course': load.course_code, 'section': load.section, 'room': load.room} for load in today_schedules_query]
            
        return render_template('faculty/dashboard.html', 
                               title='Dashboard', 
                               expiration_alert=expiration_alert, 
                               total_classes=total_classes, 
                               total_units=int(total_units), 
                               pending_sub_requests=pending_sub_requests, 
                               today_schedule=today_schedule_data)
    
@main.route('/users', methods=['GET', 'POST'])
@login_required
def user_management():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    add_user_form = AdminAddUserForm()
    filter_form = UserFilterForm(request.args)

    all_programs = Program.query.order_by(Program.program_code).all()
    add_user_form.program.choices = [(p.id, p.program_code) for p in all_programs]
    
    # Populate Department Filter Choices
    department_choices = [('', 'All Departments'), ('Dean - CCBA', 'Dean - CCBA'), ('Dean - COC', 'Dean - COC')]
    program_depts = db.session.query(Program.program_code).distinct().all()
    department_choices.extend([(dept[0], dept[0]) for dept in program_depts])
    filter_form.department.choices = department_choices

    # --- HANDLE USER CREATION (POST) ---
    if add_user_form.validate_on_submit():
        # Check duplicates
        if User.query.filter_by(email=add_user_form.email.data).first():
            flash('This email is already registered.', 'error')
        elif add_user_form.role.data == 'dean' and add_user_form.username.data and User.query.filter_by(username=add_user_form.username.data).first():
            flash('This admin username is already taken.', 'error')
        else:
            # Create new user instance (Contact Number is omitted here)
            new_user = User(
                full_name=add_user_form.full_name.data, 
                email=add_user_form.email.data
            )
            
            new_user.set_password(add_user_form.password.data)
            new_user.role = add_user_form.role.data
            new_user.status = 'active' # Admin-created users are active immediately
            
            # Specific logic based on Role
            if new_user.role == 'dean':
                if add_user_form.username.data:
                    new_user.username = add_user_form.username.data
                else:
                    flash('Admin Username is required when creating a new Dean.', 'error')
                    return redirect(url_for('faculty_main.user_management'))
            else:
                # Logic for Faculty (PROFED vs GENED)
                new_user.expertise_type = add_user_form.expertise_type.data
                
                if new_user.expertise_type == 'PROFED':
                    if not add_user_form.program.data:
                        flash('You must select a program for PROFED faculty.', 'error')
                        return redirect(url_for('faculty_main.user_management'))
                    new_user.program_id = add_user_form.program.data.id
                else:
                    # GENED or others don't belong to a specific program
                    new_user.program_id = None
            
            db.session.add(new_user)
            db.session.commit()
            flash(f'User "{new_user.full_name}" has been created successfully.', 'success')
        return redirect(url_for('faculty_main.user_management'))

    # --- HANDLE FILTERING AND LIST DISPLAY (GET) ---
    if 'department' in request.args:
        selected_department = request.args.get('department')
        session['selected_department_users'] = selected_department
    elif 'reset' in request.args or 'clear' in request.args:
        session.pop('selected_department_users', None)
        return redirect(url_for('faculty_main.user_management'))
    else:
        selected_department = session.get('selected_department_users', '') 
    
    query = User.query.outerjoin(Program, User.program_id == Program.id)

    role_filter = filter_form.role.data
    dept_filter = selected_department

    if role_filter:
        query = query.filter(User.role == role_filter)

    if dept_filter:
        dept_conditions = []

        if role_filter != 'dean':
            # Include GENED and Minor in filters if specific program isn't strictly required by logic, 
            # or adjust based on your preference. Usually generic filters include generic faculty.
            dept_conditions.append(User.expertise_type == 'GENED') 
            dept_conditions.append(User.expertise_type == 'Minor') # Legacy support

        if dept_filter in DEPARTMENT_MAP:
            # If filtering by a Dean's Department (e.g., "Dean - CCBA")
            program_codes = DEPARTMENT_MAP[dept_filter]
            dean_department_key = dept_filter.split(' - ')[1]
            
            if role_filter != 'faculty':
                dept_conditions.append(and_(User.role == 'dean', User.department == dean_department_key))
            
            if role_filter != 'dean':
                # Include PROFEDs belonging to programs under this Dean
                dept_conditions.append(and_(
                    or_(User.expertise_type == 'PROFED', User.expertise_type == 'Major'), 
                    Program.program_code.in_(program_codes)
                ))
        
        else:
            # If filtering by a specific Program (e.g., "BSCS")
            if role_filter != 'dean':
                dept_conditions.append(and_(
                    or_(User.expertise_type == 'PROFED', User.expertise_type == 'Major'), 
                    Program.program_code == dept_filter
                ))

        if dept_conditions:
            query = query.filter(or_(*dept_conditions))
        else:
            # If filter exists but matches nothing valid
            query = query.filter(False)
            
    user_list = query.order_by(User.role.desc(), User.full_name).all()

    filter_form.department.data = selected_department

    return render_template('faculty/user_management.html', title='User Management', user_list=user_list, form=add_user_form, filter_form=filter_form)

@main.route('/scheduling')
@login_required
def scheduling():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    filter_form = UserFilterForm(request.args)
    
    # Setup Department Choices
    department_choices = [('', 'All Departments'), ('Dean - CCBA', 'Dean - CCBA'), ('Dean - COC', 'Dean - COC')]
    program_depts = db.session.query(Program.program_code).distinct().all()
    department_choices.extend([(dept[0], dept[0]) for dept in program_depts])
    filter_form.department.choices = department_choices
    
    # Determine Filter
    selected_department = request.args.get('department', '')
    program_codes_to_query = []
    
    if selected_department:
        if selected_department in DEPARTMENT_MAP:
            program_codes_to_query = DEPARTMENT_MAP[selected_department]
        elif selected_department != '':
             program_codes_to_query = [selected_department]

    # --- 1. GET GENED FACULTY (Visible to Everyone) ---
    # Includes 'GENED' (New) and 'Minor' (Old)
    gened_faculty = User.query.filter(
        User.role == 'faculty', 
        User.status == 'active', 
        or_(User.expertise_type == 'GENED', User.expertise_type == 'Minor')
    ).all()

    # --- 2. GET PROFED FACULTY (Filtered by Department) ---
    # Includes 'PROFED' (New) and 'Major' (Old)
    profed_faculty_query = User.query.filter(
        User.role == 'faculty', 
        User.status == 'active', 
        or_(User.expertise_type == 'PROFED', User.expertise_type == 'Major')
    )
    
    # Apply Department/Program Filter
    if program_codes_to_query:
        profed_faculty_query = profed_faculty_query.join(Program, User.program_id == Program.id)\
                                                 .filter(Program.program_code.in_(program_codes_to_query))

    profed_faculty = profed_faculty_query.all()

    # --- 3. COMBINE & SORT ---
    # Use a dictionary to remove duplicates if a user accidentally appears in both lists
    all_faculty_map = {user.id: user for user in gened_faculty + profed_faculty}
    faculty_list = sorted(all_faculty_map.values(), key=lambda x: x.full_name)
    
    filter_form.department.data = selected_department
    
    term_ended, term_ended_message = academic_term_status()
    return render_template('faculty/scheduling_hub.html', title='Select Faculty to Schedule', faculty_list=faculty_list, filter_form=filter_form, selected_department=selected_department, term_ended=term_ended, term_ended_message=term_ended_message)
    
    filter_form = UserFilterForm(request.args)
    
    department_choices = [('', 'All Departments'), ('Dean - CCBA', 'Dean - CCBA'), ('Dean - COC', 'Dean - COC')]
    program_depts = db.session.query(Program.program_code).distinct().all()
    department_choices.extend([(dept[0], dept[0]) for dept in program_depts])
    filter_form.department.choices = department_choices
    
    USER_DEPT_TO_FILTER_MAP = {'CCBA': 'Dean - CCBA', 'COC': 'Dean - COC'}
    default_department = USER_DEPT_TO_FILTER_MAP.get(current_user.department)
    selected_department = request.args.get('department', '')

    # --- START: CORRECTED FILTERING LOGIC ---
    
    # Always get all active "Minor" faculty
    minor_faculty = User.query.filter(
        User.role == 'faculty', 
        User.status == 'active', 
        User.expertise_type == 'Minor'
    ).all()

    # Base query for all active "Major" faculty
    major_faculty_query = User.query.filter(
        User.role == 'faculty', 
        User.status == 'active', 
        User.expertise_type == 'Major'
    )

    program_codes_to_query = []
    if selected_department:
        if selected_department in DEPARTMENT_MAP:
            program_codes_to_query = DEPARTMENT_MAP[selected_department]
        elif selected_department != '': # Handle 'All Departments' case
             program_codes_to_query = [selected_department]
    
    # If a filter is active, apply it ONLY to the major_faculty_query
    if program_codes_to_query:
        major_faculty_query = major_faculty_query.join(Program, User.program_id == Program.id)\
                                                 .filter(Program.program_code.in_(program_codes_to_query))

    # Execute the query for major faculty
    major_faculty = major_faculty_query.all()

    # Combine the two lists of User objects
    # Use a dictionary to ensure no duplicates if a user somehow ends up in both lists
    all_faculty = {user.id: user for user in minor_faculty + major_faculty}
    
    # Get the final list of unique User objects and sort it
    faculty_list = sorted(all_faculty.values(), key=lambda x: x.full_name)
    
    
    filter_form.department.data = selected_department

    term_ended, term_ended_message = academic_term_status()
    return render_template('faculty/scheduling_hub.html', title='Select Faculty to Schedule', faculty_list=faculty_list, filter_form=filter_form, selected_department=selected_department, term_ended=term_ended, term_ended_message=term_ended_message)

@main.route('/schedule-grid/<int:faculty_id>/sem/<int:semester>', methods=['GET', 'POST'])
@login_required
def schedule_grid(faculty_id, semester):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))
        
    faculty_member = User.query.get_or_404(faculty_id)
    schedule_id_to_edit = request.args.get('edit', type=int)
    schedule_to_edit = TeachingLoad.query.get(schedule_id_to_edit) if schedule_id_to_edit else None
    
    form = ScheduleForm()
    
    # Settings
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value.strip() if school_year_setting and school_year_setting.value else None
    
    # 1. Load Rooms (Force reload to prevent empty choices)
    all_rooms = Room.query.order_by(Room.room_name).all()
    form.room.choices = [(r.room_name, r.room_name) for r in all_rooms]
    if not form.room.choices:
        form.room.choices = [('', 'No Rooms Created')]

    # Pre-populate Form for POST checks
    if schedule_to_edit and request.method == 'GET':
        form = ScheduleForm(obj=schedule_to_edit)
        form.time_start.data = schedule_to_edit.time_start.strftime('%I:%M %p')
        form.time_end.data = schedule_to_edit.time_end.strftime('%I:%M %p')
        form.room.data = schedule_to_edit.room
        
        # We don't set form.program/course/section here for the UI anymore, 
        # we handle that via the 'edit_data' variable sent to JavaScript below.
        # But we need to set valid choices for validation to pass if user submits immediately.
        current_section_name = schedule_to_edit.section
        form.section.choices = [(current_section_name, current_section_name)]
        form.section.data = current_section_name

    if not schedule_to_edit and request.method == 'GET' and not form.program.data:
        # Check if faculty has a specific program assigned (PROFED/Major)
        if faculty_member.program_id:
            # Verify this program has courses in this semester to avoid errors
            has_courses = Program.query.filter(Program.id == faculty_member.program_id, Program.courses.any(Course.semester == semester)).first()
            if has_courses:
                form.program.data = has_courses

    # --- DYNAMIC CHOICES (FIXED) ---
    # We allow ALL programs to be selected. 
    # This prevents the issue where "BSCS" (ID 4) is hidden because the faculty is "General Ed".
    program_query = Program.query.filter(Program.courses.any(Course.semester == semester))
    form.program.query = program_query.order_by(Program.program_code)

    # Dynamic choices update on POST
    if request.method == 'POST' and form.program.data:
        form.course.choices = [(c.id, f"{c.course_code} - {c.course_description}") for c in Course.query.filter_by(program_id=form.program.data.id, semester=semester).order_by(Course.course_code).all()]
        if active_school_year:
            # We fetch all sections for validation purposes
            potential_sections = Section.query.filter_by(program_id=form.program.data.id, semester=semester).all()
            form.section.choices = [(s.name, s.name) for s in potential_sections]

    # Handlers
    delete_form = DeleteScheduleForm()
    time_slot_form = AddTimeSlotForm()

    if 'add_time_slot_submit' in request.form:
        return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester))

    if 'add_schedule_submit' in request.form:
        blocked = term_closed_response(
            'faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester)
        if blocked:
            return blocked
        if hasattr(form, 'faculty'): del form.faculty
        # We skip validating choices for dynamic fields strictly because JS modifies them
        form.course.choices = [(form.course.data, 'selected')] 
        form.section.choices = [(form.section.data, 'selected')]

        if form.validate():
            if not active_school_year:
                flash('Error: Active school year not set.', 'error')
                return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester))
            try:
                t_start_str = form.time_start.data.strip(); t_end_str = form.time_end.data.strip()
                if len(t_start_str.split(':')[0]) == 1: t_start_str = "0" + t_start_str
                if len(t_end_str.split(':')[0]) == 1: t_end_str = "0" + t_end_str
                time_start = datetime.strptime(t_start_str, '%I:%M %p').time()
                time_end = datetime.strptime(t_end_str, '%I:%M %p').time()
            except:
                flash('Invalid time format.', 'error')
                return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester))

            schedule = schedule_to_edit or TeachingLoad(teacher=faculty_member)
            selected_course = Course.query.get(form.course.data)
            
            # Construct Full Section Name
            # If the user selected a Program (e.g. BSCS) and a section (e.g. 2A), 
            # we combine them -> "BSCS 2A" unless it already contains it.
            raw_section = form.section.data
            prog_code = form.program.data.program_code
            if raw_section.startswith(prog_code):
                 full_section_name = raw_section
            else:
                 full_section_name = f"{prog_code} {raw_section}"

            # Conflict Checking
            conflicts = []
            # 1. Faculty Conflict
            f_conf = TeachingLoad.query.join(Course, TeachingLoad.course_code == Course.course_code).filter(
                TeachingLoad.user_id == faculty_id, Course.semester == semester, 
                TeachingLoad.day == form.day.data, TeachingLoad.time_start < time_end, TeachingLoad.time_end > time_start, 
                TeachingLoad.school_year == active_school_year
            )
            if schedule_to_edit: f_conf = f_conf.filter(TeachingLoad.id != schedule_to_edit.id)
            if f_conf.first(): conflicts.append(f"Faculty {faculty_member.full_name} has a class.")

            # 2. Section Conflict
            s_conf = db.session.query(TeachingLoad).join(Course, TeachingLoad.course_code == Course.course_code).filter(
                TeachingLoad.section == full_section_name, TeachingLoad.day == form.day.data,
                TeachingLoad.time_start < time_end, TeachingLoad.time_end > time_start,
                Course.semester == semester, TeachingLoad.school_year == active_school_year
            )
            if schedule_to_edit: s_conf = s_conf.filter(TeachingLoad.id != schedule_to_edit.id)
            if s_conf.first(): conflicts.append(f"Section {full_section_name} has a class.")

            # 3. Room Conflict
            r_conf = TeachingLoad.query.join(Course, TeachingLoad.course_code == Course.course_code).filter(
                TeachingLoad.room == form.room.data, Course.semester == semester, 
                TeachingLoad.day == form.day.data, TeachingLoad.time_start < time_end, TeachingLoad.time_end > time_start, 
                TeachingLoad.school_year == active_school_year
            )
            if schedule_to_edit: r_conf = r_conf.filter(TeachingLoad.id != schedule_to_edit.id)
            if r_conf.first(): conflicts.append(f"Room {form.room.data} is booked.")

            if conflicts:
                flash(" | ".join(conflicts), 'error')
            else:
                schedule.section = full_section_name
                schedule.course_code = selected_course.course_code
                schedule.course_description = selected_course.course_description
                schedule.day = form.day.data
                schedule.time_start = time_start
                schedule.time_end = time_end
                schedule.room = form.room.data
                schedule.school_year = active_school_year
                if not schedule_to_edit: db.session.add(schedule)
                db.session.commit()
                flash('Schedule saved successfully.', 'success')
                
                # Email notification logic here...
                
            return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester))
        else:
            for f, e in form.errors.items(): flash(f"{f}: {e[0]}", 'error')

    # --- GAP-MERGING WATERFALL LOGIC (Same as before) ---
    schedules = []
    if active_school_year:
        schedules = db.session.query(TeachingLoad)\
            .join(Course, TeachingLoad.course_code == Course.course_code)\
            .filter(
                TeachingLoad.user_id == faculty_id, 
                Course.semester == semester,
                TeachingLoad.school_year == active_school_year
            ).all()

    pending_lookup = {req.teaching_load_id: req for req in SubstitutionRequest.query.filter_by(original_teacher_id=faculty_id, status='pending').all()}
    schedules_by_day = defaultdict(list)
    for s in schedules: 
        schedules_by_day[s.day].append({'schedule': s, 'has_pending_request': s.id in pending_lookup})

    schedule_grid_data = []
    today_date = datetime.now().date()
    current_time = datetime.combine(today_date, datetime.strptime("07:00", "%H:%M").time())
    limit_time = datetime.combine(today_date, datetime.strptime("17:30", "%H:%M").time())
    if schedules:
        max_sched_end = max(s.time_end for s in schedules)
        max_sched_end_dt = datetime.combine(today_date, max_sched_end)
        if max_sched_end_dt > limit_time: limit_time = max_sched_end_dt

    while current_time < limit_time:
        row_start = current_time
        next_hard_stop = None
        has_class_starting_now = False
        for day in DAYS_OF_WEEK:
            for item in schedules_by_day[day]:
                s_start = datetime.combine(today_date, item['schedule'].time_start)
                if s_start == row_start: has_class_starting_now = True
                elif s_start > row_start: 
                    if next_hard_stop is None or s_start < next_hard_stop: next_hard_stop = s_start
        row_end = row_start + timedelta(minutes=30)
        if not has_class_starting_now and next_hard_stop:
            gap_minutes = (next_hard_stop - row_start).total_seconds() / 60
            if gap_minutes <= 120: row_end = next_hard_stop
        
        row_days = []
        max_end_found = row_end 
        for day in DAYS_OF_WEEK:
            found = None
            for item in schedules_by_day[day]:
                sched_start = datetime.combine(today_date, item['schedule'].time_start)
                sched_end = datetime.combine(today_date, item['schedule'].time_end)
                if sched_start == row_start:
                    found = item
                    if sched_end > max_end_found: max_end_found = sched_end
                    break
            row_days.append(found)
        row_end = max_end_found
        
        class VirtualSlot:
            def __init__(self, s, e):
                self.id = 0; self.is_default = True; self.start_time = s.time(); self.end_time = e.time()
        
        schedule_grid_data.append({
            'slot': VirtualSlot(row_start, row_end),
            'display_time': f"{row_start.strftime('%I:%M').lstrip('0')} - {row_end.strftime('%I:%M %p').lstrip('0')}", 
            'form_start': row_start.strftime('%I:%M %p'),
            'form_end': row_end.strftime('%I:%M %p'),
            'days': row_days
        })
        current_time = row_end

    # --- PREPARE DATA FOR JAVASCRIPT EDIT MODAL (CORRECTED) ---
    edit_data = {}
    if schedule_to_edit:
        full_section = schedule_to_edit.section.strip()
        
        # 1. Determine Program
        found_program = None
        course_obj = Course.query.filter_by(course_code=schedule_to_edit.course_code).first()
        if course_obj and course_obj.program:
            found_program = course_obj.program
        
        if not found_program:
            all_programs = Program.query.all()
            all_programs.sort(key=lambda x: len(x.program_code), reverse=True)
            for prog in all_programs:
                if full_section.startswith(prog.program_code):
                    found_program = prog
                    break

        # 2. Identify Course Info
        course_id = course_obj.id if course_obj else ''
        course_text = f"{course_obj.course_code} - {course_obj.course_description}" if course_obj else ''

        # 3. Explicitly Pass Room and Section
        # This fixes the issue where room/section might be missing in the form object
        section_name = full_section
        room_name = schedule_to_edit.room.strip() if schedule_to_edit.room else ""

        edit_data = {
            'program_id': found_program.id if found_program else '',
            'course_id': course_id,
            'course_text': course_text,
            'section': section_name,
            'room': room_name  # <--- EXPLICIT ROOM PASSING
        }
        print(f"DEBUG: Edit Data Final: {edit_data}")

    # Ensure rooms are never empty
    all_rooms_refresh = Room.query.order_by(Room.room_name).all()
    form.room.choices = [(r.room_name, r.room_name) for r in all_rooms_refresh]
    if not form.room.choices: form.room.choices = [('', 'No Rooms Created')]

    term_ended, term_ended_message = academic_term_status()

    return render_template('faculty/schedule_grid.html', title=f"Scheduling for {faculty_member.full_name}", 
                           faculty=faculty_member, semester=semester, time_slots=[], 
                           days=DAYS_OF_WEEK, schedule_grid=schedule_grid_data, form=form, 
                           time_slot_form=time_slot_form, delete_form=delete_form, 
                           editing_schedule_id=schedule_id_to_edit,
                           edit_data=edit_data,
                           faculty_program_id=faculty_member.program_id if faculty_member.program_id else '',
                           term_ended=term_ended,
                           term_ended_message=term_ended_message)


@main.route('/section-grid/prog<program_id>/sec<section_name>/sem<int:semester>', methods=['GET', 'POST'])
@login_required
def section_schedule_grid(program_id, section_name, semester):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    # --- 1. SETUP VARIABLES ---
    program = Program.query.get_or_404(program_id)
    # Sections created by the admin "Manage Sections" page store their name with
    # the program code prefix (e.g. "bscs 1A"). Strip it so the grid uses the
    # same "<program_code> <name>" convention (e.g. "BSCS 1A").
    if section_name.lower().startswith(program.program_code.lower()):
        section_name = section_name[len(program.program_code):].strip()
    full_section_name = f"{program.program_code} {section_name}"
    
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    current_school_year = school_year_setting.value.strip() if school_year_setting and school_year_setting.value else None

    # Forms
    add_form = SectionGridScheduleForm()
    edit_form = EditScheduleForm()
    delete_form = DeleteScheduleForm()

    # --- 2. POPULATE DROPDOWNS ---
    relevant_faculty = User.query.filter(User.role == 'faculty', User.status == 'active').order_by(User.full_name).all()
    faculty_choices = [(str(f.id), f.full_name) for f in relevant_faculty]
    
    section_year = None
    try: section_year = int(section_name[0])
    except: pass
    
    courses_query = Course.query.filter_by(program_id=program_id, semester=semester)
    if section_year: courses_query = courses_query.filter(Course.year == section_year)
    courses_for_semester = courses_query.order_by(Course.course_code).all()
    course_choices = [(str(c.id), f"{c.course_code} - {c.course_description}") for c in courses_for_semester]
    
    all_rooms = Room.query.order_by(Room.room_name).all()
    room_choices = [(r.room_name, r.room_name) for r in all_rooms] or [('', 'No Rooms Created')]

    # Apply choices
    add_form.course.choices = course_choices
    add_form.faculty.choices = faculty_choices
    add_form.room.choices = room_choices
    add_form.day.choices = [(d, d) for d in DAYS_OF_WEEK]

    edit_form.course.choices = course_choices
    edit_form.faculty.choices = faculty_choices
    edit_form.room.choices = room_choices
    edit_form.day.choices = [(d, d) for d in DAYS_OF_WEEK]

    # --- 3. HANDLE ADD SCHEDULE ---
    if 'add_schedule_submit' in request.form:
        blocked = term_closed_response(
            'faculty_main.section_schedule_grid',
            program_id=program_id, section_name=section_name, semester=semester)
        if blocked:
            return blocked
        add_form.course.choices = [(add_form.course.data, 'selected')]
        add_form.faculty.choices = [(add_form.faculty.data, 'selected')]
        add_form.room.choices = [(add_form.room.data, 'selected')]
        
        if add_form.validate():
            if not current_school_year: 
                flash('Error: School year not set.', 'error')
            else:
                try:
                    time_start = parse_time_input(add_form.time_start.data)
                    time_end = parse_time_input(add_form.time_end.data)
                    if not time_start or not time_end:
                        flash('Invalid time. Use the time pickers.', 'error')
                        raise ValueError('unparseable time')

                    selected_course = Course.query.get(int(add_form.course.data))
                    faculty_member = User.query.get(int(add_form.faculty.data))

                    # Overlap checks are scoped to this semester as well as the
                    # school year: without the Course join, a 1st-semester class
                    # blocked the same slot in the 2nd semester.
                    def _overlap(*criteria, **extra):
                        return (TeachingLoad.query
                                .join(Course, TeachingLoad.course_code == Course.course_code)
                                .filter(*criteria,
                                        TeachingLoad.school_year == current_school_year,
                                        Course.semester == semester,
                                        TeachingLoad.day == add_form.day.data,
                                        TeachingLoad.time_start < time_end,
                                        TeachingLoad.time_end > time_start,
                                        **extra)
                                .first())

                    if time_start >= time_end:
                        flash('Error: the end time must be after the start time.', 'error')
                    else:
                        faculty_conflict = _overlap(TeachingLoad.user_id == faculty_member.id)
                        section_conflict = _overlap(TeachingLoad.section == full_section_name)
                        room_conflict = _overlap(TeachingLoad.room == add_form.room.data)

                        if faculty_conflict: flash(f'Conflict: {faculty_member.full_name} is busy at that time.', 'error')
                        elif section_conflict: flash(f'Conflict: {full_section_name} already has a class at that time.', 'error')
                        elif room_conflict: flash(f'Conflict: {add_form.room.data} is already occupied at that time.', 'error')
                        else:
                            new_schedule = TeachingLoad(
                                teacher=faculty_member, 
                                section=full_section_name, 
                                course_code=selected_course.course_code, 
                                course_description=selected_course.course_description, 
                                day=add_form.day.data, 
                                time_start=time_start, 
                                time_end=time_end, 
                                room=add_form.room.data, 
                                school_year=current_school_year,
                                is_published=False 
                            )
                            db.session.add(new_schedule)
                            db.session.commit()
                            flash('Class added as DRAFT. Click "Save & Notify Faculty" to finalize.', 'info')
                except Exception as e:
                    if str(e) != 'unparseable time':
                        flash(f'Error: {str(e)}', 'error')
            return redirect(url_for('faculty_main.section_schedule_grid', program_id=program_id, section_name=section_name, semester=semester))
        else:
            for f, e in add_form.errors.items(): flash(f"Add Error ({f}): {', '.join(e)}", 'error')

    # --- 5. HANDLE EDIT SCHEDULE ---
    if 'update_schedule_submit' in request.form:
        blocked = term_closed_response(
            'faculty_main.section_schedule_grid',
            program_id=program_id, section_name=section_name, semester=semester)
        if blocked:
            return blocked
        edit_form.course.choices = [(edit_form.course.data, 'selected')]
        edit_form.faculty.choices = [(edit_form.faculty.data, 'selected')]
        edit_form.room.choices = [(edit_form.room.data, 'selected')]

        if edit_form.validate():
            schedule_to_edit = TeachingLoad.query.get_or_404(edit_form.schedule_id.data)
            try:
                time_start = parse_time_input(request.form.get('time_start'))
                time_end = parse_time_input(request.form.get('time_end'))
                if not time_start or not time_end:
                    flash('Invalid time. Use the time pickers.', 'error')
                    raise ValueError('unparseable time')

                selected_course = Course.query.get(int(edit_form.course.data))
                faculty_member = User.query.get(int(edit_form.faculty.data))
                new_room = edit_form.room.data
                new_day = edit_form.day.data or schedule_to_edit.day
                if new_day not in DAYS_OF_WEEK:
                    new_day = schedule_to_edit.day

                # Overlap checks (excluding this row) are scoped to the semester
                # and the school year so the two semesters do not block each other.
                def _overlap(*criteria, **extra):
                    return (TeachingLoad.query
                            .join(Course, TeachingLoad.course_code == Course.course_code)
                            .filter(*criteria,
                                    TeachingLoad.id != schedule_to_edit.id,
                                    TeachingLoad.school_year == schedule_to_edit.school_year,
                                    Course.semester == semester,
                                    TeachingLoad.day == new_day,
                                    TeachingLoad.time_start < time_end,
                                    TeachingLoad.time_end > time_start,
                                    **extra)
                            .first())

                if time_start >= time_end:
                    flash('Error: the end time must be after the start time.', 'error')
                else:
                    faculty_conflict = _overlap(TeachingLoad.user_id == faculty_member.id)
                    section_conflict = _overlap(TeachingLoad.section == full_section_name)
                    room_conflict = _overlap(TeachingLoad.room == new_room)

                    if faculty_conflict: flash(f'Conflict: {faculty_member.full_name} is busy at that time.', 'error')
                    elif section_conflict: flash(f'Conflict: {full_section_name} already has a class at that time.', 'error')
                    elif room_conflict: flash(f'Conflict: {new_room} is already occupied at that time.', 'error')
                    else:
                        schedule_to_edit.user_id = faculty_member.id
                        schedule_to_edit.course_code = selected_course.course_code
                        schedule_to_edit.course_description = selected_course.course_description
                        schedule_to_edit.day = new_day
                        schedule_to_edit.time_start = time_start
                        schedule_to_edit.time_end = time_end
                        schedule_to_edit.room = new_room
                        db.session.commit()
                        flash('Schedule updated.', 'success')
                        if schedule_to_edit.is_published:
                            send_schedule_update_email(faculty_member, {'course': f"{schedule_to_edit.course_code}", 'section': schedule_to_edit.section, 'day': schedule_to_edit.day, 'time': f"{schedule_to_edit.time_start} - {schedule_to_edit.time_end}", 'room': schedule_to_edit.room}, 'updated')
            except Exception as e:
                if str(e) != 'unparseable time':
                    flash(f'Invalid data: {str(e)}', 'error')
            return redirect(url_for('faculty_main.section_schedule_grid', program_id=program_id, section_name=section_name, semester=semester))
        else:
            for f, e in edit_form.errors.items(): flash(f"Edit Error ({f}): {', '.join(e)}", 'error')

    # --- 6. DISPLAY LOGIC ---
    schedules = []
    if current_school_year:
        schedules = db.session.query(TeachingLoad)\
            .join(Course, and_(TeachingLoad.course_code == Course.course_code, Course.program_id == program_id))\
            .filter(TeachingLoad.section == full_section_name, TeachingLoad.school_year == current_school_year, Course.semester == semester)\
            .options(joinedload(TeachingLoad.teacher)).all()

    schedules_by_day = defaultdict(list)
    for s in schedules:
        schedules_by_day[s.day].append(s)

    today_date = datetime.now().date()

    # Rows follow the assigned time frames: a 7:00-10:00 class is one
    # "7:00 - 10:00 AM" row, not three hourly ones. Only genuinely free time is
    # split into hourly rows, so there is still an empty cell to click.
    day_start = datetime.combine(today_date, time(7, 0))
    day_end = datetime.combine(today_date, time(21, 0))

    intervals = []
    for s in schedules:
        s_start = datetime.combine(today_date, s.time_start)
        s_end = datetime.combine(today_date, s.time_end)
        if s_end > s_start:
            intervals.append((s_start, s_end))

    # Merge only classes that actually overlap (they can be on different days).
    # Back-to-back classes like 07:00-10:00 and 10:00-13:00 stay separate, so
    # each one keeps its own row and card.
    busy = []
    for start, end in sorted(intervals):
        if busy and start < busy[-1][1]:
            busy[-1] = (busy[-1][0], max(busy[-1][1], end))
        else:
            busy.append((start, end))

    # Widen the working day when a class starts earlier or runs later.
    grid_start = min([day_start] + [b[0] for b in busy])
    grid_end = max([day_end] + [b[1] for b in busy])

    # A class that ends at the very bottom would leave no empty cell under it,
    # so the whole grid looked like a single row after such an edit. Keep one
    # free hour below the last class.
    if busy and busy[-1][1] >= grid_end:
        grid_end = busy[-1][1] + timedelta(hours=1)

    boundaries = {grid_start, grid_end}
    # Every class start/end is a boundary. When classes overlap on different
    # days the shared time axis needs those cuts, otherwise a card that starts
    # inside a merged block is never drawn.
    for start, end in intervals:
        boundaries.add(start)
        boundaries.add(end)

    # Hourly boundaries, but only inside free stretches.
    cursor_dt = grid_start
    for start, end in busy:
        while cursor_dt + timedelta(minutes=60) <= start:
            cursor_dt += timedelta(minutes=60)
            if cursor_dt < start:
                boundaries.add(cursor_dt)
        boundaries.add(start)
        boundaries.add(end)
        cursor_dt = max(cursor_dt, end)
    while cursor_dt + timedelta(minutes=60) <= grid_end:
        cursor_dt += timedelta(minutes=60)
        if cursor_dt < grid_end:
            boundaries.add(cursor_dt)

    boundaries = sorted(boundaries)
    boundary_index = {dt: i for i, dt in enumerate(boundaries)}

    def _row_label(start_dt, end_dt):
        start_txt = start_dt.strftime('%I:%M').lstrip('0')
        if start_dt.strftime('%p') != end_dt.strftime('%p'):
            start_txt += ' ' + start_dt.strftime('%p')
        return f"{start_txt} - {end_dt.strftime('%I:%M %p').lstrip('0')}"

    schedule_grid_data = []
    for i in range(len(boundaries) - 1):
        row_start, row_end = boundaries[i], boundaries[i + 1]
        row_days = []
        for day in DAYS_OF_WEEK:
            cell = {'schedule': None, 'rowspan': 1, 'skip': False}
            for s in schedules_by_day[day]:
                s_start = datetime.combine(today_date, s.time_start)
                s_end = datetime.combine(today_date, s.time_end)
                if s_start == row_start:
                    # The card spans every row the class covers.
                    cell = {'schedule': s,
                            'rowspan': max(1, boundary_index.get(s_end, i + 1) - i),
                            'skip': False}
                    break
                if s_start < row_start < s_end:
                    # Covered by a card rendered in an earlier row.
                    cell = {'schedule': None, 'rowspan': 1, 'skip': True}
                    break
            row_days.append(cell)

        schedule_grid_data.append({
            'display_time': _row_label(row_start, row_end),
            'form_start': row_start.strftime('%H:%M'),
            'form_end': row_end.strftime('%H:%M'),
            'days': row_days,
        })

    # --- 7. COMPACT PRINT GRID ---
    print_grid_data = []
    if schedules:
        time_points = set()
        for s in schedules:
            time_points.add(_fix_time(s.time_start))
            time_points.add(_fix_time(s.time_end))
        sorted_times = sorted(list(time_points))
        for i in range(len(sorted_times) - 1):
            start = sorted_times[i]
            end = sorted_times[i+1]
            if start.hour < 6: continue
            row_days = []
            has_data = False
            for day in DAYS_OF_WEEK:
                active_sched = None
                for s in schedules:
                    if s.day == day:
                        if _fix_time(s.time_start) <= start and _fix_time(s.time_end) >= end:
                            active_sched = s; break
                row_days.append(active_sched)
                if active_sched: has_data = True
            t_str = f"{start.strftime('%I:%M').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')}"
            print_grid_data.append({'display_time': t_str, 'days': row_days, 'is_gap': not has_data})

    # Restore Choices
    add_form.course.choices = course_choices
    add_form.faculty.choices = faculty_choices
    add_form.room.choices = room_choices
    
    edit_form.course.choices = course_choices
    edit_form.faculty.choices = faculty_choices
    edit_form.room.choices = room_choices

    section_info = { 'program': program.program_name, 'section': section_name, 'semester_display': '1st Semester' if semester == 1 else '2nd Semester' }
    
    # --- 9. DRAFT CHECK (FIXED) ---
    # We now JOIN explicitly on course_code because there is no relationship in models
    has_drafts = db.session.query(TeachingLoad).join(
        Course, TeachingLoad.course_code == Course.course_code
    ).filter(
        TeachingLoad.section == full_section_name,
        TeachingLoad.is_published == False,
        Course.semester == semester
    ).count() > 0

    # Options for the start/end dropdowns, every half hour across the school
    # day. Picking from a list is much faster than spinning the native clock.
    time_options = []
    option_dt = datetime.combine(today_date, time(6, 0))
    while option_dt <= datetime.combine(today_date, time(23, 0)):
        time_options.append((option_dt.strftime('%H:%M'), option_dt.strftime('%I:%M %p').lstrip('0')))
        option_dt += timedelta(minutes=30)

    term_ended, term_ended_message = academic_term_status()

    return render_template('faculty/section_schedule_grid_final.html', 
                           title=f"Scheduling for {full_section_name}", program=program,
                           section_name=section_name, semester=semester,
                           days=DAYS_OF_WEEK, 
                           schedule_grid=schedule_grid_data,
                           print_grid_data=print_grid_data,
                           time_options=time_options,
                           add_form=add_form, edit_form=edit_form, delete_form=delete_form,
                           section_info=section_info,
                           active_school_year=current_school_year,
                           has_drafts=has_drafts,
                           term_ended=term_ended,
                           term_ended_message=term_ended_message)

@main.route('/api/get-courses/<program_id>/sem/<int:semester>')
@login_required
def get_courses(program_id, semester):
    print(f"DEBUG: API CALLED for Program {program_id}, Sem {semester}") # <--- Look for this in your terminal
    
    # 1. Fetch courses sorted by Year then Code
    courses = Course.query.filter_by(program_id=program_id, semester=semester).order_by(Course.year, Course.course_code).all()
    
    # 2. Bucket by Year
    buckets = defaultdict(list)
    for c in courses:
        # Force integer conversion, default to 0 if something is wrong
        try:
            y = int(c.year)
        except:
            y = 0
        buckets[y].append(c)

    # 3. Sort years to ensure order (1, 2, 3...)
    sorted_years = sorted(buckets.keys())
    
    final_list = []

    # 4. Generate Groups with STRICT Mapping
    for year_val in sorted_years:
        # Explicitly map the database value to the label
        if year_val == 1:
            label = "1st Year"
        elif year_val == 2:
            label = "2nd Year"
        elif year_val == 3:
            label = "3rd Year"
        elif year_val == 4:
            label = "4th Year"
        elif year_val >= 5:
            label = f"{year_val}th Year"
        else:
            # Handle 0 or weird numbers
            label = "Unassigned Year"

        # Create the list of courses for this year
        children = []
        for c in buckets[year_val]:
            children.append({
                "id": c.id,
                "text": f"{c.course_code} - {c.course_description}",
                "year": c.year
            })
        
        # Add to final list as a Group
        final_list.append({
            "text": label,
            "children": children
        })
        
    return jsonify({'courses': final_list})

@main.route('/delete-time-slot/<int:slot_id>', methods=['POST'])
@login_required
def delete_time_slot(slot_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))
    slot_to_delete = TimeSlot.query.get_or_404(slot_id)
    if TeachingLoad.query.filter(TeachingLoad.time_start >= slot_to_delete.start_time, TeachingLoad.time_end <= slot_to_delete.end_time).first():
        flash('Cannot delete a time row that contains scheduled classes.', 'error')
    elif slot_to_delete.is_default:
        flash('Cannot delete a default time row.', 'error')
    else:
        db.session.delete(slot_to_delete)
        db.session.commit()
        flash('Custom time row has been deleted.', 'success')
    return redirect(request.referrer or url_for('faculty_main.dashboard'))

@main.route('/my-schedule')
@login_required
def my_schedule():
    if current_user.role != 'faculty':
        flash('This page is for faculty members only.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    request_mode = request.args.get('request_sub', 'false').lower() == 'true'

    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value if school_year_setting else "N/A"
    
    # --- 1. Fetch Data Function ---
    def get_raw_schedules(semester):
        if active_school_year and active_school_year != "N/A":
            return db.session.query(TeachingLoad)\
                .join(Course, TeachingLoad.course_code == Course.course_code)\
                .filter(
                    TeachingLoad.user_id == current_user.id,
                    TeachingLoad.school_year == active_school_year,
                    Course.semester == semester,
                    TeachingLoad.is_published == True
                ).all()
        return []

    # --- 2. Waterfall Grid (For Screen Display) ---
    def generate_waterfall_grid(schedules):
        schedules_by_day = defaultdict(list)
        for s in schedules: schedules_by_day[s.day].append({'schedule': s})

        grid_data = []
        today_date = datetime.now().date()
        current_time = datetime.combine(today_date, datetime.strptime("07:00", "%H:%M").time())
        limit_time = datetime.combine(today_date, datetime.strptime("21:00", "%H:%M").time())
        
        if schedules:
            max_end = max(s.time_end for s in schedules)
            max_end_dt = datetime.combine(today_date, max_end)
            if max_end_dt > limit_time: limit_time = max_end_dt

        while current_time < limit_time:
            row_start = current_time
            next_hard_stop = None
            has_class_starting_now = False
            
            for day in DAYS_OF_WEEK:
                for item in schedules_by_day[day]:
                    s_start = datetime.combine(today_date, item['schedule'].time_start)
                    if s_start == row_start: has_class_starting_now = True
                    elif s_start > row_start: 
                        if next_hard_stop is None or s_start < next_hard_stop: next_hard_stop = s_start

            row_end = row_start + timedelta(minutes=30)
            if not has_class_starting_now and next_hard_stop:
                gap_minutes = (next_hard_stop - row_start).total_seconds() / 60
                if gap_minutes <= 120: row_end = next_hard_stop

            row_days = []
            max_end_found = row_end
            
            for day in DAYS_OF_WEEK:
                found = None
                for item in schedules_by_day[day]:
                    sched_start = datetime.combine(today_date, item['schedule'].time_start)
                    sched_end = datetime.combine(today_date, item['schedule'].time_end)
                    if sched_start == row_start:
                        found = item
                        if sched_end > max_end_found: max_end_found = sched_end
                        break
                row_days.append(found)
            
            row_end = max_end_found
            start_str = row_start.strftime('%I:%M').lstrip('0')
            end_str = row_end.strftime('%I:%M %p').lstrip('0')

            grid_data.append({
                'display_time': f"{start_str} - {end_str}",
                'days': row_days
            })
            current_time = row_end
            
        return grid_data

    # --- 3. Compact Grid (For Printing) ---
    def generate_compact_print_grid(schedules):
        if not schedules: return []
        
        time_points = set()
        
        for s in schedules:
            time_points.add(_fix_time(s.time_start))
            time_points.add(_fix_time(s.time_end))
            
        sorted_times = sorted(list(time_points))
        grid = []
        
        for i in range(len(sorted_times) - 1):
            start = sorted_times[i]
            end = sorted_times[i+1]
            if start.hour < 6: continue 

            row_days = []
            has_data = False
            for day in DAYS_OF_WEEK:
                active_sched = None
                for s in schedules:
                    if s.day == day:
                        if _fix_time(s.time_start) <= start and _fix_time(s.time_end) >= end:
                            active_sched = s; break
                row_days.append(active_sched)
                if active_sched: has_data = True
            
            t_str = f"{start.strftime('%I:%M').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')}"
            grid.append({'display_time': t_str, 'days': row_days, 'is_gap': not has_data})
        return grid

    # Execute Logic
    raw_sem1 = get_raw_schedules(1)
    raw_sem2 = get_raw_schedules(2)

    # Screen Data
    grid_sem1 = generate_waterfall_grid(raw_sem1)
    grid_sem2 = generate_waterfall_grid(raw_sem2)

    # Print Data (New)
    print_sem1 = generate_compact_print_grid(raw_sem1)
    print_sem2 = generate_compact_print_grid(raw_sem2)
                
    return render_template('faculty/my_schedule.html', title='My Schedule', 
                           days=DAYS_OF_WEEK, 
                           grid_sem1=grid_sem1, grid_sem2=grid_sem2,
                           print_sem1=print_sem1, print_sem2=print_sem2, # <--- Passing new variables
                           active_school_year=active_school_year,
                           request_mode=request_mode)


@main.route('/section-schedule-hub', methods=['GET', 'POST'])
@login_required
def manage_section_schedule_hub():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = CreateSectionForm() 
    delete_form = DeleteSectionForm() 
    
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value if school_year_setting else None
    
    # --- 1. HANDLE FORM SUBMISSION ---
    if form.validate_on_submit():
        blocked = term_closed_response()
        if blocked:
            return blocked
        if not active_school_year:
            flash('Cannot create a section because the active school year is not set.', 'error')
            return redirect(url_for('faculty_main.manage_section_schedule_hub'))
        
        program = form.program.data
        name = form.section_name.data.upper().strip()
        semester = int(form.semester.data)
        
        year_digit = None
        section_letter = None
        for ch in name:
            if ch.isdigit() and year_digit is None:
                year_digit = ch
            elif ch.isalpha():
                section_letter = ch
                break
        if not year_digit or not section_letter:
            flash('Section name must include a year digit and a letter (e.g., 1C).', 'error')
            return redirect(url_for('faculty_main.manage_section_schedule_hub', default_program=program.id))
        
        year_level = {'1': '1st Year', '2': '2nd Year', '3': '3rd Year', '4': '4th Year'}.get(year_digit, f'{year_digit}th Year')
        
        existing = Section.query.filter_by(program_id=program.id, name=name, semester=semester, school_year=active_school_year).first()
        
        if existing:
            flash(f'Section "{program.program_code} {name}" already exists.', 'error')
            return redirect(url_for('faculty_main.manage_section_schedule_hub', default_program=program.id))
        else:
            try:
                new_section = Section(
                    program_id=program.id,
                    name=name,
                    year_level=year_level,
                    section_letter=section_letter,
                    semester=semester,
                    school_year=active_school_year,
                    is_active=True
                )
                db.session.add(new_section)
                db.session.commit()
                flash(f'Successfully created section "{program.program_code} {name}".', 'success')
            except Exception:
                db.session.rollback()
                flash(f'Could not create section "{program.program_code} {name}". It may already exist for this year level.', 'error')
        
        return redirect(url_for('faculty_main.manage_section_schedule_hub', default_program=program.id))

    # --- 2. HANDLE PRE-SELECTION ---
    default_program_id = request.args.get('default_program')
    if default_program_id and not form.is_submitted():
        pre_selected_program = Program.query.get(default_program_id)
        if pre_selected_program:
            form.program.data = pre_selected_program

    # --- 3. LOAD DATA FOR DISPLAY ---
    programs_with_sections = []
    if active_school_year:
        # FIX: Natural Sort for the Section List
        # Also include sections created by the admin "Manage Sections" page,
        # which are stored without a school_year (NULL) and follow the
        # "<program_code> <name>" naming convention (e.g. "bscs 1A").
        all_sections_raw = Section.query.filter(
            (Section.school_year == active_school_year) | (Section.school_year.is_(None))
        ).all()
        
        def natural_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s.name)]
            
        all_sections_this_year = sorted(all_sections_raw, key=natural_key)

        sections_by_program = defaultdict(list)
        for sec in all_sections_this_year:
            sections_by_program[sec.program_id].append(sec)
            
        all_programs = Program.query.order_by(Program.program_code).all()
        for program in all_programs:
            program_sections = sections_by_program.get(program.id, [])
            
            sem1_by_year = defaultdict(list)
            sem2_by_year = defaultdict(list)

            for section in program_sections:
                # Normalize admin-style section names ("bscs 1A") for display only;
                # do NOT mutate the ORM object (that would trigger an autoflush).
                section_display_name = section.name
                if section.name and section.program and section.program.program_code and section.name.lower().startswith(section.program.program_code.lower()):
                    section_display_name = section.name[len(section.program.program_code):].strip()
                year = 0
                if section.name and section.name[0].isdigit():
                    try: year = int(section.name[0])
                    except: year = 0
                else:
                    # Handle admin-style names like "bscs 1A" or rely on year_level
                    match = re.search(r'(\d+)', section.name or '')
                    if match:
                        try: year = int(match.group(1))
                        except: year = 0
                    if not year and section.year_level:
                        match = re.search(r'(\d+)', section.year_level or '')
                        if match:
                            try: year = int(match.group(1))
                            except: year = 0

                semester = section.semester if section.semester in (1, 2) else 1

                if year and semester == 1:
                    sem1_by_year[year].append((section, section_display_name))
                elif year and semester == 2:
                    sem2_by_year[year].append((section, section_display_name))

            if program_sections:
                programs_with_sections.append({
                    'program': program, 
                    'sem1_sections_by_year': sem1_by_year, 
                    'sem2_sections_by_year': sem2_by_year
                })

    term_ended, term_ended_message = academic_term_status()

    return render_template('faculty/manage_section_schedule_hub.html', 
                           title='Manage Sections', 
                           form=form,
                           delete_form=delete_form, 
                           programs_with_sections=programs_with_sections,
                           active_school_year=active_school_year,
                           term_ended=term_ended,
                           term_ended_message=term_ended_message)

@main.route('/approvals')
@login_required
def pending_approvals():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    # Two ways an account can await approval: registration left it 'pending',
    # or a legacy grading account was created without edit rights.
    pending_list = User.query.filter(
        db.or_(User.status == 'pending', User.can_edit.is_(False))
    ).order_by(User.full_name).all()
    return render_template('faculty/approvals.html', title='Pending Approvals', pending_list=pending_list)

@main.route('/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_faculty(user_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))
    user_to_approve = User.query.get_or_404(user_id)
    user_to_approve.status = 'active'
    user_to_approve.can_edit = True
    db.session.commit()
    flash(f'Faculty member "{user_to_approve.full_name}" has been approved.', 'success')
    return redirect(url_for('faculty_main.pending_approvals'))

@main.route('/delete-schedule/<int:schedule_id>/sem/<int:semester>', methods=['POST'])
@login_required
def delete_schedule(schedule_id, semester):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))

    blocked = term_closed_response()
    if blocked:
        return blocked

    form = DeleteScheduleForm()
    if form.validate_on_submit():
        schedule_to_delete = TeachingLoad.query.get_or_404(schedule_id)
        faculty_member = schedule_to_delete.teacher
        faculty_id = faculty_member.id

        # --- START: GATHER DETAILS BEFORE DELETING ---
        schedule_details = {
            'course': f"{schedule_to_delete.course_code} - {schedule_to_delete.course_description}",
            'section': schedule_to_delete.section,
            'day': schedule_to_delete.day,
            'time': f"{schedule_to_delete.time_start.strftime('%I:%M %p')} - {schedule_to_delete.time_end.strftime('%I:%M %p')}",
            'room': schedule_to_delete.room
        }
        # --- END: GATHER DETAILS ---
        
        db.session.delete(schedule_to_delete)
        db.session.commit()
        flash('Schedule has been deleted successfully.', 'success')
        
        # --- START: SEND EMAIL NOTIFICATION ---
        send_schedule_update_email(faculty_member, schedule_details, 'deleted')
        # --- END: SEND EMAIL NOTIFICATION ---

        return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=semester))
    else:
        flash('Error: Could not delete schedule. Invalid request.', 'error')
        schedule = TeachingLoad.query.get_or_404(schedule_id)
        return redirect(url_for('faculty_main.schedule_grid', faculty_id=schedule.teacher.id, semester=semester))

@main.route('/profile/<int:user_id>', methods=['GET', 'POST'])
@login_required
def faculty_profile(user_id):
    faculty = User.query.get_or_404(user_id)
    form = EditProfileForm(obj=faculty)

    # 1. Handle Saving the Form (POST)
    if form.validate_on_submit():
        # This saves simple fields like name, email, contact_number
        form.populate_obj(faculty)
        
        # --- FIX START: Manually Save Program ---
        # Because form.major_program doesn't match user.program automatically
        if form.expertise_type.data == 'PROFED':
            if form.major_program.data:
                faculty.program = form.major_program.data
            else:
                # Handle case where PROFED is selected but no program (optional safety)
                pass 
        else:
            # If GENED, clear the program
            faculty.program = None
        # --- FIX END ---

        try:
            db.session.commit()
            flash('Profile updated successfully.', 'success')
            # Redirect to the MAIN profile view (removing ?edit=true)
            return redirect(url_for('faculty_main.faculty_profile', user_id=faculty.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating profile: {str(e)}', 'error')

    # 2. DECIDE WHICH HTML FILE TO SHOW
    if request.args.get('edit') == 'true' or (request.method == 'POST' and not form.validate()):
        # Pre-fill the major_program field if it exists
        if faculty.program:
            form.major_program.data = faculty.program

        return render_template('faculty/edit_profile.html', title='Edit Profile', user=faculty, form=form)

    # 3. Default: Show the View-Only Page
    return render_template('faculty/profile.html', title='Faculty Profile', faculty=faculty)

@main.route('/substitute/new/<int:teaching_load_id>', methods=['GET', 'POST'])
@login_required
def create_substitution_request(teaching_load_id):
    load = TeachingLoad.query.get_or_404(teaching_load_id)
    
    # --- FIX: ALLOW DEANS TO ACCESS ---
    if current_user.role not in ('dean', 'admin', 'superadmin') and load.user_id != current_user.id:
        flash('You do not have permission to request a substitute for this class.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    # Check for existing pending request
    existing_request = SubstitutionRequest.query.filter_by(
        teaching_load_id=load.id,
        status='pending'
    ).first()

    if existing_request:
        return render_template('faculty/view_substitution_status.html', title='Request Status', sub_request=existing_request, load=load)

    course = Course.query.filter_by(course_code=load.course_code).first()
    if not course:
        flash('Error: The course for this schedule could not be found.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = SubstitutionRequestForm()
    if form.validate_on_submit():
        # Double check for duplicates
        if SubstitutionRequest.query.filter_by(teaching_load_id=load.id, date_of_absence=form.date_of_absence.data).first():
             flash('A request for this date already exists.', 'warning')
             return redirect(request.referrer or url_for('faculty_main.dashboard'))

        sub_request = SubstitutionRequest(
            teaching_load_id=load.id, 
            original_teacher_id=load.user_id, # Use the load's teacher, not necessarily current_user
            date_of_absence=form.date_of_absence.data, 
            reason=form.reason.data, 
            status='pending'
        )
        db.session.add(sub_request)
        db.session.commit()
        
        send_substitution_notification_emails(sub_request)

        flash('Substitution request has been submitted.', 'success')
        
        # Redirect based on role
        if can_manage_faculty_system():
            # If Dean/Admin created it, go to the selection page immediately
            return redirect(url_for('faculty_main.select_substitutes', sub_request_id=sub_request.id))
        else:
            return redirect(url_for('faculty_main.my_schedule'))

    return render_template('faculty/create_substitution_request.html', title='Request a Substitute', form=form, load=load, course=course)


@main.route('/substitute/select/<int:sub_request_id>', methods=['GET'])
@login_required
def select_substitutes(sub_request_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    sub_request = SubstitutionRequest.query.get_or_404(sub_request_id)
    load = sub_request.teaching_load
    course = Course.query.filter_by(course_code=load.course_code).first()
    all_potential_substitutes = User.query.filter(User.id != load.user_id, User.role == 'faculty', User.status == 'active').all()
    available_faculty = []
    conflicted_faculty = []
    for faculty in all_potential_substitutes:
        is_qualified = course.course_description in (faculty.subjects_taught or "")
        conflict = TeachingLoad.query.join(Course, TeachingLoad.course_code == Course.course_code).filter(TeachingLoad.user_id == faculty.id, Course.semester == course.semester, TeachingLoad.day == load.day, TeachingLoad.time_start < load.time_end, TeachingLoad.time_end > load.time_start).first()
        if conflict:
            conflicted_faculty.append({'faculty': faculty, 'is_qualified': is_qualified})
        else:
            available_faculty.append({'faculty': faculty, 'is_qualified': is_qualified})
    return render_template('faculty/select_substitutes.html', title='Select Substitutes to Notify', sub_request=sub_request, available_faculty=available_faculty, conflicted_faculty=conflicted_faculty)

@main.route('/substitute/notify/<int:sub_request_id>', methods=['POST'])
@login_required
def send_notifications(sub_request_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    sub_request = SubstitutionRequest.query.get_or_404(sub_request_id)
    load = sub_request.teaching_load
    faculty_ids_to_notify = request.form.getlist('faculty_ids')
    if not faculty_ids_to_notify:
        flash('You did not select any faculty to notify.', 'warning')
        return redirect(url_for('faculty_main.select_substitutes', sub_request_id=sub_request.id))
    for faculty_id in faculty_ids_to_notify:
        message = (f"A substitute is needed for {load.course_code} ({load.course_description}) "
                   f"on {sub_request.date_of_absence.strftime('%B %d, %Y')}. "
                   f"This class is taught by {load.teacher.full_name}.")
        notification = Notification(user_id=faculty_id, substitution_request_id=sub_request.id, message=message)
        db.session.add(notification)
    sub_request.status = 'pending'
    db.session.commit()
    flash(f'Successfully notified {len(faculty_ids_to_notify)} faculty members.', 'success')
    course = Course.query.filter_by(course_code=load.course_code).first()
    return redirect(url_for('faculty_main.schedule_grid', faculty_id=load.user_id, semester=course.semester))

@main.route('/notifications')
@login_required
def view_notifications():
    # FIX: Removed 'is_read=False' so read notifications stay on the list
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
    return render_template('faculty/notifications.html', title='Notifications', notifications=notifications)

@main.route('/notification/read/<int:notification_id>', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id != current_user.id:
        return redirect(url_for('faculty_main.view_notifications'))
    
    notification.is_read = True
    db.session.commit()
    
    # It will reload the page, but the notification will STAY (just styled differently)
    return redirect(url_for('faculty_main.view_notifications'))

@main.route('/notification/delete/<int:notification_id>', methods=['POST'])
@login_required
def delete_notification(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id != current_user.id:
        return redirect(url_for('faculty_main.view_notifications'))
    
    db.session.delete(notification)
    db.session.commit()
    
    flash('Notification removed.', 'success')
    return redirect(url_for('faculty_main.view_notifications'))

@main.route('/notifications/clear-all', methods=['POST'])
@login_required
def clear_all_notifications():
    # FIX: Actually DELETE all notifications for this user
    Notification.query.filter_by(user_id=current_user.id).delete()
    
    db.session.commit()
    flash('All notifications deleted.', 'success')
    return redirect(url_for('faculty_main.view_notifications'))

@main.route('/substitute/accept/<int:notification_id>', methods=['POST'])
@login_required
def accept_substitution(notification_id):
    if current_user.role != 'faculty':
        flash('Only faculty members can accept substitution requests.', 'error')
        return redirect(url_for('faculty_main.view_notifications'))
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id != current_user.id:
        return redirect(url_for('faculty_main.view_notifications'))
    sub_request = notification.substitution_request
    if sub_request.status != 'pending':
        flash('This substitution request is no longer active.', 'warning')
        notification.is_read = True
        db.session.commit()
        return redirect(url_for('faculty_main.view_notifications'))
    sub_request.status = 'accepted'
    sub_request.substitute_teacher_id = current_user.id
    deans = User.query.filter_by(role='dean').all()
    for dean in deans:
        message = (f"{current_user.full_name} has accepted the request to substitute for "
                   f"{sub_request.original_teacher.full_name}'s {sub_request.teaching_load.course_code} class "
                   f"on {sub_request.date_of_absence.strftime('%B %d, %Y')}.")
        dean_notification = Notification(user_id=dean.id, message=message)
        db.session.add(dean_notification)
    other_notifications = Notification.query.filter(Notification.substitution_request_id == sub_request.id, Notification.id != notification.id).all()
    for notif in other_notifications:
        notif.is_read = True
    notification.is_read = True
    db.session.commit()
    flash('You have successfully accepted the substitution. The dean has been notified.', 'success')
    return redirect(url_for('faculty_main.view_notifications'))


def _setting_value(key):
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value.strip() if row and row.value and row.value.strip() else None


def _setting_date(key):
    value = _setting_value(key)
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _set_setting(key, value):
    row = SystemSetting.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.session.add(SystemSetting(key=key, value=str(value)))


def _sync_academic_term(school_year, semester):
    """Sync the faculty System Settings with the admission system's academic_terms table
    and the grading system's active settings."""
    sem_map = {'1': '1st Semester', '2': '2nd Semester'}
    sem_label = sem_map.get(semester, semester)

    existing = db.session.execute(
        text("SELECT id FROM academic_terms WHERE year_name = :y AND semester = :s"),
        {'y': school_year, 's': sem_label}
    ).fetchone()

    if existing:
        term_id = existing[0]
    else:
        result = db.session.execute(
            text("INSERT INTO academic_terms (year_name, semester, is_active, max_units) VALUES (:y, :s, 1, 26)"),
            {'y': school_year, 's': sem_label}
        )
        term_id = result.lastrowid

    db.session.execute(text("UPDATE academic_terms SET is_active = FALSE"))
    db.session.execute(text("UPDATE academic_terms SET is_active = TRUE WHERE id = :tid"), {'tid': term_id})

    # Sync the grading system's active school year + semester (single row in grading_settings)
    grading_row = db.session.execute(text("SELECT id FROM grading_settings LIMIT 1")).fetchone()
    if grading_row:
        db.session.execute(
            text("UPDATE grading_settings SET active_school_year = :y, active_semester = :s WHERE id = :gid"),
            {'y': school_year, 's': sem_label, 'gid': grading_row[0]}
        )
    else:
        db.session.execute(
            text("INSERT INTO grading_settings (active_school_year, active_semester) VALUES (:y, :s)"),
            {'y': school_year, 's': sem_label}
        )


@main.route('/system-settings', methods=['GET', 'POST'])
@login_required
def system_settings():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = SystemSettingsForm()

    # --- 1. THE TERM CURRENTLY IN USE ---
    current_year = _setting_value('school_year')
    if not current_year:
        active_term = db.session.execute(
            text("SELECT year_name FROM academic_terms WHERE is_active = TRUE LIMIT 1")
        ).fetchone()
        grading_row = db.session.execute(
            text("SELECT active_school_year FROM grading_settings LIMIT 1")
        ).fetchone()
        if active_term and active_term[0]:
            current_year = active_term[0]
        elif grading_row and grading_row[0]:
            current_year = grading_row[0]

    current_semester = _setting_value('current_semester')
    if current_semester not in ('1', '2'):
        grading_sem = db.session.execute(
            text("SELECT active_semester FROM grading_settings LIMIT 1")
        ).fetchone()
        current_semester = '1'
        if grading_sem and grading_sem[0]:
            current_semester = '1' if '1st' in grading_sem[0] else '2'

    # --- 2. SCHOOL YEARS AVAILABLE IN THE DROPDOWN ---
    history_list = []
    history_value = _setting_value('school_year_history')
    if history_value:
        try:
            history_list = [str(year) for year in json.loads(history_value) if year]
        except (TypeError, ValueError):
            history_list = []

    for (year,) in db.session.query(TeachingLoad.school_year).distinct().all():
        if year and year not in history_list:
            history_list.append(year)
    if current_year and current_year not in history_list:
        history_list.append(current_year)

    history_list = sorted(set(history_list), reverse=True)
    form.select_year.choices = [(year, year) for year in history_list] or [('', 'No school years yet')]

    # --- 3. SAVE THE ACTIVE TERM ---
    if request.method == 'POST' and not form.validate_on_submit():
        for field_name, messages in form.errors.items():
            label = getattr(form, field_name).label.text
            for message in messages:
                flash(f'{label}: {message}', 'error')
    elif request.method == 'POST':
        selected_year = (form.select_year.data or '').strip()
        new_year = (form.new_year.data or '').strip()
        school_year = new_year or selected_year
        semester = form.current_semester.data
        sy_start = form.active_start_date.data
        sy_end = form.active_end_date.data
        sem_start = form.sem_start_date.data
        sem_end = form.sem_end_date.data

        errors = []
        if not school_year:
            errors.append('Choose a school year or enter a new one.')
        elif new_year and not re.fullmatch(r'\d{4}\s*-\s*\d{4}', new_year):
            errors.append('A new school year must look like 2026-2027.')
        if not all((sy_start, sy_end, sem_start, sem_end)):
            errors.append('Fill in the school year and semester start and end dates.')
        else:
            if sy_start >= sy_end:
                errors.append('The school year end date must be after its start date.')
            if sem_start >= sem_end:
                errors.append('The semester end date must be after its start date.')
            if sem_start < sy_start or sem_end > sy_end:
                errors.append('The semester dates must fall inside the school year.')

        if errors:
            for message in errors:
                flash(message, 'error')
        else:
            school_year = re.sub(r'\s*-\s*', '-', new_year) if new_year else selected_year

            _set_setting('school_year', school_year)
            _set_setting('current_semester', semester)
            _set_setting('school_year_start', sy_start)
            _set_setting('school_year_end', sy_end)
            _set_setting('semester_start_date', sem_start)
            _set_setting('semester_end_date', sem_end)

            if school_year not in history_list:
                history_list.append(school_year)
                history_list.sort(reverse=True)
            _set_setting('school_year_history', json.dumps(history_list))

            _sync_academic_term(school_year, semester)
            db.session.commit()

            semester_label = '1st Semester' if str(semester) == '1' else '2nd Semester'
            flash(f'Academic term set to {school_year} — {semester_label}.', 'success')
            return redirect(url_for('faculty_main.system_settings'))

    # --- 4. PRE-FILL THE FORM (only when opening the page) ---
    if request.method == 'GET':
        if current_year:
            form.select_year.data = (current_year if current_year in history_list
                                     else (history_list[0] if history_list else ''))
        form.current_semester.data = current_semester or '1'
        form.active_start_date.data = _setting_date('school_year_start')
        form.active_end_date.data = _setting_date('school_year_end')
        form.sem_start_date.data = _setting_date('semester_start_date')
        form.sem_end_date.data = _setting_date('semester_end_date')

    sy_start = _setting_date('school_year_start')
    sy_end = _setting_date('school_year_end')
    sem_start = _setting_date('semester_start_date')
    sem_end = _setting_date('semester_end_date')
    today = datetime.now().date()
    term_ended, term_ended_message = academic_term_status()
    semester_label = '1st Semester' if current_semester == '1' else '2nd Semester'

    return render_template('faculty/system_settings.html',
                           title='System Settings',
                           form=form,
                           current_sy=current_year or 'Not Set',
                           current_semester_label=semester_label,
                           sy_start=sy_start, sy_end=sy_end,
                           sem_start=sem_start, sem_end=sem_end,
                           term_status='ended' if term_ended else 'active',
                           term_ended=term_ended,
                           term_ended_message=term_ended_message,
                           today=today)

@main.route('/analytics')
@login_required
def analytics():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    filter_form = UserFilterForm(request.args)
    
    department_choices = [('', 'All Departments'), ('Dean - CCBA', 'Dean - CCBA'), ('Dean - COC', 'Dean - COC')]
    program_depts = db.session.query(Program.program_code).distinct().all()
    department_choices.extend([(dept[0], dept[0]) for dept in program_depts])
    filter_form.department.choices = department_choices

    selected_department = request.args.get('department', default='')
    
    current_semester_setting = SystemSetting.query.filter_by(key='current_semester').first()
    current_semester = int(current_semester_setting.value) if current_semester_setting else 1
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value if school_year_setting else None
    
    if not active_school_year:
        flash("The active school year has not been set. Please set it in System Settings.", 'warning')
        return render_template('faculty/analytics.html', title='Faculty Schedule Analytics', 
                               current_semester=current_semester, faculty_workload=[], 
                               room_utilization=[], chart_data={'faculty_labels':[],'faculty_unit_counts':[],'room_labels':[],'room_class_counts':[]}, 
                               filter_form=filter_form, selected_department=selected_department,
                               overloaded_faculty=[], underloaded_faculty=[])

    # --- Definitive Faculty Workload Query ---
    distinct_courses = db.session.query(
        Course.course_code, Course.units, Course.program_id
    ).filter(Course.semester == current_semester).distinct(Course.course_code).subquery()

    faculty_load_query = db.session.query(
        User,
        func.count(TeachingLoad.id).label('class_count'),
        func.sum(distinct_courses.c.units).label('unit_sum')
    ).select_from(TeachingLoad)\
     .join(User, TeachingLoad.user_id == User.id)\
     .join(distinct_courses, TeachingLoad.course_code == distinct_courses.c.course_code)\
     .join(Program, distinct_courses.c.program_id == Program.id)\
     .filter(
        TeachingLoad.school_year == active_school_year,
        TeachingLoad.is_published == True,
        User.role == 'faculty',
        User.status == 'active'
     )

    program_codes_to_query = []
    if selected_department:
        if selected_department in DEPARTMENT_MAP:
            program_codes_to_query = DEPARTMENT_MAP[selected_department]
        else:
            program_codes_to_query = [selected_department]

    if program_codes_to_query:
        faculty_load_query = faculty_load_query.filter(Program.program_code.in_(program_codes_to_query))

    faculty_loads = faculty_load_query.group_by(User.id).order_by(User.full_name).all()
    
    faculty_workload = []
    MAX_UNITS = 30.0
    for faculty, class_count, unit_sum in faculty_loads:
        units = int(unit_sum) if unit_sum is not None else 0
        percentage = min((units / MAX_UNITS) * 100, 100)
        status_color = 'bg-green-500'
        if units > MAX_UNITS: status_color = 'bg-red-500'
        elif percentage >= 90: status_color = 'bg-yellow-500'
        faculty_workload.append({
            'faculty': faculty, 'class_count': class_count or 0, 'unit_sum': units,
            'load_percentage': percentage, 'status_color': status_color, 'is_overloaded': units > MAX_UNITS
        })

    UNDERLOAD_THRESHOLD = 15
    overloaded_faculty = [item for item in faculty_workload if item['is_overloaded']]
    underloaded_faculty = [item for item in faculty_workload if item['unit_sum'] > 0 and item['unit_sum'] < UNDERLOAD_THRESHOLD]

    # --- Room Utilization Query ---
    room_usage_query = db.session.query(TeachingLoad.room, func.count(TeachingLoad.id).label('count'))\
                            .join(Course, TeachingLoad.course_code == Course.course_code)\
                            .join(Program, Course.program_id == Program.id)\
                            .filter(TeachingLoad.room != None, 
                                    TeachingLoad.room != '',
                                    Course.semester == current_semester, 
                                    TeachingLoad.school_year == active_school_year,
                                    TeachingLoad.is_published == True)
    if program_codes_to_query:
        room_usage_query = room_usage_query.filter(Program.program_code.in_(program_codes_to_query))
        

    room_utilization_tuples = room_usage_query.group_by(TeachingLoad.room).order_by(func.count(TeachingLoad.id).desc()).all()
    

    room_utilization = [{'room': room, 'count': count} for room, count in room_utilization_tuples]
    
    faculty_workload_sorted_for_chart = sorted(faculty_workload, key=lambda x: x['unit_sum'], reverse=True)
    

    chart_data = {
        'faculty_labels': [item['faculty'].full_name for item in faculty_workload_sorted_for_chart],
        'faculty_unit_counts': [item['unit_sum'] for item in faculty_workload_sorted_for_chart],
        'room_labels': [item['room'] for item in room_utilization], # FIX: Removed "Room " prefix
        'room_class_counts': [item['count'] for item in room_utilization]
    }

    filter_form.department.data = selected_department

    return render_template('faculty/analytics.html', title='Faculty Schedule Analytics', 
                           current_semester=current_semester, faculty_workload=faculty_workload, 
                           room_utilization=room_utilization, chart_data=chart_data, 
                           filter_form=filter_form, selected_department=selected_department,
                           overloaded_faculty=overloaded_faculty, underloaded_faculty=underloaded_faculty)

@main.route('/absence-reports')
@login_required
def absence_reports_list():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    # 1. Get Active School Year
    sy_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_sy = sy_setting.value if sy_setting else None

    # 2. Base Query (Filter by School Year)
    query = SubstitutionRequest.query.join(TeachingLoad).filter(
        TeachingLoad.school_year == active_sy
    )
    
    # Get all records for processing
    all_reports = query.order_by(SubstitutionRequest.timestamp.desc()).all()

    # --- 3. CALCULATE KPIS ---
    total_requests = len(all_reports)
    
    # Count Successful (Accepted or Completed)
    covered_count = sum(1 for r in all_reports if r.status in ['accepted', 'completed'])
    sub_rate = round((covered_count / total_requests * 100), 1) if total_requests > 0 else 0
    
    pending_count = sum(1 for r in all_reports if r.status == 'pending')

    # Calculate Peak Day (e.g., "Monday")
    day_counts = defaultdict(int)
    for r in all_reports:
        day_name = r.date_of_absence.strftime('%A')
        day_counts[day_name] += 1
    peak_day = max(day_counts, key=day_counts.get) if day_counts else "N/A"


    # --- 4. LEADERBOARDS ---
    
    # Top Substitutes (The Heroes)
    sub_counts = defaultdict(int)
    for r in all_reports:
        if r.substitute_teacher:
            sub_counts[r.substitute_teacher.full_name] += 1
    
    # Sort and take Top 5
    top_substitutes = sorted(sub_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Top Requesters (Neutral renaming)
    req_counts = defaultdict(int)
    for r in all_reports:
        req_counts[r.original_teacher.full_name] += 1
    
    top_requesters = sorted(req_counts.items(), key=lambda x: x[1], reverse=True)[:5]


    # --- 5. CHART DATA (Monthly Trend) ---
    # Initialize current academic months (approximate)
    month_data = defaultdict(int)
    for r in all_reports:
        month_key = r.date_of_absence.strftime('%b') # e.g., "Jan", "Feb"
        month_data[month_key] += 1
    
    # Prepare data for Chart.js
    chart_labels = list(month_data.keys())
    chart_values = list(month_data.values())

    chart_payload = {
        'labels': chart_labels,
        'data': chart_values,
        'top_subs_labels': [x[0] for x in top_substitutes],
        'top_subs_data': [x[1] for x in top_substitutes],
        'top_req_labels': [x[0] for x in top_requesters],
        'top_req_data': [x[1] for x in top_requesters]
    }

    return render_template('faculty/absence_reports.html', 
                           title='Absence & Substitution Reports', 
                           reports=all_reports,
                           total_requests=total_requests,
                           sub_rate=sub_rate,
                           pending_count=pending_count,
                           peak_day=peak_day,
                           chart_data=chart_payload)


@main.route('/absence-report/complete/<int:report_id>', methods=['POST'])
@login_required
def mark_report_completed(report_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
        
    report = SubstitutionRequest.query.get_or_404(report_id)
    report.status = 'completed'
    db.session.commit()
    
    flash('Report has been marked as completed.', 'success')
    return redirect(url_for('faculty_main.absence_reports_list'))

@main.route('/schedule-for/<int:faculty_id>')
@login_required
def schedule_dispatcher(faculty_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    # Get the active semester from the database setting
    current_semester_setting = SystemSetting.query.filter_by(key='current_semester').first()
    active_semester = int(current_semester_setting.value) if current_semester_setting else 1
    
    # Redirect to the specific schedule grid for the active semester
    return redirect(url_for('faculty_main.schedule_grid', faculty_id=faculty_id, semester=active_semester))

def _get_schedule_data(faculty_list):
    """
    Helper function to fetch and structure schedule data for printing.
    """
    all_faculty_data = []
    
    FIXED_TIME_SLOTS = [
        ("7:30", "9:00", time(7, 30), time(9, 0)),
        ("9:00", "10:30", time(9, 0), time(10, 30)),
        ("10:45", "12:15", time(10, 45), time(12, 15)),
        ("1:00", "2:30", time(13, 0), time(14, 30)),
        ("2:30", "4:00", time(14, 30), time(16, 0)),
        ("4:00", "5:30", time(16, 0), time(17, 30)),
    ]
    

    # Get the currently active school year and semester from system settings
    current_semester_setting = SystemSetting.query.filter_by(key='current_semester').first()
    current_semester = int(current_semester_setting.value) if current_semester_setting else 1
    
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_school_year = school_year_setting.value if school_year_setting else None


    for faculty in faculty_list:
        # If no school year is active, don't query for schedules
        if not active_school_year:
            faculty_schedules = []
        else:

            # The query now filters by the active_school_year as well
            faculty_schedules = db.session.query(TeachingLoad).join(
                Course, TeachingLoad.course_code == Course.course_code
            ).filter(
                TeachingLoad.user_id == faculty.id,
                Course.semester == current_semester,
                TeachingLoad.school_year == active_school_year  
            ).all()


        schedule_grid_data = []
        for slot_start_str, slot_end_str, slot_start_obj, slot_end_obj in FIXED_TIME_SLOTS:
            row_data = {
                'time_label': f"{slot_start_str} - {slot_end_str}",
                'days': []
            }
            for day in DAYS_OF_WEEK:
                found_schedule_data = None
                for schedule in faculty_schedules:
                    if schedule.day == day and (schedule.time_start < slot_end_obj and schedule.time_end > slot_start_obj):
                        
                        is_non_standard = (schedule.time_start != slot_start_obj or schedule.time_end != slot_end_obj)

                        # This time formatting is complex; let's simplify for cross-platform compatibility
                        start_time_f = schedule.time_start.strftime('%I:%M').lstrip('0')
                        end_time_f = schedule.time_end.strftime('%I:%M').lstrip('0')
                        
                        found_schedule_data = {
                            'schedule': schedule,
                            'is_non_standard': is_non_standard,
                            'time_display': f"({start_time_f}-{end_time_f})"
                        }
                        break
                row_data['days'].append(found_schedule_data)
            
            schedule_grid_data.append(row_data)
        
        if faculty_schedules:
            all_faculty_data.append({
                'faculty_name': _format_faculty_name(faculty.full_name),
                'schedule_grid': schedule_grid_data,
            })
            
    return all_faculty_data

def _format_faculty_name(full_name):
    """Formats 'First M Last' to 'LAST, FIRST M.'"""
    parts = full_name.split()
    if len(parts) >= 2:
        last_name = parts[-1]
        first_name = parts[0]
        middle_initial = ""
        if len(parts) > 2:
            # Handle multiple middle names/initials
            middle_initial = " ".join([p[0] + "." for p in parts[1:-1]])
        return f"{last_name.upper()}, {first_name.upper()} {middle_initial}".strip()
    return full_name.upper()

@main.route('/schedule/print/<int:faculty_id>')
@login_required
def print_faculty_schedule(faculty_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    faculty_member = User.query.get_or_404(faculty_id) 
    faculty_data = _get_schedule_data([faculty_member]) # Pass as a list
    
    return render_template('faculty/print_schedule.html', all_faculty_data=faculty_data)

@main.route('/scheduling/print-all')
@login_required
def print_all_schedules():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    

    # Get the selected department from the URL's query parameter
    selected_department = request.args.get('department', default='')

    # Determine the display name for the header
    department_name = selected_department if selected_department else "All Departments"


    faculty_departments_to_query = []
    if selected_department:
        if selected_department in DEPARTMENT_MAP:
            faculty_departments_to_query = [selected_department]
        else:
            # This handles both specific program codes and dean departments
            faculty_departments_to_query = [selected_department]
            

    faculty_query = User.query.filter_by(role='faculty', status='active')
    if faculty_departments_to_query:
        
        faculty_query = faculty_query.filter(User.department.in_(faculty_departments_to_query))
    
    faculty_list = faculty_query.order_by(User.full_name).all()
    all_faculty_data = _get_schedule_data(faculty_list)

    return render_template('faculty/print_schedule.html', all_faculty_data=all_faculty_data, department_name=department_name)

@main.route('/manage-programs', methods=['GET', 'POST'])
@login_required
def manage_programs():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    program_form = ProgramForm()
    upload_form = FileUploadForm()

    # --- Logic for Adding a New Program ---
    if program_form.validate_on_submit() and program_form.submit.data:
        existing_program = Program.query.filter_by(program_code=program_form.program_code.data).first()
        if existing_program:
            flash('A program with this code already exists.', 'error')
        else:
            new_program = Program(program_code=program_form.program_code.data, program_name=program_form.program_name.data)
            db.session.add(new_program)
            db.session.commit()
            flash('New program has been added successfully.', 'success')
        return redirect(url_for('faculty_main.manage_programs'))

    # --- Logic for Importing a Curriculum File ---
    if upload_form.validate_on_submit() and upload_form.submit.data:
        file = upload_form.file.data
        try:
            df = pd.read_excel(file)
            required_columns = ['PROGRAM_CODE', 'YEAR', 'SEMESTER', 'COURSE_CODE', 'COURSE_DESCRIPTION', 'UNITS']
            if not all(col in df.columns for col in required_columns):
                flash('The uploaded Excel file is missing one or more required columns. Please use the correct template.', 'error')
                return redirect(url_for('faculty_main.manage_programs'))

            courses_added = 0
            for index, row in df.iterrows():
                prog_key = str(row['PROGRAM_CODE']).strip()
                # Match by program_code OR program_id (case-insensitive) so programs
                # created by either the admission or the faculty admin resolve to the same row.
                program = Program.query.filter(
                    db.or_(
                        db.func.lower(Program.program_code) == prog_key.lower(),
                        db.func.lower(Program.id) == prog_key.lower()
                    )
                ).first()
                if not program:
                    flash(f"Error on row {index + 2}: Program '{prog_key}' not found. Please add it on the 'Manage Programs' page first.", 'error')
                    continue

                existing_course = Course.query.filter_by(program_id=program.id, course_code=row['COURSE_CODE']).first()
                if existing_course:
                    continue

                new_course = Course(
                    program_id=program.id, year=row['YEAR'], semester=row['SEMESTER'],
                    course_code=row['COURSE_CODE'], course_description=row['COURSE_DESCRIPTION'],
                    units=row['UNITS']
                )
                db.session.add(new_course)
                courses_added += 1

                # SYNC to the admission system's tables so both admins see the same curriculum.
                course_desc = str(row['COURSE_DESCRIPTION']).strip()
                course_code = str(row['COURSE_CODE']).strip().upper()

                # 1. program_courses -> shown on the admission program form & website program page
                if not ProgramCourse.query.filter_by(program_id=program.id, course_code=course_code).first():
                    db.session.add(ProgramCourse(
                        program_id=program.id,
                        course_code=course_code,
                        course_name=str(row['COURSE_NAME']).strip() if 'COURSE_NAME' in row else course_desc,
                        course_description=course_desc,
                    ))

                # 2. subjects -> used by admission enrollment / grades / Manage Subjects
                if not Subject.query.filter_by(subject_code=course_code).first() and not Subject.query.filter_by(program_id=program.id, subject_title=course_desc).first():
                    db.session.add(Subject(
                        program_id=program.id,
                        subject_code=course_code,
                        subject_title=course_desc,
                        units=int(float(row['UNITS'])),
                        semester=int(row['SEMESTER']),
                        year_level=int(row['YEAR']),
                    ))

            db.session.commit()
            flash(f'Successfully imported {courses_added} new courses.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred while processing the file: {e}', 'error')
        return redirect(url_for('faculty_main.manage_programs'))
            
    programs = Program.query.order_by(Program.program_code).all()
    # Pass both forms to the template
    return render_template('faculty/program_management.html', title='Manage Programs', programs=programs, form=program_form, upload_form=upload_form)
            
    programs = Program.query.order_by(Program.program_code).all()
    return render_template('faculty/program_management.html', title='Manage Programs', programs=programs, form=form)

@main.route('/edit-program/<program_id>', methods=['GET', 'POST'])
@login_required
def edit_program(program_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))
        
    program = Program.query.get_or_404(program_id)
    form = ProgramForm(obj=program)
    
    if form.validate_on_submit():
        # Check if the new program code conflicts with another existing program
        existing_program = Program.query.filter(Program.program_code == form.program_code.data, Program.id != program_id).first()
        if existing_program:
            flash('Another program with this code already exists.', 'error')
        else:
            program.program_code = form.program_code.data
            program.program_name = form.program_name.data
            db.session.commit()
            flash('Program has been updated successfully.', 'success')
            return redirect(url_for('faculty_main.manage_programs'))
            
    return render_template('faculty/edit_program.html', title='Edit Program', form=form, program=program)

@main.route('/delete-program/<program_id>', methods=['POST'])
@login_required
def delete_program(program_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))
        
    program = Program.query.get_or_404(program_id)
    
    # Safety check: Do not allow deletion if courses are associated with this program
    if program.courses.first():
        flash('Cannot delete this program because it has courses associated with it. Please remove the courses first.', 'error')
    else:
        db.session.delete(program)
        db.session.commit()
        flash('Program has been deleted successfully.', 'success')
        
    return redirect(url_for('faculty_main.manage_programs'))


@main.route('/program/<program_id>/courses', methods=['GET', 'POST'])
@login_required
def manage_program_courses(program_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    program = Program.query.get_or_404(program_id)
    form = CourseForm()

    if form.validate_on_submit():
        # Check if a course with the same code already exists for this program
        existing_course = Course.query.filter_by(program_id=program.id, course_code=form.course_code.data).first()
        if existing_course:
            flash(f"A course with the code '{form.course_code.data}' already exists for this program.", 'error')
        else:
            new_course = Course(
                program_id=program.id,
                year=form.year.data,
                semester=form.semester.data,
                course_code=form.course_code.data.upper(),
                course_description=form.course_description.data,
                units=form.units.data
            )
            db.session.add(new_course)
            db.session.commit()
            flash(f"Successfully added course '{new_course.course_code}'.", 'success')
        return redirect(url_for('faculty_main.manage_program_courses', program_id=program_id))

    courses = program.courses.order_by(Course.year, Course.semester, Course.course_code).all()
    
    return render_template('faculty/manage_courses.html', 
                           title=f"Courses for {program.program_code}", 
                           program=program, 
                           courses=courses,
                           form=form)

@main.route('/delete-course/<int:course_id>', methods=['POST'])
@login_required
def delete_course(course_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
        
    course = Course.query.get_or_404(course_id)
    program_id = course.program_id # Save the program_id before deleting

    # Safety Check: Do not delete a course if it's currently in a teaching load
    if TeachingLoad.query.filter_by(course_code=course.course_code).first():
        flash(f"Cannot delete '{course.course_code}' because it is currently assigned to a faculty member's schedule.", 'error')
        return redirect(url_for('faculty_main.manage_program_courses', program_id=program_id))
    
    db.session.delete(course)
    db.session.commit()
    flash(f"Course '{course.course_code}' has been deleted successfully.", 'success')
    
    return redirect(url_for('faculty_main.manage_program_courses', program_id=program_id))

@main.route('/school-year-analytics', methods=['GET', 'POST'])
@login_required
def school_year_analytics():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = SchoolYearAnalyticsForm()

    # --- 1. GET ACTIVE SCHOOL YEAR ---
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_global_year = school_year_setting.value if school_year_setting else None

    # --- 2. GET YEARS FROM SCHEDULE DATA ---
    existing_years_query = db.session.query(TeachingLoad.school_year).distinct().all()
    available_years = {sy[0] for sy in existing_years_query if sy[0]}
    
    # --- 3. GET YEARS FROM HISTORY (This fixes your issue) ---
    history_setting = SystemSetting.query.filter_by(key='school_year_history').first()
    if history_setting:
        try:
            history_list = json.loads(history_setting.value)
            for h_year in history_list:
                available_years.add(h_year)
        except:
            pass # If JSON is corrupt, just ignore it

    # --- 4. COMBINE AND SORT ---
    if active_global_year:
        available_years.add(active_global_year)
    
    sorted_years = sorted(list(available_years), reverse=True)
    form.school_year.choices = [(y, y) for y in sorted_years]

    analytics_data = None
    selected_school_year = None

    # --- 5. HANDLE SELECTION ---
    # The report form submits as GET (?school_year=...) so it never depends on
    # a CSRF token staying in sync with the session. POST is still accepted for
    # older bookmarks/clients.
    if request.method == 'POST':
        if form.validate_on_submit():
            selected_school_year = form.school_year.data
        else:
            # Never fail silently: surface the errors and still honour the
            # submitted year when it is one we actually offer.
            for field, errors in form.errors.items():
                for err in errors:
                    flash(f'{field}: {err}', 'error')
            submitted_year = (request.form.get('school_year') or '').strip()
            if submitted_year in dict(form.school_year.choices):
                selected_school_year = submitted_year
    else:
        requested_year = (request.args.get('school_year') or '').strip()
        if requested_year in dict(form.school_year.choices):
            selected_school_year = requested_year
        elif active_global_year:
            selected_school_year = active_global_year

    if selected_school_year:
        form.school_year.data = selected_school_year

    # --- 6. GENERATE REPORT ---
    if selected_school_year:
        
        faculty_workload = {}
        MAX_UNITS = 30.0
        
        for sem in [1, 2]:
            course_subquery = db.session.query(Course.course_code, func.avg(Course.units).label('units'))\
                                        .filter(Course.semester == sem).group_by(Course.course_code).subquery()

            loads = db.session.query(
                User.full_name, func.sum(course_subquery.c.units).label('total_units')
            ).join(TeachingLoad, User.id == TeachingLoad.user_id)\
             .join(course_subquery, TeachingLoad.course_code == course_subquery.c.course_code)\
             .filter(TeachingLoad.school_year == selected_school_year, TeachingLoad.is_published == True, User.role == 'faculty')\
             .group_by(User.id).all()

            for full_name, total_units in loads:
                if full_name not in faculty_workload:
                    faculty_workload[full_name] = {'sem1': 0, 'sem2': 0, 'total': 0}
                
                units = int(total_units) if total_units is not None else 0
                faculty_workload[full_name][f'sem{sem}'] = units
        
        for name, data in faculty_workload.items():
            total = data['sem1'] + data['sem2']
            data['total'] = total
            higher_load = max(data['sem1'], data['sem2'])
            percentage = min((higher_load / MAX_UNITS) * 100, 100)
            status_color = 'bg-green-500'
            if higher_load > MAX_UNITS: status_color = 'bg-red-500'
            elif percentage >= 90: status_color = 'bg-yellow-500'
            data['percentage'] = percentage
            data['status_color'] = status_color

        program_stats = db.session.query(
            Program.program_name,
            func.count(TeachingLoad.id).label('class_count')
        ).join(Course, Program.id == Course.program_id)\
         .join(TeachingLoad, Course.course_code == TeachingLoad.course_code)\
         .filter(TeachingLoad.school_year == selected_school_year, TeachingLoad.is_published == True)\
         .group_by(Program.id).order_by(func.count(TeachingLoad.id).desc()).all()

        analytics_data = {
            'faculty_workload': sorted(faculty_workload.items(), key=lambda item: item[1]['total'], reverse=True),
            'program_stats': program_stats
        }

    return render_template('faculty/school_year_analytics.html', title='School Year Analytics', 
                           form=form, analytics_data=analytics_data, school_year=selected_school_year)

@main.route('/my-analytics')
@login_required
def my_analytics():
    if current_user.role != 'faculty':
        flash('This page is for faculty members only.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    # Query all teaching loads for the current user across all time
    all_loads = db.session.query(
        TeachingLoad.school_year,
        Course.semester,
        func.count(TeachingLoad.id).label('class_count'),
        func.sum(Course.units).label('total_units')
    ).join(Course, TeachingLoad.course_code == Course.course_code)\
     .filter(TeachingLoad.user_id == current_user.id,TeachingLoad.is_published == True)\
     .group_by(TeachingLoad.school_year, Course.semester)\
     .order_by(TeachingLoad.school_year.desc(), Course.semester.asc())\
     .all()
    
    # Process the data into a nested dictionary structure: {school_year: {sem1: data, sem2: data}}
    workload_by_sy = defaultdict(lambda: {
        1: {'classes': 0, 'units': 0},
        2: {'classes': 0, 'units': 0}
    })

    for school_year, semester, class_count, total_units in all_loads:
        if school_year: # Ensure school_year is not None
            workload_by_sy[school_year][semester]['classes'] = class_count
            workload_by_sy[school_year][semester]['units'] = int(total_units) if total_units else 0

    return render_template('faculty/my_analytics.html', title='My Analytics', workload_by_sy=workload_by_sy)

@main.route('/request-substitution')
@login_required
def request_substitution():
    if current_user.role != 'faculty':
        return redirect(url_for('faculty_main.dashboard'))
    
    # This route now simply redirects to the schedule page in "request mode"
    return redirect(url_for('faculty_main.my_schedule', request_sub='true'))

@main.route('/substitution/accept/<int:request_id>')
@login_required
def accept_substitution_request(request_id):
    if current_user.role != 'faculty':
        flash('Only faculty members can accept substitution requests.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    sub_request = SubstitutionRequest.query.get_or_404(request_id)

    if sub_request.status != 'pending':
        flash('This substitution request is no longer available.', 'warning')
        return redirect(url_for('faculty_main.dashboard'))
    
    if sub_request.original_teacher_id == current_user.id:
        flash('You cannot accept your own substitution request.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    # Update the request
    sub_request.status = 'accepted'
    sub_request.substitute_teacher_id = current_user.id
    db.session.commit()


    flash(f'You have successfully accepted the request to substitute for {sub_request.original_teacher.full_name}. Thank you!', 'success')
    return redirect(url_for('faculty_main.my_schedule'))

@main.route('/section-schedule/delete/<int:schedule_id>', methods=['POST'])
@login_required
def delete_section_schedule(schedule_id):
    """
    Deletes a schedule and ensures redirection back to the correct Section Grid
    by parsing the Section Name instead of the Course Program.
    """
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    blocked = term_closed_response()
    if blocked:
        return blocked

    delete_form = DeleteScheduleForm()
    if delete_form.validate_on_submit():
        schedule_to_delete = TeachingLoad.query.get_or_404(schedule_id)

        # 1. Capture the Full Section String (e.g., "BSCS 1A")
        full_section_name = schedule_to_delete.section
        
        # 2. Capture Semester (fallback to 1 if missing)
        course = Course.query.filter_by(course_code=schedule_to_delete.course_code).first()
        semester = course.semester if course else 1 

        # 3. Determine the correct Program based on the Section Name prefix
        # This ensures that deleting a GENED course still returns you to the BSCS grid.
        target_program = None
        target_section_name = ""
        
        all_programs = Program.query.all()
        # Sort by length descending to match "BSCS" before "BS" if names overlap
        all_programs.sort(key=lambda p: len(p.program_code), reverse=True)
        
        for prog in all_programs:
            # Check if "BSCS 1A" starts with "BSCS"
            if full_section_name.startswith(prog.program_code):
                target_program = prog
                # Remove the program code to get just "1A"
                target_section_name = full_section_name[len(prog.program_code):].strip()
                break
        
        # 4. Delete the schedule
        db.session.delete(schedule_to_delete)
        db.session.commit()
        flash('Schedule has been deleted successfully.', 'success')

        # 5. Redirect
        if target_program and target_section_name:
            return redirect(url_for('faculty_main.section_schedule_grid', 
                                    program_id=target_program.id, 
                                    section_name=target_section_name, 
                                    semester=semester))
        else:
            # Fallback if parsing fails
            flash('Could not determine section details for redirection. Returned to Hub.', 'warning')
            return redirect(url_for('faculty_main.manage_section_schedule_hub'))

    else:
        flash('Invalid delete request.', 'error')
        return redirect(url_for('faculty_main.manage_section_schedule_hub'))

@main.route('/section/delete/<int:section_id>', methods=['POST'])
@login_required
def delete_section(section_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to access this page.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    section_to_delete = Section.query.get_or_404(section_id)
    
    # SAFETY CHECK: Do not allow deletion if the section has classes scheduled.
    full_section_name = f"{section_to_delete.program.program_code} {section_to_delete.name}"
    has_loads = TeachingLoad.query.filter_by(
        section=full_section_name,
        school_year=section_to_delete.school_year,
    ).join(Course, TeachingLoad.course_code == Course.course_code)\
     .filter(Course.semester == section_to_delete.semester)\
     .first()

    if has_loads:
        flash(f'Cannot delete section "{full_section_name}" because it has classes scheduled. Please remove them from the schedule grid first.', 'error')
    elif Applicant.query.filter_by(section_id=section_to_delete.id).count() > 0:
        flash(f'Cannot delete section "{full_section_name}" because it still has students assigned. Move them to another section first.', 'error')
    else:
        try:
            # faculty_assignments.section_id is NOT NULL, so the assignments cannot
            # simply be detached. Deleting them explicitly avoids the IntegrityError
            # ("Column 'section_id' cannot be null") that SQLAlchemy throws when it
            # tries to NULL the FK while flushing the section delete.
            FacultyAssignment.query.filter_by(section_id=section_to_delete.id).delete(synchronize_session=False)
            db.session.delete(section_to_delete)
            db.session.commit()
            flash(f'Section "{full_section_name}" has been deleted successfully.', 'success')
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Failed to delete section %s', section_id)
            flash(f'Could not delete section "{full_section_name}": {e}', 'error')

    return redirect(url_for('faculty_main.manage_section_schedule_hub'))

@main.route('/download-curriculum-template')
@login_required
def download_template():
    """Serves the pre-made curriculum Excel template file."""
    # The 'static' directory is the root. We specify the subdirectory 'downloads'.
    try:
        return send_from_directory(
            'static/downloads', 
            'curriculum_template.xlsx', 
            as_attachment=True
        )
    except FileNotFoundError:
        flash('Error: The template file could not be found on the server.', 'error')
        return redirect(url_for('faculty_main.manage_programs'))
    
@main.route('/api/get-sections/<program_id>/sem/<int:semester>')
@login_required
def get_sections(program_id, semester):
    # 1. Query ALL sections for this program and semester
    all_program_sections = Section.query.filter_by(program_id=program_id, semester=semester).all()
    
    valid_sections = []
    
    for s in all_program_sections:
        # Extract Year Level from Name (e.g. "BSCS 2A" -> 2, "1B" -> 1)
        # We look for the first digit in the name
        year_level = 0
        import re
        match = re.search(r'\d+', s.name)
        if match:
            year_level = int(match.group())

        valid_sections.append({
            'id': s.name, 
            'text': s.name,
            'year': year_level # <--- Sending Year to Frontend
        })

    # 2. Natural Sort
    try:
        valid_sections.sort(key=lambda s: _natural_sort_key(s['text']))
    except:
        valid_sections.sort(key=lambda x: x['text'])
    
    return jsonify({'sections': valid_sections})

@main.route('/manage-rooms', methods=['GET', 'POST'])
@login_required
def manage_rooms():
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('Access denied.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = RoomForm()
    if form.validate_on_submit():
        # Check if exists (Case insensitive check)
        existing = Room.query.filter(func.lower(Room.room_name) == func.lower(form.room_name.data)).first()
        if existing:
            flash('That room already exists.', 'error')
        else:
            new_room = Room(room_name=form.room_name.data)
            db.session.add(new_room)
            db.session.commit()
            flash('Room added successfully.', 'success')
        return redirect(url_for('faculty_main.manage_rooms'))

    # --- FIX: NATURAL SORTING LOGIC ---
    # 1. Fetch all rooms unsorted
    rooms_raw = Room.query.all()

    # 2. Sort the list naturally (e.g., "Room 10" after "Room 9")
    rooms = sorted(rooms_raw, key=lambda r: _natural_sort_key(r.room_name))
    # ----------------------------------

    return render_template('faculty/manage_rooms.html', title='Manage Rooms', form=form, rooms=rooms)

@main.route('/delete-room/<int:room_id>', methods=['POST'])
@login_required
def delete_room(room_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('Access denied.', 'error')
        return redirect(url_for('faculty_main.dashboard'))
    
    room = Room.query.get_or_404(room_id)


    usage_check = TeachingLoad.query.filter_by(room=room.room_name).first()

    if usage_check:
        flash(f"Cannot delete '{room.room_name}' because it is currently assigned to a class schedule.", 'error')
        return redirect(url_for('faculty_main.manage_rooms'))
    # ------------------------------------------------------

    db.session.delete(room)
    db.session.commit()
    flash('Room deleted successfully.', 'success')
    return redirect(url_for('faculty_main.manage_rooms'))


@main.route('/reset-slots')
@login_required
def reset_time_slots():
    if current_user.role not in ('dean', 'admin', 'superadmin'): return "Unauthorized"
    
    # 1. Clear existing slots
    TimeSlot.query.delete()
    db.session.commit()
    
    # 2. Generate 30-min intervals from 7:00 AM to 9:00 PM
    start_time = datetime.strptime("07:00", "%H:%M")
    end_limit = datetime.strptime("21:00", "%H:%M")
    
    current = start_time
    while current < end_limit:
        next_time = current + timedelta(minutes=30)
        # Create slot
        new_slot = TimeSlot(start_time=current.time(), end_time=next_time.time(), is_default=True)
        db.session.add(new_slot)
        current = next_time
        
    db.session.commit()
    return "Time Slots Reset to 30-minute intervals! <a href='/dashboard'>Go Back</a>"


@main.route('/api/check-availability', methods=['GET'])
@login_required
def check_availability():
    # 1. Get Parameters
    day = request.args.get('day')
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    semester = request.args.get('semester')
    exclude_id = request.args.get('exclude_id', type=int) # <--- NEW PARAMETER

    # 2. Get Active School Year
    school_year_setting = SystemSetting.query.filter_by(key='school_year').first()
    active_sy = school_year_setting.value.strip() if school_year_setting and school_year_setting.value else None

    if not (day and start_str and end_str and active_sy):
        return jsonify({'busy_rooms': [], 'busy_sections': [], 'busy_faculty': []})

    try:
        # 3. Parse Time (accepts both the native picker's 24h "19:00" and
        #    older 12h "07:00 PM" values)
        time_start = parse_time_input(start_str)
        time_end = parse_time_input(end_str)
        if not time_start or not time_end:
            return jsonify({'busy_rooms': [], 'busy_sections': [], 'busy_faculty': []})

        # 4. Query Overlapping Schedules
        query = db.session.query(
            TeachingLoad.room,
            TeachingLoad.section,
            TeachingLoad.user_id
        ).join(Course, TeachingLoad.course_code == Course.course_code).filter(
            TeachingLoad.school_year == active_sy,
            TeachingLoad.day == day,
            TeachingLoad.time_start < time_end,
            TeachingLoad.time_end > time_start
        )
        if semester:
            query = query.filter(Course.semester == int(semester))

        # 5. EXCLUDE CURRENT SCHEDULE IF EDITING (The Fix)
        if exclude_id:
            query = query.filter(TeachingLoad.id != exclude_id)

        conflicts = query.all()

        # 6. Extract Data
        busy_rooms = [c.room for c in conflicts if c.room]
        busy_faculty = [str(c.user_id) for c in conflicts if c.user_id]
        
        busy_sections = []
        for c in conflicts:
            # Handle "BSCS 2A" vs "2A" mismatch
            if c.section and ' ' in c.section:
                busy_sections.append(c.section.split(' ', 1)[1]) 
            else:
                busy_sections.append(c.section)

        return jsonify({
            'busy_rooms': list(set(busy_rooms)), 
            'busy_sections': list(set(busy_sections)),
            'busy_faculty': list(set(busy_faculty))
        })

    except Exception as e:
        print(f"API Error: {e}")
        return jsonify({'busy_rooms': [], 'busy_sections': [], 'busy_faculty': []})
    

@main.route('/room-schedule', defaults={'room_id': None})
@main.route('/room-schedule/<int:room_id>')
@login_required
def room_schedule(room_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('Access denied.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    # 1. Get System Settings
    semester_setting = SystemSetting.query.filter_by(key='current_semester').first()
    active_sy_setting = SystemSetting.query.filter_by(key='school_year').first()
    
    current_semester = int(semester_setting.value) if semester_setting else 1
    active_school_year = active_sy_setting.value.strip() if active_sy_setting and active_sy_setting.value else None

    # 2. Get All Rooms (Natural Sort)
    all_rooms_raw = Room.query.all()
    def room_sort_key(r):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', r.room_name)]
    all_rooms = sorted(all_rooms_raw, key=room_sort_key)
    
    selected_room = None
    room_schedules = []
    
    if room_id:
        selected_room = Room.query.get_or_404(room_id)
        if active_school_year:
            # Fetch schedules for this room
            room_schedules = db.session.query(TeachingLoad)\
                .join(TeachingLoad.teacher)\
                .join(Course, TeachingLoad.course_code == Course.course_code)\
                .filter(
                    TeachingLoad.room == selected_room.room_name,
                    TeachingLoad.school_year == active_school_year,
                    Course.semester == current_semester
                ).options(joinedload(TeachingLoad.teacher)).all()

    # --- 3. DYNAMIC WATERFALL GRID LOGIC ---
    # Map schedules to days
    schedules_by_day = defaultdict(list)
    for s in room_schedules:
        schedules_by_day[s.day].append(s)

    schedule_grid_data = []
    today_date = datetime.now().date()
    
    # Standard Range: 7:00 AM to 9:00 PM
    current_time = datetime.combine(today_date, datetime.strptime("07:00", "%H:%M").time())
    limit_time = datetime.combine(today_date, datetime.strptime("21:00", "%H:%M").time())
    
    # Extend if late classes exist
    if room_schedules:
        max_end = max(s.time_end for s in room_schedules)
        max_end_dt = datetime.combine(today_date, max_end)
        if max_end_dt > limit_time: limit_time = max_end_dt

    while current_time < limit_time:
        row_start = current_time
        
        # Look Ahead Logic
        next_hard_stop = None
        has_class_starting_now = False
        
        for day in DAYS_OF_WEEK:
            for sched in schedules_by_day[day]:
                s_start = datetime.combine(today_date, sched.time_start)
                if s_start == row_start: has_class_starting_now = True
                elif s_start > row_start: 
                    if next_hard_stop is None or s_start < next_hard_stop: next_hard_stop = s_start

        row_end = row_start + timedelta(minutes=30)
        
        if not has_class_starting_now and next_hard_stop:
            gap_minutes = (next_hard_stop - row_start).total_seconds() / 60
            if gap_minutes <= 120: row_end = next_hard_stop

        # Fill Row
        row_days = []
        max_end_found = row_end
        
        for day in DAYS_OF_WEEK:
            found_sched = None
            for sched in schedules_by_day[day]:
                s_start = datetime.combine(today_date, sched.time_start)
                s_end = datetime.combine(today_date, sched.time_end)
                
                if s_start == row_start:
                    found_sched = sched
                    if s_end > max_end_found: max_end_found = s_end
                    break
            row_days.append(found_sched)
        
        row_end = max_end_found
        start_str = row_start.strftime('%I:%M').lstrip('0')
        end_str = row_end.strftime('%I:%M %p').lstrip('0')

        schedule_grid_data.append({
            'display_time': f"{start_str} - {end_str}",
            'days': row_days
        })
        
        current_time = row_end

    return render_template('faculty/room_schedule.html', 
                           title='Room Utilization', 
                           all_rooms=all_rooms, 
                           selected_room=selected_room,
                           schedule_grid=schedule_grid_data,
                           days=DAYS_OF_WEEK,
                           semester=current_semester,
                           school_year=active_school_year)


@main.route('/section-grid/publish/<program_id>/sec<section_name>/sem<int:semester>', methods=['POST'])
@login_required
def publish_section_schedule(program_id, section_name, semester):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        return redirect(url_for('faculty_main.dashboard'))

    blocked = term_closed_response(
        'faculty_main.section_schedule_grid',
        program_id=program_id, section_name=section_name, semester=semester)
    if blocked:
        return blocked

    program = Program.query.get_or_404(program_id)
    full_section_name = f"{program.program_code} {section_name}"
    
    # Get all DRAFTS for this section
    drafts = TeachingLoad.query.filter_by(
        section=full_section_name,
        is_published=False 
    ).join(Course, TeachingLoad.course_code == Course.course_code).filter(
        Course.semester == semester
    ).all()

    if not drafts:
        flash('No changes to save.', 'info')
        return redirect(url_for('faculty_main.section_schedule_grid', program_id=program_id, section_name=section_name, semester=semester))

    # Publish and Notify
    count = 0
    for load in drafts:
        load.is_published = True
        
        # Send the notification NOW
        schedule_details = {
            'course': f"{load.course_code}", 
            'section': load.section, 
            'day': load.day, 
            'time': f"{load.time_start.strftime('%I:%M %p')} - {load.time_end.strftime('%I:%M %p')}", 
            'room': load.room
        }
        send_schedule_update_email(load.teacher, schedule_details, 'added')
        count += 1

    db.session.commit()
    flash(f'Successfully saved and published {count} classes. Faculty members have been notified.', 'success')
    
    return redirect(url_for('faculty_main.section_schedule_grid', program_id=program_id, section_name=section_name, semester=semester))

@main.route('/my-salary', methods=['GET', 'POST'])
@login_required
def my_salary():
    if current_user.role != 'faculty':
        return redirect(url_for('faculty_main.dashboard'))

    # 1. Handle Rate Update
    form = UpdateRateForm()
    if form.validate_on_submit():
        current_user.hourly_rate = form.hourly_rate.data
        db.session.commit()
        flash('Hourly rate updated successfully.', 'success')
        return redirect(url_for('faculty_main.my_salary'))
    
    if request.method == 'GET':
        form.hourly_rate.data = current_user.hourly_rate

    # 2. Determine Date Range
    today = datetime.now().date()
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else:
        # Default Logic (5th and 20th cut-offs)
        if today.day <= 5:
            # Previous Month 16-End
            if today.month == 1:
                start_date = datetime(today.year - 1, 12, 16).date()
                end_date = datetime(today.year - 1, 12, 31).date()
            else:
                import calendar
                last_day = calendar.monthrange(today.year, today.month - 1)[1]
                start_date = datetime(today.year, today.month - 1, 16).date()
                end_date = datetime(today.year, today.month - 1, last_day).date()
        elif today.day <= 20:
             # Current Month 1-15
             start_date = datetime(today.year, today.month, 1).date()
             end_date = datetime(today.year, today.month, 15).date()
        else:
             # Current Month 16-End
             import calendar
             last_day = calendar.monthrange(today.year, today.month)[1]
             start_date = datetime(today.year, today.month, 16).date()
             end_date = datetime(today.year, today.month, last_day).date()

    # --- HELPER: Fix Time Calculation (Solves Negative Hours) ---
    def calc_duration(t_start, t_end):
        # If time is 00:xx (Midnight), treat as 12:xx (Noon)
        if t_end.hour == 0: t_end = t_end.replace(hour=12)
        if t_start.hour == 0: t_start = t_start.replace(hour=12)
        
        d1 = datetime.combine(datetime.min, t_start)
        d2 = datetime.combine(datetime.min, t_end)
        
        # Calculate difference in hours
        diff = (d2 - d1).total_seconds() / 3600
        
        # Safety: Return absolute value to prevent negative pay
        return abs(diff)

    # 3. CALCULATE SCHEDULED HOURS
    total_scheduled_hours = 0
    active_loads = TeachingLoad.query.filter_by(user_id=current_user.id).all()
    
    delta = timedelta(days=1)
    curr = start_date
    while curr <= end_date:
        day_name = curr.strftime("%A")
        for load in active_loads:
            if load.day == day_name:
                # Use Helper
                dur = calc_duration(load.time_start, load.time_end)
                total_scheduled_hours += dur
        curr += delta

    # 4. CALCULATE DEDUCTIONS (Absences)
    absences = SubstitutionRequest.query.filter(
        SubstitutionRequest.original_teacher_id == current_user.id,
        SubstitutionRequest.date_of_absence >= start_date,
        SubstitutionRequest.date_of_absence <= end_date,
        SubstitutionRequest.status.in_(['pending', 'accepted', 'completed'])
    ).all()

    total_deduction_hours = 0
    deduction_details = []
    
    for abs_req in absences:
        load = abs_req.teaching_load
        # Use Helper
        dur = calc_duration(load.time_start, load.time_end)
        total_deduction_hours += dur
        deduction_details.append({
            'date': abs_req.date_of_absence,
            'course': load.course_code,
            'hours': round(dur, 2),
            'status': abs_req.status
        })

    # 5. CALCULATE ADDITIONS (Substitutions)
    substitutions = SubstitutionRequest.query.filter(
        SubstitutionRequest.substitute_teacher_id == current_user.id,
        SubstitutionRequest.date_of_absence >= start_date,
        SubstitutionRequest.date_of_absence <= end_date,
        SubstitutionRequest.status == 'completed'
    ).all()

    total_sub_hours = 0
    sub_details = []
    
    for sub in substitutions:
        load = sub.teaching_load
        # Use Helper
        dur = calc_duration(load.time_start, load.time_end)
        total_sub_hours += dur
        sub_details.append({
            'id': sub.id,          # <--- Add ID
            'status': sub.status,  # <--- Add Status
            'date': sub.date_of_absence,
            'course': load.course_code,
            'original_teacher': sub.original_teacher.full_name,
            'hours': round(dur, 2)
        })

    # 6. FINAL MATH
    rate = current_user.hourly_rate or 0
    net_hours = total_scheduled_hours - total_deduction_hours + total_sub_hours
    gross_pay = net_hours * rate

    return render_template('faculty/my_salary.html', title='My Salary',
                           form=form,
                           start_date=start_date, end_date=end_date,
                           rate=rate,
                           total_scheduled_hours=round(total_scheduled_hours, 2),
                           total_deduction_hours=round(total_deduction_hours, 2),
                           total_sub_hours=round(total_sub_hours, 2),
                           net_hours=round(net_hours, 2),
                           gross_pay=round(gross_pay, 2),
                           deduction_details=deduction_details,
                           sub_details=sub_details)

@main.route('/substitution/upload-proof/<int:request_id>', methods=['GET', 'POST'])
@login_required
def upload_proof(request_id):
    sub_request = SubstitutionRequest.query.get_or_404(request_id)
    
    # Security: Only the substitute OR the original teacher (if makeup/online) can upload
    if sub_request.substitute_teacher_id != current_user.id and sub_request.original_teacher_id != current_user.id:
        flash('You are not authorized to upload proof for this class.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    form = UploadProofForm()
    
    if form.validate_on_submit():
        if form.proof.data:
            file = form.proof.data
            filename = secure_filename(f"proof_{sub_request.id}_{current_user.id}_{file.filename}")
            
            # Ensure directory exists
            upload_dir = current_app.config['UPLOAD_FOLDER']
            if not os.path.exists(upload_dir):
                os.makedirs(upload_dir)
                
            file.save(os.path.join(upload_dir, filename))
            
            # Save to DB
            sub_request.proof_file = filename
            sub_request.status = 'completed' # Auto-mark as completed when proof is sent
            db.session.commit()
            
            flash('Proof uploaded successfully. The request is now marked as Completed.', 'success')
            return redirect(url_for('faculty_main.view_notifications')) # Or redirect to My Schedule

    return render_template('faculty/upload_proof.html', title='Upload Proof', form=form, sub_request=sub_request)

@main.route('/user/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role not in ('dean', 'admin', 'superadmin'):
        flash('You do not have permission to perform this action.', 'error')
        return redirect(url_for('faculty_main.dashboard'))

    user_to_delete = User.query.get_or_404(user_id)

    # 1. Prevent deleting yourself
    if user_to_delete.id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('faculty_main.user_management'))

    # 2. (Optional) Prevent deleting other Deans if you want strict hierarchy
    # if user_to_delete.role == 'dean':
    #     flash('You cannot delete another Dean.', 'error')
    #     return redirect(url_for('faculty_main.user_management'))

    try:
        # 3. Remove the rows that reference this user directly. Faculty
        # assignments and grades have no ON DELETE CASCADE, so they must go
        # first or the delete fails.
        FacultyAssignment.query.filter_by(faculty_id=user_id).delete()
        Grade.query.filter_by(faculty_id=user_id).delete()

        # 4. Delete the user
        # Note: Because of cascading rules in models.py, this should also 
        # delete their teaching loads and schedules automatically.
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f'User "{user_to_delete.full_name}" deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting user: {str(e)}', 'error')

    return redirect(url_for('faculty_main.user_management'))

# ===========================================================================
# GRADING SYSTEM (merged from grading blueprint)
# ===========================================================================



def grading_check_prerequisites(student_id, subject_id):
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


def grading_get_admin_program_scope():
    # Deans (CCBA/COC admins) and grading admins share this route set.
    # Scope follows the department: CCBA & COC are limited, the system
    # admin (no department) outranks them and sees every program.
    if not current_user.is_authenticated or current_user.role not in ('dean', 'admin', 'superadmin'):
        return []
    if current_user.department == 'CCBA':
        return ['BPA', 'BSCS', 'BSMA']
    elif current_user.department == 'COC':
        return ['BSCRIM']
    return None


def grading_admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('dean', 'admin', 'superadmin'):
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('faculty_auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def grading_faculty_id():
    """id of the logged-in faculty member for grading queries.

    Grades and class assignments are keyed by this id. Identity is now a single
    `users` row, so there is no twin account to resolve any more.
    """
    user = current_user
    if not user.is_authenticated:
        return None
    return user.id


def grading_active_term():
    """(school year, semester) currently open, or (None, None) if unset."""
    year_row = SystemSetting.query.filter_by(key='school_year').first()
    sem_row = SystemSetting.query.filter_by(key='current_semester').first()
    school_year = year_row.value.strip() if year_row and year_row.value else None
    semester = int(sem_row.value) if sem_row and str(sem_row.value).strip().isdigit() else None
    return school_year, semester


def grading_section_from_label(section_label):
    """Resolve a schedule label such as 'BSCS 3A' to its Section row."""
    if not section_label:
        return None
    parts = section_label.strip().split(None, 1)
    if len(parts) != 2:
        return None
    program_code, section_name = parts[0].upper(), parts[1].upper()
    return (
        Section.query.join(Program)
        .filter(func.upper(Program.program_code) == program_code,
                func.upper(Section.name) == section_name)
        .first()
    )


def grading_assignments_for(faculty_id):
    """The faculty member's grading classes, taken from the section schedule.

    There is no separate "assign loads" step: a class is gradable exactly when
    it is scheduled for that teacher and published. The FacultyAssignment row
    is only a per-class grading settings record, so it is created on demand the
    first time the class is opened for grading.
    """
    if not faculty_id:
        return []

    school_year, semester = grading_active_term()
    query = TeachingLoad.query.filter_by(user_id=faculty_id, is_published=True)
    if school_year:
        query = query.filter(TeachingLoad.school_year == school_year)
    loads = query.order_by(TeachingLoad.course_code, TeachingLoad.section).all()

    course_semesters = {}
    if semester is not None:
        for course in Course.query.filter_by(semester=semester).all():
            course_semesters[course.course_code] = course

    assignments = []
    seen = set()
    created = False
    for load in loads:
        if semester is not None and load.course_code not in course_semesters:
            continue
        subject = Subject.query.filter_by(subject_code=load.course_code).first()
        section = grading_section_from_label(load.section)
        if not subject or not section:
            continue
        key = (subject.id, section.id)
        if key in seen:
            continue
        seen.add(key)

        assignment = FacultyAssignment.query.filter_by(
            faculty_id=faculty_id, subject_id=subject.id, section_id=section.id
        ).first()
        if not assignment:
            assignment = FacultyAssignment(
                faculty_id=faculty_id, subject_id=subject.id, section_id=section.id
            )
            db.session.add(assignment)
            db.session.flush()
            created = True
        assignments.append(assignment)

    if created:
        db.session.commit()
    return assignments


def grading_assignment_is_scheduled(assignment):
    """True when the class still exists in the faculty member's schedule."""
    if not assignment:
        return False
    return any(a.id == assignment.id
               for a in grading_assignments_for(assignment.faculty_id))


@main.route('/logout')
def grading_logout():
    logout_user()
    return redirect(url_for('faculty_auth.login'))

# (helper defined above)
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


# (helper defined above)
    if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
        return []
    if current_user.department == 'CCBA':
        return ['BPA', 'BSCS', 'BSMA']
    elif current_user.department == 'COC':
        return ['BSCRIM']
    return None


# (helper defined above)
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'superadmin'):
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('faculty_auth.login'))
        return f(*args, **kwargs)
    return decorated_function


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

@main.route("/grading-admin/maintenance", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_admin_maintenance():
    if current_user.role != 'superadmin':
        flash('Only the superadministrator can manage maintenance mode.', 'danger')
        return redirect(url_for('faculty_main.grading_admin_dashboard'))

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
        return redirect(url_for('faculty_main.grading_admin_maintenance'))

    row = db.session.execute(
        text("SELECT setting_value FROM system_settings WHERE setting_key = 'maintenance_mode'")
    ).fetchone()
    maintenance_on = bool(row and row[0] == '1')
    return render_template(
        'grading/admin/admin_maintenance.html',
        title='System Maintenance', active_nav='maintenance',
        maintenance_on=maintenance_on
    )


@main.route("/grading-admin/dashboard")
@login_required
@grading_admin_required
def grading_admin_dashboard():
    scope = grading_get_admin_program_scope()

    pending_approvals = User.query.filter_by(role='faculty', can_edit=False).count()
    faculty_count = User.query.filter_by(role='faculty', can_edit=True).count()

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


@main.route("/grading-admin/approvals")
@login_required
@grading_admin_required
def grading_admin_approvals():
    # Phase 2: this screen duplicated the dean's approval queue once both
    # portals shared the `users` table. The queue now lives in one place.
    return redirect(url_for('faculty_main.pending_approvals'))


@main.route("/grading-admin/student_records")
@login_required
@grading_admin_required
def grading_student_records():
    scope = grading_get_admin_program_scope()
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


@main.route("/grading-admin/student/<int:student_id>/grades")
@login_required
@grading_admin_required
def grading_view_student_grades(student_id):
    student = Applicant.query.get_or_404(student_id)
    scope = grading_get_admin_program_scope()
    if scope and student.section and student.section.program and student.section.program.program_code not in scope:
        return redirect(url_for('faculty_main.grading_student_records'))

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


@main.route("/grading-admin/student/<int:student_id>/grades/print")
@login_required
@grading_admin_required
def grading_print_student_grades(student_id):
    student = Applicant.query.get_or_404(student_id)
    scope = grading_get_admin_program_scope()
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


@main.route("/grading-admin/faculty_records")
@login_required
@grading_admin_required
def grading_admin_faculty_records():
    # Phase 2: folded into the dean's User Management screen (same `users` table).
    return redirect(url_for('faculty_main.user_management'))


@main.route("/grading-admin/add_faculty", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_add_faculty():
    # Phase 2: the dean's User Management form is the single place that creates
    # faculty accounts (it already covers program/ expertise assignment).
    return redirect(url_for('faculty_main.user_management'))


@main.route("/grading-admin/faculty/<int:user_id>/assign", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_admin_assign_loads(user_id):
    # Grading classes now come from the section schedule, so the old manual
    # "assign loads" screen has no input of its own. Keep the URL working and
    # point the dean at the screen that actually assigns the teacher.
    faculty = User.query.get_or_404(user_id)
    flash(f"{faculty.full_name}'s classes now come from the section schedule.", 'info')
    return redirect(url_for('faculty_main.manage_section_schedule_hub'))


@main.route("/grading-admin/remove_load/<int:assignment_id>", methods=['POST'])
@login_required
@grading_admin_required
def grading_remove_faculty_load(assignment_id):
    flash('Classes follow the section schedule now. Remove the class from the schedule instead.', 'info')
    return redirect(url_for('faculty_main.manage_section_schedule_hub'))


@main.route("/grading-admin/faculty/delete/<int:user_id>", methods=['POST'])
@login_required
@grading_admin_required
def grading_delete_faculty(user_id):
    # Phase 2: one delete path - the dean's delete_user, which now removes the
    # faculty's assignments and grades too.
    return delete_user(user_id)


@main.route("/grading-admin/programs")
@login_required
@grading_admin_required
def grading_manage_programs():
    # Phase 2: folded into the dean's Manage Programs screen (same `programs` table).
    return redirect(url_for('faculty_main.manage_programs'))


@main.route("/grading-admin/programs/add", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_add_program():
    # Phase 2: adding a program happens on the dean's Manage Programs screen.
    return redirect(url_for('faculty_main.manage_programs'))


@main.route("/grading-admin/programs/<program_id>")
@login_required
@grading_admin_required
def grading_view_program_subjects(program_id):
    program = Program.query.get_or_404(program_id)
    subjects = Subject.query.filter_by(program_id=program.id).order_by(
        Subject.year_level, Subject.semester, Subject.subject_code
    ).all()
    return render_template(
        'grading/admin/view_program_subjects.html',
        title=f"{program.program_code} Subjects", active_nav='programs',
        program=program, subjects=subjects
    )


@main.route("/grading-admin/programs/<program_id>/add_subject", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_add_subject(program_id):
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
        return redirect(url_for('faculty_main.grading_view_program_subjects', program_id=program.id))
    return render_template(
        'grading/admin/add_subject.html',
        title="Add Subject", active_nav='programs', program=program
    )


@main.route("/grading-admin/delete_program/<program_id>", methods=['POST'])
@login_required
@grading_admin_required
def grading_delete_program(program_id):
    # Force-delete escape hatch: removes a program plus its subjects, sections,
    # courses and grade records, which the dean's safe delete refuses to do.
    # Restricted to the superadmin because it bypasses every safety check.
    if current_user.role != 'superadmin':
        flash('Only the superadministrator can force-delete a program.', 'danger')
        return redirect(url_for('faculty_main.manage_programs'))

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
    return redirect(url_for('faculty_main.manage_programs'))


@main.route("/grading-admin/delete_subject/<int:subject_id>", methods=['POST'])
@login_required
@grading_admin_required
def grading_delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    program_id = subject.program_id
    db.session.delete(subject)
    db.session.commit()
    flash('Subject deleted.', 'success')
    return redirect(url_for('faculty_main.grading_view_program_subjects', program_id=program_id))


@main.route("/grading-admin/settings", methods=['GET', 'POST'])
@login_required
@grading_admin_required
def grading_settings():
    # The academic term is owned by /faculty/system-settings, which writes the
    # `system_settings` keys the dashboard and scheduling actually read and
    # keeps `academic_terms` / `grading_settings` in sync. This screen edited a
    # second copy of the same two values and nothing read it back, so saving
    # here silently reverted the real term.
    return redirect(url_for('faculty_main.system_settings'))


@main.route("/grading-admin/student_records/print")
@login_required
@grading_admin_required
def grading_print_student_records():
    scope = grading_get_admin_program_scope()
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


@main.route("/grading-admin/class/<int:assignment_id>/add_student", methods=['POST'])
@login_required
@grading_admin_required
def grading_admin_add_irregular_student(assignment_id):
    assignment = FacultyAssignment.query.get_or_404(assignment_id)
    student_number = request.form.get('student_number')
    student = Applicant.query.filter_by(old_student_id=student_number).first()
    if not student:
        flash('Student ID not found.', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    existing_grade = Grade.query.filter_by(
        student_id=student.applicant_id,
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
    ).first()
    if existing_grade:
        flash('Student is already in this class list.', 'warning')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    is_allowed, message = grading_check_prerequisites(student.applicant_id, assignment.subject_id)
    if not is_allowed:
        flash(f'Cannot add student: {message}', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    new_grade = Grade(
        student_id=student.applicant_id,
        subject_id=assignment.subject_id,
        faculty_id=assignment.faculty_id
    )
    db.session.add(new_grade)
    db.session.commit()
    flash(f'Irregular Student {student.name} successfully added to class.', 'success')
    return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))


# ---------------------------------------------------------------------------
# Faculty Routes
# ---------------------------------------------------------------------------

@main.route("/grades/dashboard")
@login_required
def grading_faculty_dashboard():
    assignments = grading_assignments_for(grading_faculty_id())
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
                faculty_id=grading_faculty_id()
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


@main.route("/grades/manage_classes")
@login_required
def grading_manage_classes():
    current_assignments = grading_assignments_for(grading_faculty_id())
    return render_template(
        'grading/faculty/manage_classes.html',
        title="My Class Load", active_nav='manage_classes',
        current_assignments=current_assignments
    )


@main.route("/grades/remove_assignment/<int:assignment_id>", methods=['GET'])
@login_required
def grading_remove_assignment(assignment_id):
    # The schedule owns class membership now; there is nothing to unassign.
    flash('Classes follow the section schedule. Ask your dean to change the schedule instead.', 'info')
    return redirect(url_for('faculty_main.grading_manage_classes'))


@main.route("/grades/grade_management", methods=['GET', 'POST'])
@login_required
def grading_grade_management():
    assignments = grading_assignments_for(grading_faculty_id())
    assignment_id = request.args.get('assignment_id') or request.form.get('assignment_id')

    selected_assignment = None
    students = []
    grades_dict = {}

    if assignment_id:
        selected_assignment = next(
            (a for a in assignments if str(a.id) == str(assignment_id)), None)
        if selected_assignment:
            regular_students = Applicant.query.filter_by(section_id=selected_assignment.section_id).all()
            irregular_grades = Grade.query.filter_by(
                subject_id=selected_assignment.subject_id, faculty_id=grading_faculty_id()
            ).all()
            irregular_student_ids = [g.student_id for g in irregular_grades]
            irregular_students = Applicant.query.filter(Applicant.applicant_id.in_(irregular_student_ids)).all()
            all_students_set = set(regular_students + irregular_students)
            students = sorted(list(all_students_set), key=lambda x: x.last_name)

            all_grades = Grade.query.filter_by(
                subject_id=selected_assignment.subject_id, faculty_id=grading_faculty_id()
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
                grade = Grade(student_id=sid_int, subject_id=selected_assignment.subject_id, faculty_id=grading_faculty_id())
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

            grading_recalculate(selected_assignment, grade)

        if errors:
            db.session.rollback()
            for msg in errors:
                flash(msg, 'danger')
            flash('No grades were saved. Please correct the invalid entries above.', 'warning')
            return redirect(url_for('faculty_main.grading_grade_management', assignment_id=selected_assignment.id))

        db.session.commit()
        flash('Grades updated successfully.', 'success')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=selected_assignment.id))

    return render_template(
        'grading/faculty/grade_management.html',
        title='Grade Management', active_nav='grades',
        assignments=assignments, selected_assignment=selected_assignment,
        students=students, grades_dict=grades_dict
    )


# ---------------------------------------------------------------------------
# Grade Management — Export / Import / Template
# ---------------------------------------------------------------------------

# (Excel header label, grade column field) for every raw-score column.
GRADING_COLUMN_SPECS = []
for _term, _label in (('p', 'PRELIM'), ('m', 'MIDTERM'), ('f', 'FINALS')):
    for _i in range(1, 6):
        GRADING_COLUMN_SPECS.append((f'{_label} QUIZ {_i}', f'{_term}_q{_i}'))
    GRADING_COLUMN_SPECS.append((f'{_label} RECITATION', f'{_term}_recit'))
    for _i in range(1, 6):
        GRADING_COLUMN_SPECS.append((f'{_label} ACTIVITY {_i}', f'{_term}_act{_i}'))
    GRADING_COLUMN_SPECS.append((f'{_label} EXAM', f'{_term}_exam'))

# (grade field -> FacultyAssignment perfect-score column)
GRADING_MAX_FIELDS = {}
for _term in ('p', 'm', 'f'):
    for _i in range(1, 6):
        GRADING_MAX_FIELDS[f'{_term}_q{_i}'] = f'{_term}_max_q{_i}'
        GRADING_MAX_FIELDS[f'{_term}_act{_i}'] = f'{_term}_max_act{_i}'
    GRADING_MAX_FIELDS[f'{_term}_recit'] = f'{_term}_max_r'
    GRADING_MAX_FIELDS[f'{_term}_exam'] = f'{_term}_max_exam'


def grading_owned_assignment(assignment_id):
    """Fetch an assignment only if it belongs to the logged-in faculty member
    and the class still exists in their section schedule."""
    assignment = FacultyAssignment.query.get(int(assignment_id))
    if not assignment or assignment.faculty_id != grading_faculty_id():
        flash('You do not have permission to manage this class.', 'danger')
        return None
    if not grading_assignment_is_scheduled(assignment):
        flash('This class is no longer in your schedule.', 'danger')
        return None
    return assignment


def grading_class_students(assignment):
    """Regular students of the section plus any irregular students already graded."""
    regular_students = Applicant.query.filter_by(section_id=assignment.section_id).all()
    irregular_ids = [
        g.student_id for g in Grade.query.filter_by(
            subject_id=assignment.subject_id, faculty_id=grading_faculty_id()
        ).all()
    ]
    irregular_students = Applicant.query.filter(Applicant.applicant_id.in_(irregular_ids)).all() if irregular_ids else []
    merged = set(regular_students + irregular_students)
    return sorted(merged, key=lambda s: s.last_name or '')


def grading_recalculate(assignment, grade):
    """Recompute prelim/midterm/final grades and the grade point from raw scores."""
    def calc_equiv(raw, max_score):
        if not max_score or max_score <= 0:
            return 0
        return (raw or 0) / max_score * 100

    grade.prelim_grade = round(
        (calc_equiv(grade.p_quiz_total, assignment.p_total_max_q) * 0.20) +
        (calc_equiv(grade.p_recit, assignment.p_max_r) * 0.20) +
        (calc_equiv(grade.p_act_total, assignment.p_total_max_a) * 0.10) +
        (calc_equiv(grade.p_exam, assignment.p_max_exam) * 0.50)
    )
    grade.midterm_grade = round(
        (calc_equiv(grade.m_quiz_total, assignment.m_total_max_q) * 0.20) +
        (calc_equiv(grade.m_recit, assignment.m_max_r) * 0.10) +
        (calc_equiv(grade.m_act_total, assignment.m_total_max_a) * 0.20) +
        (calc_equiv(grade.m_exam, assignment.m_max_exam) * 0.50)
    )
    grade.final_term_grade = round(
        (calc_equiv(grade.f_quiz_total, assignment.f_total_max_q) * 0.20) +
        (calc_equiv(grade.f_recit, assignment.f_max_r) * 0.20) +
        (calc_equiv(grade.f_act_total, assignment.f_total_max_a) * 0.10) +
        (calc_equiv(grade.f_exam, assignment.f_max_exam) * 0.50)
    )
    grade.midterm_standing = round((grade.prelim_grade * 0.50) + (grade.midterm_grade * 0.50))
    grade.final_grade = round((grade.midterm_standing * 0.60) + (grade.final_term_grade * 0.40))

    score = grade.final_grade or 0
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


def _grading_header_map(ws, header_row):
    """Build {column_index: grade_field} from a header row, plus id/name columns."""
    mapping = {}
    id_col = None
    name_col = None
    for cell in ws[header_row]:
        label = str(cell.value or '').strip().upper()
        if not label:
            continue
        if label in ('STUDENT ID', 'ID', 'STUDENT NO', 'STUDENT NUMBER'):
            id_col = cell.column
        elif label in ('STUDENT NAME', 'NAME'):
            name_col = cell.column
        else:
            for header, field in GRADING_COLUMN_SPECS:
                if label == header:
                    mapping[cell.column] = field
                    break
    return id_col, name_col, mapping


def _grading_write_sheet(ws, assignment, students, grades_dict, include_scores):
    """Write the shared header/metadata/table layout used by export and template."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    subject_code = assignment.subject.subject_code if assignment.subject else ''
    subject_title = assignment.subject.subject_title if assignment.subject else ''
    section_name = assignment.section.name if assignment.section else ''
    program_code = assignment.section.program.program_code if assignment.section and assignment.section.program else ''

    ws['A1'] = 'GRADE MANAGEMENT SHEET'
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = f'Subject: {subject_code} - {subject_title}'
    ws['A3'] = f'Section: {program_code} {section_name}'

    headers = ['STUDENT ID', 'STUDENT NAME'] + [h for h, _ in GRADING_COLUMN_SPECS]
    thin = Side(style='thin', color='B0B0B0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill('solid', fgColor='DBEAFE')

    header_row = 5
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=header)
        cell.font = Font(bold=True, size=10)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border

    for row_offset, student in enumerate(students, start=1):
        g = grades_dict.get(student.applicant_id)
        row = header_row + row_offset
        ws.cell(row=row, column=1, value=student.old_student_id or '')
        ws.cell(row=row, column=2, value=student.name)
        for col, (_, field) in enumerate(GRADING_COLUMN_SPECS, start=3):
            value = ''
            if include_scores and g is not None:
                raw = getattr(g, field, None)
                value = raw if raw not in (None,) else ''
            cell = ws.cell(row=row, column=col, value=value)
            cell.alignment = Alignment(horizontal='center')
            cell.border = border
        ws.cell(row=row, column=1).border = border
        ws.cell(row=row, column=2).border = border

    ws.column_dimensions['A'].width = 16
    ws.column_dimensions['B'].width = 34
    for col in range(3, 3 + len(GRADING_COLUMN_SPECS)):
        ws.column_dimensions[ws.cell(row=header_row, column=col).column_letter].width = 13
    ws.row_dimensions[header_row].height = 34
    ws.freeze_panes = ws.cell(row=header_row + 1, column=3)
    return header_row


@main.route("/grades/grade_management/export/<int:assignment_id>")
@login_required
def grading_export_grades(assignment_id):
    from openpyxl import Workbook

    assignment = grading_owned_assignment(assignment_id)
    if not assignment:
        return redirect(url_for('faculty_main.grading_grade_management'))

    students = grading_class_students(assignment)
    all_grades = Grade.query.filter_by(
        subject_id=assignment.subject_id, faculty_id=grading_faculty_id()
    ).all()
    grades_dict = {g.student_id: g for g in all_grades}

    wb = Workbook()
    ws = wb.active
    ws.title = 'Grades'
    _grading_write_sheet(ws, assignment, students, grades_dict, include_scores=True)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    subject_code = assignment.subject.subject_code if assignment.subject else 'class'
    section_name = assignment.section.name if assignment.section else 'section'
    filename = f'Grades_{subject_code}_{section_name}.xlsx'.replace(' ', '')
    return send_file(
        output, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@main.route("/grades/grade_management/template/<int:assignment_id>")
@login_required
def grading_grades_template(assignment_id):
    from openpyxl import Workbook

    assignment = grading_owned_assignment(assignment_id)
    if not assignment:
        return redirect(url_for('faculty_main.grading_grade_management'))

    students = grading_class_students(assignment)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Grades'
    _grading_write_sheet(ws, assignment, students, {}, include_scores=False)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    subject_code = assignment.subject.subject_code if assignment.subject else 'class'
    section_name = assignment.section.name if assignment.section else 'section'
    filename = f'Template_Grades_{subject_code}_{section_name}.xlsx'.replace(' ', '')
    return send_file(
        output, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@main.route("/grades/grade_management/import/<int:assignment_id>", methods=['POST'])
@login_required
def grading_import_grades(assignment_id):
    from openpyxl import load_workbook

    assignment = grading_owned_assignment(assignment_id)
    if not assignment:
        return redirect(url_for('faculty_main.grading_grade_management'))

    file = request.files.get('file')
    if not file or not file.filename:
        flash('Please choose an Excel file to import.', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))
    if not file.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('Only .xlsx Excel files are supported.', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    students = grading_class_students(assignment)
    by_id = {s.old_student_id: s for s in students if s.old_student_id}
    by_name = {(s.name or '').strip().upper(): s for s in students}
    by_last_first = {}
    for s in students:
        key = f"{(s.last_name or '').strip().upper()}, {(s.first_name or '').strip().upper()}"
        by_last_first[key] = s
    grades_dict = {
        g.student_id: g for g in Grade.query.filter_by(
            subject_id=assignment.subject_id, faculty_id=grading_faculty_id()
        ).all()
    }

    try:
        wb = load_workbook(file, data_only=True)
        ws = wb.active
    except Exception:
        flash('The uploaded file could not be opened as an Excel workbook.', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    # Locate the header row within the first 15 rows.
    header_row = None
    for row_idx in range(1, 16):
        _, name_col, mapping = _grading_header_map(ws, row_idx)
        if name_col and mapping:
            header_row = row_idx
            break
    if header_row is None:
        flash('No header row found. Please use the downloaded template file.', 'danger')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    id_col, name_col, col_fields = _grading_header_map(ws, header_row)

    errors = []
    updated = 0
    skipped = 0
    touched = set()

    for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row):
        def cell_value(col):
            if not col or col > len(row):
                return None
            return row[col - 1].value

        raw_id = cell_value(id_col)
        raw_name = cell_value(name_col)

        id_str = str(raw_id).strip() if raw_id is not None else ''
        name_str = str(raw_name).strip() if raw_name is not None else ''
        if not id_str and not name_str:
            continue

        student = None
        if id_str:
            student = by_id.get(id_str)
        if student is None and name_str:
            upper = name_str.upper()
            student = by_name.get(upper) or by_last_first.get(upper)
        if student is None:
            skipped += 1
            errors.append(f"'{name_str or id_str}' is not in this class list — skipped.")
            continue

        if student.applicant_id in touched:
            continue
        touched.add(student.applicant_id)

        row_has_score = False
        for col, field in col_fields.items():
            raw = cell_value(col)
            if raw is None or str(raw).strip() == '':
                continue
            try:
                score = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{student.name} — {field}: '{raw}' is not a valid number.")
                continue
            max_val = getattr(assignment, GRADING_MAX_FIELDS[field], 0) or 0
            if score < 0:
                errors.append(f"{student.name} — {field}: {score} is negative.")
                continue
            if max_val > 0 and score > max_val:
                errors.append(f"{student.name} — {field}: {score} exceeds max {max_val}.")
                continue
            grade = grades_dict.get(student.applicant_id)
            if not grade:
                grade = Grade(
                    student_id=student.applicant_id,
                    subject_id=assignment.subject_id,
                    faculty_id=grading_faculty_id()
                )
                db.session.add(grade)
                grades_dict[student.applicant_id] = grade
            setattr(grade, field, score)
            row_has_score = True

        if row_has_score:
            grading_recalculate(assignment, grades_dict[student.applicant_id])
            updated += 1

    if errors and not updated:
        db.session.rollback()
        for msg in errors[:10]:
            flash(msg, 'danger')
        if skipped:
            flash(f'{skipped} row(s) did not match a student in this class.', 'warning')
        flash('No grades were imported. Please correct the file and try again.', 'warning')
        return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))

    db.session.commit()

    if updated:
        flash(f'Successfully imported grades for {updated} student(s).', 'success')
    else:
        flash('No grade values were found in the uploaded file.', 'warning')

    if skipped:
        flash(f'{skipped} row(s) did not match a student in this class.', 'warning')
    for msg in errors[:10]:
        flash(msg, 'warning')
    if len(errors) > 10:
        flash(f'...and {len(errors) - 10} more issues.', 'warning')

    return redirect(url_for('faculty_main.grading_grade_management', assignment_id=assignment.id))


@main.route("/grades/print_report/<int:assignment_id>/<report_type>")
@login_required
def grading_print_faculty_report(assignment_id, report_type):
    assignment = grading_owned_assignment(assignment_id)
    if not assignment:
        return redirect(url_for('faculty_main.grading_grade_management'))

    students = Applicant.query.filter_by(section_id=assignment.section_id).order_by(Applicant.last_name).all()
    grades = Grade.query.filter_by(subject_id=assignment.subject_id, faculty_id=grading_faculty_id()).all()
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

@main.route('/get_sections_for_filters')
@login_required
def grading_get_sections_for_filters():
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


@main.route('/get_subjects_for_filters')
@login_required
def grading_get_subjects_for_filters():
    program_id = request.args.get('program_id', type=str)
    year = request.args.get('year', type=int)
    semester = request.args.get('semester', type=int)
    if not all([program_id, year, semester]):
        return jsonify([])
    subjects = Subject.query.filter_by(
        program_id=program_id, year_level=year, semester=semester
    ).order_by(Subject.subject_title).all()
    return jsonify([{'id': s.id, 'code': s.subject_code, 'description': s.subject_title} for s in subjects])

