# website/models.py
#
# Single source of truth for ALL database tables used by the combined
# Admission + Faculty + Grading system. Run db.create_all() on a fresh
# database to create the entire schema.
#
# NOTE: The faculty blueprints re-export these models from
# website.faculty.models for backwards compatibility.

import sqlalchemy as _sa
from website import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import random
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app


# =========================================================
# ACADEMIC TERMS / SCHOOL YEAR
# =========================================================

class AcademicTerm(db.Model):
    __tablename__ = 'academic_terms'

    id = db.Column(db.Integer, primary_key=True)
    year_name = db.Column(db.String(50), nullable=False)
    semester = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    max_units = db.Column(db.Integer, default=26)

    __table_args__ = (
        db.UniqueConstraint('year_name', 'semester', name='unique_term'),
    )

    def __repr__(self):
        return f'<AcademicTerm {self.year_name} - {self.semester}>'


class AdmissionStep(db.Model):
    __tablename__ = 'admission_steps'

    id = db.Column(db.Integer, primary_key=True)
    step_number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    icon_class = db.Column(db.String(50), default='ri-number-1')
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class StudentEnrollmentRecord(db.Model):
    __tablename__ = 'student_enrollment_records'

    id = db.Column(db.Integer, primary_key=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('student_users.id'), nullable=False)
    academic_term_id = db.Column(db.Integer, db.ForeignKey('academic_terms.id'), nullable=False)
    year_level = db.Column(db.String(50))
    enrollment_semester = db.Column(db.String(50))
    enrollment_status = db.Column(db.String(50), default='Enrolling')
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    is_approved_by_president = db.Column(db.Boolean, default=False)
    president_approval_date = db.Column(db.DateTime)
    is_approved_by_accounting = db.Column(db.Boolean, default=False)
    accounting_approval_date = db.Column(db.DateTime)
    is_approved_by_dean = db.Column(db.Boolean, default=False)
    dean_approval_date = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('student_user_id', 'academic_term_id', name='unique_enrollment'),
    )


# =========================================================
# CURRICULUM / PROGRAMS / SUBJECTS
# =========================================================

class Program(db.Model):
    __tablename__ = 'programs'

    id = db.Column('program_id', db.String(50), primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    hero_image_filename = db.Column(db.String(255))
    description = db.Column(db.Text)
    admission_link_endpoint = db.Column(db.String(255), default='views.existing_or_not')
    total_units = db.Column(db.Integer, default=0)
    max_units = db.Column(db.Integer, default=26)
    program_code = db.Column(db.String(20))
    program_name = db.Column(db.String(200))

    courses = db.relationship('Course', backref='program', lazy='dynamic')
    subjects = db.relationship('Subject', backref='program', lazy=True)

    @property
    def code(self):
        return self.program_code

    @property
    def program_id(self):
        return self.id

    @property
    def name(self):
        return self.program_name

    def __repr__(self):
        return f'<Program {self.program_code}>'


@_sa.event.listens_for(Program, 'before_insert')
def _program_assign_id(mapper, connection, target):
    if not target.id:
        target.id = (target.program_code or '').lower()
    if not target.title:
        target.title = target.program_name or ''


class Course(db.Model):
    __tablename__ = 'courses'

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    course_code = db.Column(db.String(20), nullable=False)
    course_description = db.Column(db.String(200), nullable=False)
    units = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    semester = db.Column(db.Integer, nullable=False)

    def __repr__(self):
        return f'<Course {self.course_code}>'


class Section(db.Model):
    __tablename__ = 'sections'

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    year_level = db.Column(db.String(20), nullable=False)
    name = db.Column('section_name', db.String(50), nullable=False)
    section_letter = db.Column(db.String(5), nullable=False)
    max_students = db.Column(db.Integer, default=60)
    current_students = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    semester = db.Column(db.Integer)
    school_year = db.Column(db.String(9), index=True)

    __table_args__ = (
        db.UniqueConstraint('program_id', 'year_level', 'section_name'),
    )

    program = db.relationship('Program', backref=db.backref('sections', cascade="all, delete-orphan"))
    applicants = db.relationship('Applicant', backref=db.backref('section', lazy='joined'), lazy='dynamic')

    def __repr__(self):
        return f'<Section {self.program.program_code} {self.name}>'


subject_prerequisites = db.Table(
    'subject_prerequisites',
    db.Column('subject_id', db.Integer, db.ForeignKey('subjects.id'), primary_key=True),
    db.Column('prerequisite_id', db.Integer, db.ForeignKey('subjects.id'), primary_key=True)
)


class Subject(db.Model):
    __tablename__ = 'subjects'

    id = db.Column(db.Integer, primary_key=True)
    subject_code = db.Column(db.String(20), nullable=False, unique=True)
    subject_title = db.Column(db.String(255), nullable=False)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    units = db.Column(db.Numeric(3, 1), default=3.0)
    semester = db.Column(db.Integer)
    prerequisite_subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'))
    year_level = db.Column(db.Integer)

    prerequisites = db.relationship(
        'Subject',
        secondary=subject_prerequisites,
        primaryjoin=(subject_prerequisites.c.subject_id == id),
        secondaryjoin=(subject_prerequisites.c.prerequisite_id == id),
        backref=db.backref('required_for', lazy='dynamic'),
        lazy='dynamic'
    )

    @property
    def code(self):
        return self.subject_code

    @property
    def description(self):
        return self.subject_title

    @property
    def year(self):
        return self.year_level


class Prerequisite(db.Model):
    __tablename__ = 'prerequisites'

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    prerequisite_subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('subject_id', 'prerequisite_subject_id'),
    )


