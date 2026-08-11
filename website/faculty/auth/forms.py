# app/auth/forms.py

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField, SelectField, TextAreaField, IntegerField
from wtforms.validators import DataRequired, Email, EqualTo, Optional
from wtforms.validators import Length
from wtforms_sqlalchemy.fields import QuerySelectField
from website.faculty.models import Program


def program_query():
    return Program.query.order_by(Program.program_name).all()   

class VerifyOTPForm(FlaskForm):
    otp = StringField('Verification Code', validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField('Verify Account')
# Single login form for both faculty and deans
class LoginForm(FlaskForm):
    email = StringField('Email / Admin Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')


# Form for FACULTY registration


class RegistrationForm(FlaskForm):
    full_name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    
    # NEW: Contact Number
    contact_number = StringField('Contact Number', validators=[DataRequired(), Length(min=11, max=13)])
    
    password = PasswordField('Password', validators=[DataRequired()])
    password2 = PasswordField('Repeat Password', validators=[DataRequired(), EqualTo('password')])
    
    baccalaureate_degree = StringField('Baccalaureate Degree', validators=[DataRequired()])
    baccalaureate_year_grad = IntegerField('Year Graduated (Baccalaureate)', validators=[DataRequired()])
    highest_edu_attainment = StringField('Highest Educational Attainment', validators=[Optional()])
    highest_edu_year_grad = StringField('Year Graduated (Highest Degree)', validators=[Optional()])
    industry_experience = TextAreaField('Industry Experience (separate with commas)', validators=[Optional()])
    is_licensed = SelectField('Licensed Professional?', choices=[('Yes', 'Yes'), ('No', 'No')], validators=[DataRequired()])
    
    # REMOVED: major = TextAreaField(...)
    
    # UPDATED CHOICES
    expertise_type = SelectField('Expertise Type', choices=[
        ('', '-- Select Type --'),
        ('PROFED', 'PROFED'),
        ('GENED', 'GENED')
    ], validators=[DataRequired()])
    
    major_program = QuerySelectField('Major Program', 
                                     query_factory=program_query, 
                                     get_label='program_name', 
                                     allow_blank=True, 
                                     blank_text='-- Select Program --',
                                     validators=[Optional()])

    submit = SubmitField('Register Account')

class AdminAddUserForm(FlaskForm):
    full_name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    
    # REMOVED: contact_number field
    
    password = PasswordField('Password', validators=[DataRequired()])
    username = StringField('Admin Username (if role is Dean)', validators=[Optional()])
    role = SelectField('Role', choices=[('faculty', 'Faculty'), ('dean', 'Dean')], validators=[DataRequired()])
    
    expertise_type = SelectField('Expertise Type (for Faculty)', choices=[
        ('', '-- Select Type --'),
        ('PROFED', 'PROFED'),
        ('GENED', 'GENED')
    ], validators=[Optional()])

    program = QuerySelectField('Program (if PROFED)', 
                               query_factory=lambda: Program.query.order_by(Program.program_code).all(), 
                               get_label='program_code', 
                               allow_blank=True, 
                               blank_text='-- Select Program --', 
                               validators=[Optional()])

    submit = SubmitField('Create User')

# Form to request a password reset email
class PasswordResetRequestForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Request Password Reset')

# Form to actually reset the password
class PasswordResetForm(FlaskForm):
    password = PasswordField('New Password', validators=[DataRequired()])
    password2 = PasswordField('Repeat New Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Reset Password')
class UserFilterForm(FlaskForm):
    role = SelectField('Filter by Role', choices=[
        ('', 'All Roles'),
        ('faculty', 'Faculty'),
        ('dean', 'Dean')
    ], default='')

    department = SelectField('Filter by Department')
    
    submit = SubmitField('Apply Filter')