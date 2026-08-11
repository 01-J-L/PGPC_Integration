# app/forms.py

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, BooleanField, IntegerField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError, Optional
from app.models import User, Student, Program
from flask_wtf.file import FileField, FileRequired, FileAllowed

# --- Faculty Registration Form ---
class RegistrationForm(FlaskForm):
    name = StringField('Full Name', validators=[DataRequired(), Length(min=2, max=100)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    
    # Faculty Designation Logic
    faculty_type = SelectField('Faculty Designation', validators=[DataRequired()], choices=[
        ('', 'Select Designation'),
        ('minor', 'Minor / General Education Instructor'),
        ('major', 'Major Subject Instructor')
    ])
    
    # Optional because Minor profs won't select this. We handle logic in routes.
    program_id = SelectField('Department / Program', validators=[Optional()], choices=[]) 

    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Register')

    def __init__(self, *args, **kwargs):
        super(RegistrationForm, self).__init__(*args, **kwargs)
        # Populate programs dynamically from DB
        self.program_id.choices = [("", "Select Department")] + [(str(p.id), p.code) for p in Program.query.order_by(Program.code).all()]

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('That email is already taken. Please choose a different one.')

# --- Student Registration Form ---
class StudentRegistrationForm(FlaskForm):
    student_id = StringField('Student ID', validators=[DataRequired()])
    first_name = StringField('First Name', validators=[DataRequired()])
    middle_name = StringField('Middle Name (Optional)')
    last_name = StringField('Last Name', validators=[DataRequired()])
    program_id = SelectField('Program/Department', validators=[DataRequired(message="Please select a program.")])
    section = StringField('Section', validators=[DataRequired()])
    contact_info = StringField('Contact Info (Phone)', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Register')

    def __init__(self, *args, **kwargs):
        super(StudentRegistrationForm, self).__init__(*args, **kwargs)
        self.program_id.choices = [("", "Select Program")] + [(str(p.id), p.code) for p in Program.query.order_by(Program.code).all()]

    def validate_student_id(self, student_id):
        student = Student.query.filter_by(student_number=student_id.data).first()
        if student:
            raise ValidationError('That Student ID is already registered.')

    def validate_email(self, email):
        student = Student.query.filter_by(contact_info_email=email.data).first()
        if student:
            raise ValidationError('That email is already taken. Please choose a different one.')

# --- Login & Auth Forms ---
class OTPVerificationForm(FlaskForm):
    otp = StringField('Verification Code', validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField('Verify')

class AdminLoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

class StudentLoginForm(FlaskForm):
    email = StringField('Student Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

class FacultyLoginForm(FlaskForm):
    email = StringField('Faculty Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')

# --- NEW FORMS FOR PROGRAM MANAGEMENT ---

class ProgramForm(FlaskForm):
    code = StringField('Program Code', validators=[DataRequired(), Length(max=10)])
    name = StringField('Program Name', validators=[DataRequired(), Length(max=100)])
    submit = SubmitField('Save Program')

    def validate_code(self, code):
        # Only check uniqueness if it's a new entry (simplified logic)
        program = Program.query.filter_by(code=code.data).first()
        if program:
            raise ValidationError('This Program Code already exists.')

class SubjectForm(FlaskForm):
    code = StringField('Subject Code', validators=[DataRequired(), Length(max=20)])
    description = StringField('Description', validators=[DataRequired(), Length(max=200)])
    units = IntegerField('Units', validators=[DataRequired()])
    year = SelectField('Year Level', validators=[DataRequired()], choices=[(1, '1st Year'), (2, '2nd Year'), (3, '3rd Year'), (4, '4th Year')], coerce=int)
    semester = SelectField('Semester', validators=[DataRequired()], choices=[(1, '1st Semester'), (2, '2nd Semester')], coerce=int)
    submit = SubmitField('Save Subject')

class SettingsForm(FlaskForm):
    active_school_year = StringField('Active School Year', validators=[DataRequired()], render_kw={"placeholder": "e.g. 2024-2025"})
    active_semester = SelectField('Active Semester', validators=[DataRequired()], choices=[
        ('1st Semester', '1st Semester'), 
        ('2nd Semester', '2nd Semester')
    ])
    submit = SubmitField('Save Settings')


class AdminAssignmentForm(FlaskForm):
    program = SelectField('Program', validators=[DataRequired()])
    year = SelectField('Year', validators=[DataRequired()], choices=[('', 'Select Year'), ('1', '1st'), ('2', '2nd'), ('3', '3rd'), ('4', '4th')])
    semester = SelectField('Semester', validators=[DataRequired()], choices=[('', 'Select Semester'), ('1', '1st'), ('2', '2nd')])
    section = SelectField('Section', validators=[DataRequired()])
    subject = SelectField('Subject', validators=[DataRequired()])
    submit = SubmitField('Assign Subject')