class ProgramObjective(db.Model):
    __tablename__ = 'program_objectives'

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    objective_text = db.Column(db.Text, nullable=False)


class ProgramCareer(db.Model):
    __tablename__ = 'program_careers'

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    career_text = db.Column(db.String(255), nullable=False)


class ProgramCourse(db.Model):
    __tablename__ = 'program_courses'

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'), nullable=False)
    course_code = db.Column(db.String(50))
    course_name = db.Column(db.String(255), nullable=False)
    course_description = db.Column(db.Text)


# =========================================================
# USERS (faculty, system/staff, student)
# =========================================================

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True, nullable=True)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), nullable=False, default='faculty')
    status = db.Column(db.String(20), nullable=False, default='pending')
    full_name = db.Column(db.String(150), nullable=False)
    contact_number = db.Column(db.String(20))
    department = db.Column(db.String(100))

    baccalaureate_degree = db.Column(db.String(150))
    baccalaureate_year_grad = db.Column(db.Integer)
    highest_edu_attainment = db.Column(db.String(150))
    highest_edu_year_grad = db.Column(db.String(20))
    industry_experience = db.Column(db.Text)
    is_licensed = db.Column(db.Boolean, default=False)
    major = db.Column(db.String(100))
    subjects_taught = db.Column(db.Text)

    otp = db.Column(db.String(6))
    otp_expiration = db.Column(db.DateTime)
    hourly_rate = db.Column(db.Float, default=0.0)

    expertise_type = db.Column(db.String(10))
    program_id = db.Column(db.String(50), db.ForeignKey('programs.program_id'))

    program = db.relationship('Program', backref='major_faculty')
    teaching_loads = db.relationship('TeachingLoad', backref='teacher', lazy='dynamic', cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_otp(self):
        self.otp = str(random.randint(100000, 999999))
        self.otp_expiration = datetime.utcnow() + timedelta(minutes=10)
        return self.otp

    def verify_otp(self, user_otp):
        if self.otp == user_otp and self.otp_expiration > datetime.utcnow():
            self.otp = None
            self.otp_expiration = None
            return True
        return False

    def get_reset_password_token(self, expires_sec=1800):
        s = Serializer(current_app.config['SECRET_KEY'])
        return s.dumps({'user_id': self.id})

    @staticmethod
    def verify_reset_password_token(token, expires_sec=1800):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            user_id = s.loads(token, max_age=expires_sec)['user_id']
        except Exception:
            return None
        return User.query.get(user_id)

    def __repr__(self):
        return f'<User {self.full_name}>'


@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(int(user_id))
    if user:
        return user
    return SystemUser.query.get(int(user_id))


class SystemUser(UserMixin, db.Model):
    __tablename__ = 'system_users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column('password', db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    full_name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    can_edit = db.Column(db.Boolean, default=True)
    department = db.Column(db.String(100))
    email = db.Column(db.String(100))
    system = db.Column(db.String(20), default='admission')

    @property
    def name(self):
        return self.full_name

    @property
    def is_approved(self):
        return True

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        if not self.password:
            return False
        try:
            return check_password_hash(self.password, password)
        except Exception:
            return False


class StudentUser(db.Model):
    __tablename__ = 'student_users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    otp_code = db.Column(db.String(10))
    otp_expiry = db.Column(db.DateTime)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    student_type = db.Column(db.String(10), default='new', nullable=False)
    old_student_id = db.Column(db.String(255))
    reset_token = db.Column(db.String(128))
    reset_token_expiry = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), nullable=False)


