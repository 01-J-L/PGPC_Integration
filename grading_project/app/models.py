# website/faculty/models.py (or your grading system models path)

from app import db, login_manager, bcrypt
from flask_login import UserMixin
from sqlalchemy import event, text
from sqlalchemy.ext.hybrid import hybrid_property

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(db.Model, UserMixin):
    __tablename__ = 'system_users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), name='password') # maps to 'password' column
    role = db.Column(db.String(20), nullable=False, default='faculty')
    full_name = db.Column(db.String(100), nullable=False) # maps to 'full_name' column
    email = db.Column(db.String(100), nullable=True)
    can_edit = db.Column(db.Boolean, default=True)
    department = db.Column(db.String(100))

    # Compatibility properties for legacy templates and routes
    @hybrid_property
    def name(self):
        return self.full_name

    @name.setter
    def name(self, value):
        self.full_name = value

    @name.expression
    def name(cls):
        return cls.full_name

    @property
    def is_approved(self):
        return True # Handled natively in system_users roles
        
    @is_approved.setter
    def is_approved(self, value):
        pass

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        if not self.password_hash:
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except ValueError:
            pass
        return bcrypt.check_password_hash(self.password_hash, password)


class Student(db.Model):
    __tablename__ = 'applicants'
    
    id = db.Column(db.Integer, name='applicant_id', primary_key=True)
    student_number = db.Column(db.String(50), name='old_student_id', unique=True, nullable=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    middle_name = db.Column(db.String(100), nullable=True)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=True)
    section = db.relationship('Section', backref='students')
    student_user_id = db.Column(db.Integer, nullable=True)

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


class Program(db.Model):
    __tablename__ = 'programs'
    
    program_id = db.Column(db.String(10), primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    program_code = db.Column(db.String(20), nullable=True)
    program_name = db.Column(db.String(200), nullable=True)
    total_units = db.Column(db.Integer, nullable=True)

    @hybrid_property
    def id(self):
        return self.program_id

    @id.expression
    def id(cls):
        return cls.program_id

    @hybrid_property
    def code(self):
        return self.program_code

    @code.expression
    def code(cls):
        return cls.program_code

    @hybrid_property
    def name(self):
        return self.title

    @name.expression
    def name(cls):
        return cls.title

    subjects = db.relationship('Subject', backref='program', lazy='select')


class Section(db.Model):
    __tablename__ = 'sections'
    
    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.String(10), db.ForeignKey('programs.program_id'), nullable=False)
    year_level = db.Column(db.String(20), nullable=False)
    section_name = db.Column(db.String(50), nullable=False)
    section_letter = db.Column(db.String(5), nullable=False)
    max_students = db.Column(db.Integer, nullable=True, default=60)
    current_students = db.Column(db.Integer, nullable=True, default=0)
    is_active = db.Column(db.Boolean, nullable=True, default=True)
    semester = db.Column(db.Integer, nullable=True)
    school_year = db.Column(db.String(9), nullable=True)
    program = db.relationship('Program', backref='sections')

    @hybrid_property
    def name(self):
        return self.section_name

    @name.expression
    def name(cls):
        return cls.section_name


class Subject(db.Model):
    __tablename__ = 'subjects'
    
    id = db.Column(db.Integer, primary_key=True)
    subject_code = db.Column(db.String(20), nullable=False)
    subject_title = db.Column(db.String(200), nullable=False)
    units = db.Column(db.Integer, nullable=False)
    year_level = db.Column(db.Integer, nullable=False)
    semester = db.Column(db.Integer, nullable=False)
    program_id = db.Column(db.String(10), db.ForeignKey('programs.program_id'), nullable=False)
    
    # Relationship for Prerequisites
    subject_prerequisites = db.Table('subject_prerequisites',
        db.Column('subject_id', db.Integer, db.ForeignKey('subjects.id'), primary_key=True),
        db.Column('prerequisite_id', db.Integer, db.ForeignKey('subjects.id'), primary_key=True)
    )
    
    prerequisites = db.relationship(
        'Subject', 
        secondary=subject_prerequisites,
        primaryjoin=(subject_prerequisites.c.subject_id == id),
        secondaryjoin=(subject_prerequisites.c.prerequisite_id == id),
        backref=db.backref('required_for', lazy='dynamic'),
        lazy='dynamic'
    )

    @hybrid_property
    def code(self):
        return self.subject_code

    @code.expression
    def code(cls):
        return cls.subject_code

    @hybrid_property
    def description(self):
        return self.subject_title

    @description.expression
    def description(cls):
        return cls.subject_title

    @hybrid_property
    def year(self):
        return self.year_level

    @year.expression
    def year(cls):
        return cls.year_level