class Applicant(db.Model):
    __tablename__ = 'applicants'

    applicant_id = db.Column(db.Integer, primary_key=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('student_users.id'), nullable=False)
    program_choice = db.Column(db.String(255))
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    middle_name = db.Column(db.String(100))
    date_of_birth = db.Column(db.Date, nullable=False)
    place_of_birth = db.Column(db.Text)
    sex = db.Column(db.String(20), nullable=False)
    civil_status = db.Column(db.String(50), nullable=False)
    religion = db.Column(db.String(100), nullable=False)
    citizenship = db.Column(db.String(100), nullable=False)
    mobile_number = db.Column(db.String(30), nullable=False)
    email_address = db.Column(db.String(255), nullable=False)
    permanent_address_street_barangay = db.Column(db.Text)
    permanent_address_city_municipality = db.Column(db.String(100), nullable=False)
    permanent_address_province = db.Column(db.String(100), nullable=False)
    permanent_address_postal_code = db.Column(db.String(20), nullable=False)
    cultural_minority_group = db.Column(db.Text)
    physical_disability = db.Column(db.Text)
    control_number = db.Column(db.String(50), unique=True)
    permit_control_no = db.Column(db.String(50), unique=True)
    old_student_id = db.Column(db.String(255))
    permit_exam_date = db.Column(db.Date)
    permit_exam_time = db.Column(db.String(100))
    permit_testing_room = db.Column(db.String(100))
    date_of_application = db.Column(db.Date, nullable=False)
    academic_year = db.Column(db.String(20), nullable=False)
    average_family_income = db.Column(db.String(100), nullable=False)
    father_name = db.Column(db.String(255))
    father_occupation = db.Column(db.String(100))
    father_company_address = db.Column(db.Text)
    father_contact_number = db.Column(db.String(30))
    mother_maiden_name = db.Column(db.String(255))
    mother_occupation = db.Column(db.String(100))
    mother_company_address = db.Column(db.Text)
    mother_contact_number = db.Column(db.String(30))
    guardian_name = db.Column(db.String(255), nullable=False)
    guardian_occupation = db.Column(db.String(100), nullable=False)
    guardian_company_address = db.Column(db.Text)
    guardian_contact_number = db.Column(db.String(30), nullable=False)
    senior_high_school = db.Column(db.Text)
    senior_high_school_address = db.Column(db.Text)
    senior_high_school_track_strand = db.Column(db.String(100), nullable=False)
    senior_high_school_year_from = db.Column(db.String(10), nullable=False)
    senior_high_school_year_to = db.Column(db.String(10), nullable=False)
    tertiary_school = db.Column(db.Text)
    tertiary_school_address = db.Column(db.Text)
    tertiary_course = db.Column(db.Text)
    tertiary_year_from = db.Column(db.String(10))
    tertiary_year_to = db.Column(db.String(10))
    photo = db.Column(db.LargeBinary)
    photo_mimetype = db.Column(db.String(100))
    shs_diploma_file = db.Column(db.LargeBinary)
    shs_diploma_filename = db.Column(db.String(255))
    shs_diploma_mimetype = db.Column(db.String(100))
    shs_card_file = db.Column(db.LargeBinary)
    shs_card_filename = db.Column(db.String(255))
    shs_card_mimetype = db.Column(db.String(100))
    birth_certificate_file = db.Column(db.LargeBinary)
    birth_certificate_filename = db.Column(db.String(255))
    birth_certificate_mimetype = db.Column(db.String(100))
    application_status = db.Column(db.String(50), default='Pending', nullable=False)
    original_enrollment_status = db.Column(db.String(50))
    original_academic_term = db.Column(db.String(100))
    exam_status = db.Column(db.String(50))
    admin_notes = db.Column(db.Text)
    agreements = db.Column(db.String(10), nullable=False)
    final_submission_date = db.Column(db.Date, nullable=False)
    submitted_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), nullable=False)
    decision_date = db.Column(db.DateTime)
    last_updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), nullable=False)
    enrollment_year_level = db.Column(db.String(20))
    enrollment_semester = db.Column(db.String(20))
    enrollment_student_type = db.Column(db.String(20))
    inventory_gender = db.Column(db.String(255))
    inventory_age = db.Column(db.String(50))
    inventory_religion = db.Column(db.String(255))
    inventory_complete_address = db.Column(db.Text)
    inventory_mobile_number = db.Column(db.String(255))
    inventory_facebook_account = db.Column(db.Text)
    inventory_interest_hobbies = db.Column(db.Text)
    inventory_health_condition = db.Column(db.Text)
    inventory_pre_elementary_school = db.Column(db.Text)
    inventory_pre_elementary_dates = db.Column(db.String(255))
    inventory_pre_elementary_awards = db.Column(db.Text)
    inventory_elementary_school = db.Column(db.Text)
    inventory_elementary_dates = db.Column(db.String(255))
    inventory_elementary_awards = db.Column(db.Text)
    inventory_secondary_school = db.Column(db.Text)
    inventory_secondary_dates = db.Column(db.String(255))
    inventory_secondary_awards = db.Column(db.Text)
    inventory_vocational_school = db.Column(db.Text)
    inventory_vocational_dates = db.Column(db.String(255))
    inventory_vocational_awards = db.Column(db.Text)
    inventory_father_name = db.Column(db.String(255))
    inventory_father_age = db.Column(db.String(50))
    inventory_father_status = db.Column(db.String(255))
    inventory_father_education = db.Column(db.Text)
    inventory_father_occupation = db.Column(db.Text)
    inventory_father_contact = db.Column(db.String(255))
    inventory_mother_name = db.Column(db.String(255))
    inventory_mother_age = db.Column(db.String(50))
    inventory_mother_status = db.Column(db.String(255))
    inventory_mother_education = db.Column(db.Text)
    inventory_mother_occupation = db.Column(db.Text)
    inventory_mother_contact = db.Column(db.String(255))
    inventory_parents_marital_status = db.Column(db.String(255))
    inventory_number_of_children = db.Column(db.String(50))
    inventory_number_of_brothers = db.Column(db.String(50))
    inventory_number_of_sisters = db.Column(db.String(50))
    inventory_guardian_name = db.Column(db.String(255))
    inventory_guardian_relationship = db.Column(db.Text)
    inventory_guardian_contact = db.Column(db.String(255))
    inventory_family_income = db.Column(db.String(255))
    inventory_favorite_colors = db.Column(db.Text)
    inventory_favorite_sports = db.Column(db.Text)
    inventory_favorite_foods = db.Column(db.Text)
    inventory_family_description = db.Column(db.Text)
    inventory_emergency_contact_name = db.Column(db.String(255))
    inventory_emergency_contact_number = db.Column(db.String(255))
    inventory_emergency_contact_relationship = db.Column(db.String(255))
    enrollment_good_moral_file = db.Column(db.LargeBinary)
    enrollment_good_moral_filename = db.Column(db.String(255))
    enrollment_good_moral_mimetype = db.Column(db.String(100))
    enrollment_psa_birth_file = db.Column(db.LargeBinary)
    enrollment_psa_birth_filename = db.Column(db.String(255))
    enrollment_psa_birth_mimetype = db.Column(db.String(100))
    enrollment_photos_2x2_file = db.Column(db.LargeBinary)
    enrollment_photos_2x2_filename = db.Column(db.String(255))
    enrollment_photos_2x2_mimetype = db.Column(db.String(100))
    enrollment_entrance_fee_proof_file = db.Column(db.LargeBinary)
    enrollment_entrance_fee_proof_filename = db.Column(db.String(255))
    enrollment_entrance_fee_proof_mimetype = db.Column(db.String(100))
    enrollment_voters_id_file = db.Column(db.LargeBinary)
    enrollment_voters_id_filename = db.Column(db.String(255))
    enrollment_voters_id_mimetype = db.Column(db.String(100))
    enrollment_cbs_file = db.Column(db.LargeBinary)
    enrollment_cbs_filename = db.Column(db.String(255))
    enrollment_cbs_mimetype = db.Column(db.String(100))
    enrollment_brgy_cert_file = db.Column(db.LargeBinary)
    enrollment_brgy_cert_filename = db.Column(db.String(255))
    enrollment_brgy_cert_mimetype = db.Column(db.String(100))
    is_approved_by_president = db.Column(db.Boolean, default=False, nullable=False)
    president_approval_date = db.Column(db.DateTime)
    is_approved_by_accounting = db.Column(db.Boolean, default=False, nullable=False)
    accounting_approval_date = db.Column(db.DateTime)
    is_approved_by_dean = db.Column(db.Boolean, default=False, nullable=False)
    dean_approval_date = db.Column(db.DateTime)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id'))
    pwd_id_file = db.Column(db.LargeBinary)
    pwd_id_filename = db.Column(db.String(255))
    pwd_id_mimetype = db.Column(db.String(100))
    is_section_permanent = db.Column(db.Boolean, default=False)
    organization = db.Column(db.String(100))

    @property
    def student_number(self):
        return self.old_student_id

    @property
    def id(self):
        return self.applicant_id

    @property
    def name(self):
        if self.middle_name and self.middle_name.strip():
            return f"{self.last_name}, {self.first_name} {self.middle_name[0]}."
        return f"{self.last_name}, {self.first_name}"

    def calculate_gwa(self, year, semester):
        grades = [g for g in self.grades if g.subject.year_level == year and g.subject.semester == semester]
        total_points, total_units = 0, 0
        for g in grades:
            if g.final_grade_point:
                point = g.final_grade_point
                if point <= 3.0:
                    total_points += point * float(g.subject.units)
                    total_units += float(g.subject.units)
        return round(total_points / total_units, 2) if total_units > 0 else 0.00


class ApplicationMessage(db.Model):
    __tablename__ = 'application_messages'

    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    sender_type = db.Column(db.String(20), nullable=False)
    sender_name = db.Column(db.String(255))
    admin_role = db.Column(db.String(20))
    message_text = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class StudentGrade(db.Model):
    __tablename__ = 'student_grades'

    id = db.Column(db.Integer, primary_key=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('student_users.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    grade = db.Column(db.Numeric(4, 2))
    remarks = db.Column(db.String(20), nullable=False)
    academic_year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), nullable=False)
    percentage = db.Column(db.Integer)

    __table_args__ = (
        db.UniqueConstraint('student_user_id', 'subject_id', 'academic_year', 'semester', name='uq_student_grades'),
    )


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'

    key = db.Column('setting_key', db.String(50), primary_key=True)
    value = db.Column('setting_value', db.String(255), nullable=False)

    def __repr__(self):
        return f'<SystemSetting {self.key}={self.value}>'


class GradingSettings(db.Model):
    __tablename__ = 'grading_settings'

    id = db.Column(db.Integer, primary_key=True)
    active_school_year = db.Column(db.String(20), nullable=False, default='2024-2025')
    active_semester = db.Column(db.String(20), nullable=False, default='1st Semester')


# =========================================================
# TEACHING / SCHEDULING (FACULTY SYSTEM)
# =========================================================