class FacultyAssignment(db.Model):
    __tablename__ = 'faculty_assignments'
    
    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.Integer, db.ForeignKey('system_users.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=False)
    
    # Prelim max scores
    p_max_q1 = db.Column(db.Integer, default=10); p_max_q2 = db.Column(db.Integer, default=10); p_max_q3 = db.Column(db.Integer, default=0)
    p_max_q4 = db.Column(db.Integer, default=0);  p_max_q5 = db.Column(db.Integer, default=0)
    p_max_r = db.Column(db.Integer, default=100)
    p_max_act1 = db.Column(db.Integer, default=50); p_max_act2 = db.Column(db.Integer, default=50); p_max_act3 = db.Column(db.Integer, default=50)
    p_max_act4 = db.Column(db.Integer, default=0);  p_max_act5 = db.Column(db.Integer, default=0)
    p_max_exam = db.Column(db.Integer, default=100)

    # Midterm max scores
    m_max_q1 = db.Column(db.Integer, default=10); m_max_q2 = db.Column(db.Integer, default=0); m_max_q3 = db.Column(db.Integer, default=0)
    m_max_q4 = db.Column(db.Integer, default=0);  m_max_q5 = db.Column(db.Integer, default=0)
    m_max_r = db.Column(db.Integer, default=100)
    m_max_act1 = db.Column(db.Integer, default=50); m_max_act2 = db.Column(db.Integer, default=0); m_max_act3 = db.Column(db.Integer, default=0)
    m_max_act4 = db.Column(db.Integer, default=0);  m_max_act5 = db.Column(db.Integer, default=0)
    m_max_exam = db.Column(db.Integer, default=100)

    # Finals max scores
    f_max_q1 = db.Column(db.Integer, default=10); f_max_q2 = db.Column(db.Integer, default=0); f_max_q3 = db.Column(db.Integer, default=0)
    f_max_q4 = db.Column(db.Integer, default=0);  f_max_q5 = db.Column(db.Integer, default=0)
    f_max_r = db.Column(db.Integer, default=100)
    f_max_act1 = db.Column(db.Integer, default=50); f_max_act2 = db.Column(db.Integer, default=50); f_max_act3 = db.Column(db.Integer, default=50)
    f_max_act4 = db.Column(db.Integer, default=0);  f_max_act5 = db.Column(db.Integer, default=0)
    f_max_exam = db.Column(db.Integer, default=100)

    faculty = db.relationship('User', backref='assignments')
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


class SystemSettings(db.Model):
    __tablename__ = 'grading_settings'
    id = db.Column(db.Integer, primary_key=True)
    active_school_year = db.Column(db.String(20), nullable=False, default='2024-2025')
    active_semester = db.Column(db.String(20), nullable=False, default='1st Semester')


class Grade(db.Model):
    __tablename__ = 'grades'
    
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('applicants.applicant_id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey('system_users.id'), nullable=False)

    # Raw scores
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

    # Calculated outputs
    prelim_grade = db.Column(db.Float)
    midterm_grade = db.Column(db.Float)
    midterm_standing = db.Column(db.Float)
    final_term_grade = db.Column(db.Float)
    final_grade = db.Column(db.Float)
    final_grade_point = db.Column(db.Float)

    student = db.relationship('Student', backref=db.backref('grades', lazy=True))
    subject = db.relationship('Subject', backref=db.backref('grades', lazy=True))
    faculty = db.relationship('User', backref=db.backref('grades', lazy=True))

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
        if self.final_grade_point is None: return "Pending"
        return "Passed" if self.final_grade_point <= 3.0 else "Failed"


# --- AUTOMATIC SYNCHRONIZATION WITH THE STUDENT SYSTEM ---
@event.listens_for(Grade, 'after_insert')
@event.listens_for(Grade, 'after_update')
def sync_computed_grades_to_student_portal(mapper, connection, target):
    """
    Listens for updates/saves on the detailed Grade model and automatically 
    synchronizes the final computed values to the shared 'student_grades' table.
    """
    # Fetch applicant details through target id to obtain student_user_id
    student = Student.query.get(target.student_id)
    if not student or not student.student_user_id:
        return

    # Grab the active year/semester context
    settings = SystemSettings.query.first()
    ay = settings.active_school_year if settings else "2024-2025"
    sem = settings.active_semester if settings else "1st Semester"

    remarks = "Passed" if (target.final_grade_point and target.final_grade_point <= 3.0) else "Failed"
    if target.final_grade_point is None:
        remarks = "Incomplete"

    # Upsert query using native MySQL command
    stmt = text("""
        INSERT INTO student_grades (student_user_id, subject_id, grade, percentage, remarks, academic_year, semester, created_at, updated_at)
        VALUES (:student_user_id, :subject_id, :grade, :percentage, :remarks, :academic_year, :semester, NOW(), NOW())
        ON DUPLICATE KEY UPDATE
            grade = VALUES(grade),
            percentage = VALUES(percentage),
            remarks = VALUES(remarks),
            academic_year = VALUES(academic_year),
            semester = VALUES(semester),
            updated_at = NOW()
    """)
    connection.execute(stmt, {
        'student_user_id': student.student_user_id,
        'subject_id': target.subject_id,
        'grade': target.final_grade_point,
        'percentage': int(target.final_grade) if target.final_grade else None,
        'remarks': remarks,
        'academic_year': ay,
        'semester': sem
    })