class TeachingLoad(db.Model):
    __tablename__ = 'teaching_loads'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    course_code = db.Column(db.String(20), nullable=False)
    course_description = db.Column(db.String(200))
    section = db.Column(db.String(20), nullable=False)
    day = db.Column(db.String(20), nullable=False)
    time_start = db.Column(db.Time, nullable=False)
    time_end = db.Column(db.Time, nullable=False)
    room = db.Column(db.String(20))
    school_year = db.Column(db.String(9), index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'course_code': self.course_code,
            'course_description': self.course_description,
            'section': self.section,
            'day': self.day,
            'time_start': self.time_start.strftime('%H:%M:%S'),
            'time_end': self.time_end.strftime('%H:%M:%S'),
            'room': self.room,
            'teacher': {'full_name': self.teacher.full_name}
        }

    def __repr__(self):
        return f'<TeachingLoad {self.course_code} - {self.section}>'


class Announcement(db.Model):
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    read_more_link = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), nullable=False)
    image_filename = db.Column(db.String(255))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    timestamp = db.Column(db.DateTime)

    def __repr__(self):
        return f'<Announcement {self.title}>'


class TimeSlot(db.Model):
    __tablename__ = 'time_slots'

    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.Time, nullable=False, unique=True)
    end_time = db.Column(db.Time, nullable=False, unique=True)
    is_default = db.Column(db.Boolean, default=False, nullable=False)

    def __init__(self, start_time, end_time, is_default=False):
        self.start_time = start_time
        self.end_time = end_time
        self.is_default = is_default

    def to_string(self):
        return f"{self.start_time.strftime('%I:%M %p').lstrip('0')}-{self.end_time.strftime('%I:%M %p').lstrip('0')}"

    def __repr__(self):
        return f'<TimeSlot {self.to_string()}>'


class SubstitutionRequest(db.Model):
    __tablename__ = 'substitution_requests'

    id = db.Column(db.Integer, primary_key=True)
    teaching_load_id = db.Column(db.Integer, db.ForeignKey('teaching_loads.id'), nullable=False)
    original_teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    substitute_teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    date_of_absence = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(50), default='pending', nullable=False)
    reason = db.Column(db.Text)
    proof_file = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    teaching_load = db.relationship('TeachingLoad', foreign_keys=[teaching_load_id], backref=db.backref('substitution_requests', cascade="all, delete-orphan"))
    original_teacher = db.relationship('User', foreign_keys=[original_teacher_id])
    substitute_teacher = db.relationship('User', foreign_keys=[substitute_teacher_id])

    def __repr__(self):
        return f'<SubstitutionRequest for {self.teaching_load.course_code} on {self.date_of_absence}>'


class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    substitution_request_id = db.Column(db.Integer, db.ForeignKey('substitution_requests.id'))
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    user = db.relationship('User', backref='notifications')
    substitution_request = db.relationship('SubstitutionRequest', backref=db.backref('notifications', cascade="all, delete-orphan"))

    def __repr__(self):
        return f'<Notification for {self.user.full_name}>'


class Room(db.Model):
    __tablename__ = 'room'

    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(50), unique=True, nullable=False)

    def __repr__(self):
        return f'<Room {self.room_name}>'


# =========================================================
# ASSESSMENTS / ACCOUNTING / PAYMENTS
# =========================================================

class Assessment(db.Model):
    __tablename__ = 'assessments'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    amount_paid = db.Column(db.Numeric(10, 2), default=0.00)
    balance = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(20), default='unpaid')
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    batch_id = db.Column(db.Integer)


class BatchAssessmentLog(db.Model):
    __tablename__ = 'batch_assessments_log'

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(255))
    amount = db.Column(db.Numeric(10, 2))
    target_year = db.Column(db.String(50))
    semester = db.Column(db.String(50))
    course = db.Column(db.String(100))
    year_level = db.Column(db.String(50))
    scope = db.Column(db.String(50))
    student_count = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())


class ChartOfAccount(db.Model):
    __tablename__ = 'chart_of_accounts'

    id = db.Column(db.Integer, primary_key=True)
    account_code = db.Column(db.String(20), nullable=False)
    account_name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(30), nullable=False)
    is_active = db.Column(db.Boolean, default=True)


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    assessment_id = db.Column(db.Integer, db.ForeignKey('assessments.id'))
    amount_paid = db.Column(db.Numeric(10, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.String(50), default='Cash')
    remark = db.Column(db.String(100))
    receipt_number = db.Column(db.String(50))
    cashier_username = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class OrganizationDiscount(db.Model):
    __tablename__ = 'organization_discounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    discount_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)


class StudentDiscountApplied(db.Model):
    __tablename__ = 'student_discounts_applied'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    org_discount_id = db.Column(db.Integer, db.ForeignKey('organization_discounts.id'), nullable=False)
    date_applied = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    applied_by = db.Column(db.String(100))


class VoucherReturn(db.Model):
    __tablename__ = 'voucher_returns'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    school_year = db.Column(db.String(50))
    semester = db.Column(db.String(50))
    returned_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    returned_by = db.Column(db.String(100))
    amount = db.Column(db.Numeric(10, 2), default=0.00)


class JournalEntry(db.Model):
    __tablename__ = 'journal_entries'

    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.Text)
    reference_number = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    created_by = db.Column(db.Integer)


class JournalLine(db.Model):
    __tablename__ = 'journal_lines'

    id = db.Column(db.Integer, primary_key=True)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('chart_of_accounts.id'), nullable=False)
    debit = db.Column(db.Numeric(15, 2), default=0.00)
    credit = db.Column(db.Numeric(15, 2), default=0.00)


class StudentAssessment(db.Model):
    __tablename__ = 'student_assessments'

    id = db.Column(db.Integer, primary_key=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('student_users.id'), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    balance = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    status = db.Column(db.String(20), default='unpaid')
    academic_year = db.Column(db.String(20))
    semester = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class StudentPayment(db.Model):
    __tablename__ = 'student_payments'

    id = db.Column(db.Integer, primary_key=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('student_users.id'), nullable=False)
    assessment_id = db.Column(db.Integer, db.ForeignKey('student_assessments.id'))
    receipt_number = db.Column(db.String(60), unique=True, nullable=False)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.String(50))
    remark = db.Column(db.String(100))
    cashier_username = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class FacultyAssignment(db.Model):
    __tablename__ = 'faculty_assignments'

    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.Integer, db.ForeignKey('system_users.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=False)

    p_max_q1 = db.Column(db.Integer, default=10); p_max_q2 = db.Column(db.Integer, default=10); p_max_q3 = db.Column(db.Integer, default=0)
    p_max_q4 = db.Column(db.Integer, default=0); p_max_q5 = db.Column(db.Integer, default=0)
    p_max_r = db.Column(db.Integer, default=100)
    p_max_act1 = db.Column(db.Integer, default=50); p_max_act2 = db.Column(db.Integer, default=50); p_max_act3 = db.Column(db.Integer, default=50)
    p_max_act4 = db.Column(db.Integer, default=0); p_max_act5 = db.Column(db.Integer, default=0)
    p_max_exam = db.Column(db.Integer, default=100)
    m_max_q1 = db.Column(db.Integer, default=10); m_max_q2 = db.Column(db.Integer, default=0); m_max_q3 = db.Column(db.Integer, default=0)
    m_max_q4 = db.Column(db.Integer, default=0); m_max_q5 = db.Column(db.Integer, default=0)
    m_max_r = db.Column(db.Integer, default=100)
    m_max_act1 = db.Column(db.Integer, default=50); m_max_act2 = db.Column(db.Integer, default=0); m_max_act3 = db.Column(db.Integer, default=0)
    m_max_act4 = db.Column(db.Integer, default=0); m_max_act5 = db.Column(db.Integer, default=0)
    m_max_exam = db.Column(db.Integer, default=100)
    f_max_q1 = db.Column(db.Integer, default=10); f_max_q2 = db.Column(db.Integer, default=0); f_max_q3 = db.Column(db.Integer, default=0)
    f_max_q4 = db.Column(db.Integer, default=0); f_max_q5 = db.Column(db.Integer, default=0)
    f_max_r = db.Column(db.Integer, default=100)
    f_max_act1 = db.Column(db.Integer, default=50); f_max_act2 = db.Column(db.Integer, default=50); f_max_act3 = db.Column(db.Integer, default=50)
    f_max_act4 = db.Column(db.Integer, default=0); f_max_act5 = db.Column(db.Integer, default=0)
    f_max_exam = db.Column(db.Integer, default=100)

    faculty = db.relationship('SystemUser', backref='assignments')
    subject = db.relationship('Subject', backref='assignments')
    section = db.relationship('Section', backref='assignments')

    @property
    def p_total_max_q(self): return (self.p_max_q1 or 0) + (self.p_max_q2 or 0) + (self.p_max_q3 or 0) + (self.p_max_q4 or 0) + (self.p_max_q5 or 0)
    @property
    def p_total_max_a(self): return (self.p_max_act1 or 0) + (self.p_max_act2 or 0) + (self.p_max_act3 or 0) + (self.p_max_act4 or 0) + (self.p_max_act5 or 0)
    @property
    def m_total_max_q(self): return (self.m_max_q1 or 0) + (self.m_max_q2 or 0) + (self.m_max_q3 or 0) + (self.m_max_q4 or 0) + (self.m_max_q5 or 0)
    @property
    def m_total_max_a(self): return (self.m_max_act1 or 0) + (self.m_max_act2 or 0) + (self.m_max_act3 or 0) + (self.m_max_act4 or 0) + (self.m_max_act5 or 0)
    @property
    def f_total_max_q(self): return (self.f_max_q1 or 0) + (self.f_max_q2 or 0) + (self.f_max_q3 or 0) + (self.f_max_q4 or 0) + (self.f_max_q5 or 0)
    @property
    def f_total_max_a(self): return (self.f_max_act1 or 0) + (self.f_max_act2 or 0) + (self.f_max_act3 or 0) + (self.f_max_act4 or 0) + (self.f_max_act5 or 0)


class Grade(db.Model):
    __tablename__ = 'grades'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey('system_users.id'), nullable=False)

    p_q1 = db.Column(db.Float, default=0); p_q2 = db.Column(db.Float, default=0); p_q3 = db.Column(db.Float, default=0)
    p_q4 = db.Column(db.Float, default=0); p_q5 = db.Column(db.Float, default=0)
    p_act1 = db.Column(db.Float, default=0); p_act2 = db.Column(db.Float, default=0); p_act3 = db.Column(db.Float, default=0)
    p_act4 = db.Column(db.Float, default=0); p_act5 = db.Column(db.Float, default=0)
    p_recit = db.Column(db.Float, default=0); p_exam = db.Column(db.Float, default=0)
    m_q1 = db.Column(db.Float, default=0); m_q2 = db.Column(db.Float, default=0); m_q3 = db.Column(db.Float, default=0)
    m_q4 = db.Column(db.Float, default=0); m_q5 = db.Column(db.Float, default=0)
    m_act1 = db.Column(db.Float, default=0); m_act2 = db.Column(db.Float, default=0); m_act3 = db.Column(db.Float, default=0)
    m_act4 = db.Column(db.Float, default=0); m_act5 = db.Column(db.Float, default=0)
    m_recit = db.Column(db.Float, default=0); m_exam = db.Column(db.Float, default=0)
    f_q1 = db.Column(db.Float, default=0); f_q2 = db.Column(db.Float, default=0); f_q3 = db.Column(db.Float, default=0)
    f_q4 = db.Column(db.Float, default=0); f_q5 = db.Column(db.Float, default=0)
    f_act1 = db.Column(db.Float, default=0); f_act2 = db.Column(db.Float, default=0); f_act3 = db.Column(db.Float, default=0)
    f_act4 = db.Column(db.Float, default=0); f_act5 = db.Column(db.Float, default=0)
    f_recit = db.Column(db.Float, default=0); f_exam = db.Column(db.Float, default=0)

    prelim_grade = db.Column(db.Float)
    midterm_grade = db.Column(db.Float)
    midterm_standing = db.Column(db.Float)
    final_term_grade = db.Column(db.Float)
    final_grade = db.Column(db.Float)
    final_grade_point = db.Column(db.Float)

    student = db.relationship('Applicant', backref=db.backref('grades', lazy=True))
    subject = db.relationship('Subject', backref=db.backref('grades', lazy=True))
    faculty = db.relationship('SystemUser', backref=db.backref('grades', lazy=True))

    @property
    def p_quiz_total(self): return (self.p_q1 or 0) + (self.p_q2 or 0) + (self.p_q3 or 0) + (self.p_q4 or 0) + (self.p_q5 or 0)
    @property
    def p_act_total(self): return (self.p_act1 or 0) + (self.p_act2 or 0) + (self.p_act3 or 0) + (self.p_act4 or 0) + (self.p_act5 or 0)
    @property
    def m_quiz_total(self): return (self.m_q1 or 0) + (self.m_q2 or 0) + (self.m_q3 or 0) + (self.m_q4 or 0) + (self.m_q5 or 0)
    @property
    def m_act_total(self): return (self.m_act1 or 0) + (self.m_act2 or 0) + (self.m_act3 or 0) + (self.m_act4 or 0) + (self.m_act5 or 0)
    @property
    def f_quiz_total(self): return (self.f_q1 or 0) + (self.f_q2 or 0) + (self.f_q3 or 0) + (self.f_q4 or 0) + (self.f_q5 or 0)
    @property
    def f_act_total(self): return (self.f_act1 or 0) + (self.f_act2 or 0) + (self.f_act3 or 0) + (self.f_act4 or 0) + (self.f_act5 or 0)
    @property
    def remarks(self):
        if self.final_grade_point is None:
            return "Pending"
        return "Passed" if self.final_grade_point <= 3.0 else "Failed"


# =========================================================
# WEBSITE CONTENT
# =========================================================

class PageContent(db.Model):
    __tablename__ = 'page_content'

    content_key = db.Column(db.String(50), primary_key=True)
    content_value = db.Column(db.Text)


class HeroSlide(db.Model):
    __tablename__ = 'hero_slides'

    id = db.Column(db.Integer, primary_key=True)
    image_filename = db.Column(db.String(255))
    intro_text = db.Column(db.String(255))
    title_text = db.Column(db.String(255))
    highlight_text = db.Column(db.String(255))
    button_text = db.Column(db.String(50), default='Learn More')
    button_link = db.Column(db.String(255), default='#')
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class NewsArticle(db.Model):
    __tablename__ = 'news_articles'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    publish_date = db.Column(db.Date)
    image_filename = db.Column(db.String(255))
    description = db.Column(db.Text)
    read_more_link = db.Column(db.String(255))


class FacultyMember(db.Model):
    __tablename__ = 'faculty'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(255), nullable=False)
    image_filename = db.Column(db.String(255))
    category = db.Column(db.String(20), nullable=False)


class FAQ(db.Model):
    __tablename__ = 'faqs'

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class StatusRequirement(db.Model):
    __tablename__ = 'status_requirements'

    id = db.Column(db.Integer, primary_key=True)
    status_key = db.Column(db.String(50), nullable=False)
    requirement_text = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class StatusUpload(db.Model):
    __tablename__ = 'status_uploads'

    id = db.Column(db.Integer, primary_key=True)
    status_key = db.Column(db.String(50), nullable=False)
    program_id = db.Column(db.String(50))
    file_filename = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(50))
    uploaded_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())


class SocialMediaLink(db.Model):
    __tablename__ = 'social_media_links'

    id = db.Column(db.Integer, primary_key=True)
    platform_name = db.Column(db.String(50), nullable=False)
    url = db.Column(db.String(255), nullable=False)
    icon_class = db.Column(db.String(50), nullable=False)


class StudentDownload(db.Model):
    __tablename__ = 'student_downloads'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    target_program = db.Column(db.String(100), default='All')
    target_year_level = db.Column(db.String(50), default='All')
    target_section = db.Column(db.String(100), default='All')


class ReceiptLayoutSetting(db.Model):
    __tablename__ = 'receipt_layout_settings'

    field_id = db.Column(db.String(50), primary_key=True)
    top_pos = db.Column(db.String(20), default='0%')
    left_pos = db.Column(db.String(20), default='0%')
    font_size = db.Column(db.String(10), default='14px')
    is_visible = db.Column(db.Boolean, default=True)
