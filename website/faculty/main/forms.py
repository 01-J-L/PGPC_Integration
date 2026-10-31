from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, SubmitField, TimeField, DateField, TextAreaField, HiddenField, FloatField
from wtforms.validators import DataRequired, Email, Optional, Length
from wtforms_sqlalchemy.fields import QuerySelectField
from website.faculty.models import Program, User
from wtforms import BooleanField, IntegerField
from flask_wtf.file import FileField, FileRequired, FileAllowed


def faculty_query():
    return User.query.filter_by(role='faculty', status='active').order_by(User.full_name).all()

def program_query():
    return Program.query.order_by(Program.program_name).all()

class ScheduleForm(FlaskForm):
    program = QuerySelectField('Program', query_factory=program_query, get_label='program_name', allow_blank=True, blank_text='-- Select a Program --', validators=[DataRequired()])
    course = SelectField('Course', choices=[], validators=[DataRequired()])
    
    # CHANGED: This is now a SelectField (Dropdown)
    section = SelectField('Section', choices=[], validators=[DataRequired()])
    room = SelectField('Room', choices=[], validators=[DataRequired()]) 
    room = SelectField('Room', choices=[], validators=[DataRequired()])
    time_start = StringField('Start Time', validators=[DataRequired()])
    time_end = StringField('End Time', validators=[DataRequired()])
    
    faculty = QuerySelectField('Faculty', query_factory=faculty_query, get_label='full_name', allow_blank=True, blank_text='-- Select Faculty --', validators=[DataRequired()])

    day = HiddenField('Day', validators=[DataRequired()])
    submit = SubmitField('Add Schedule')


class DeleteScheduleForm(FlaskForm):
    submit = SubmitField('Delete')

class AddTimeSlotForm(FlaskForm):
    new_start_time = TimeField('Start Time', format='%H:%M', validators=[DataRequired()])
    new_end_time = TimeField('End Time', format='%H:%M', validators=[DataRequired()])
    submit = SubmitField('Add Time Slot')


class SubstitutionRequestForm(FlaskForm):
    date_of_absence = DateField('Date of Absence', validators=[DataRequired()], format='%Y-%m-%d')
    reason = TextAreaField('Reason (Optional)')
    submit = SubmitField('Notify Qualified Faculty')

class SystemSettingsForm(FlaskForm):
    # 1. Global Setting (Semester)
    current_semester = SelectField('Current Active Semester', choices=[('1', '1st Semester'), ('2', '2nd Semester')], validators=[DataRequired()])
    
    # --- NEW: Semester Specific Dates ---
    sem_start_date = DateField('Sem Start', format='%Y-%m-%d', validators=[Optional()])
    sem_end_date = DateField('Sem End', format='%Y-%m-%d', validators=[Optional()])
    # ------------------------------------

    # Existing School Year Dates
    active_start_date = DateField('SY Start', format='%Y-%m-%d', validators=[Optional()])
    active_end_date = DateField('SY End', format='%Y-%m-%d', validators=[Optional()])

    # 2. Switch Active Year
    select_year = SelectField('Switch Active Year', choices=[], validators=[Optional()])
    submit_switch = SubmitField('Update Active Settings')

    # 3. Create New Year
    new_year = StringField('New School Year', validators=[Optional()], render_kw={"placeholder": "e.g. 2026-2027"})
    
    start_date = DateField('Start Date', format='%Y-%m-%d', validators=[Optional()])
    end_date = DateField('End Date', format='%Y-%m-%d', validators=[Optional()])

    submit_create = SubmitField('Create & Set Active')

class EditProfileForm(FlaskForm):
    full_name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    
    # NEW: Contact Number
    contact_number = StringField('Contact Number', validators=[DataRequired(), Length(min=11, max=13)])
    
    baccalaureate_degree = StringField('Baccalaureate Degree')
    baccalaureate_year_grad = IntegerField('Year Graduated (Baccalaureate)', validators=[Optional()])
    highest_edu_attainment = StringField('Highest Educational Attainment')
    highest_edu_year_grad = StringField('Year Graduated (Highest)')
    
    # REMOVED: major = StringField(...)
    
    is_licensed = BooleanField('Is Licensed?')
    subjects_taught = TextAreaField('Subjects Taught (comma-separated)')
    
    # UPDATED CHOICES
    expertise_type = SelectField('Expertise Type', choices=[('PROFED', 'PROFED'), ('GENED', 'GENED')], validators=[DataRequired()])
    
    major_program = QuerySelectField('Major Program', query_factory=program_query, get_label='program_name', allow_blank=True, blank_text='-- Select Program --', validators=[Optional()])
    submit = SubmitField('Save Changes')

class ProgramForm(FlaskForm):
    program_code = StringField('Program Code', validators=[DataRequired(), Length(max=20)])
    program_name = StringField('Program Name', validators=[DataRequired(), Length(max=200)])
    submit = SubmitField('Save Program')

class FileUploadForm(FlaskForm):
    file = FileField('Curriculum File', validators=[FileRequired(), FileAllowed(['xlsx'], 'Excel files only!')])
    submit = SubmitField('Import Curriculum')

class SchoolYearAnalyticsForm(FlaskForm):
    school_year = SelectField('Select School Year', validators=[DataRequired()])
    submit = SubmitField('Generate Report')

class CreateSectionForm(FlaskForm):
    program = QuerySelectField('Select Program', query_factory=program_query, get_label='program_name', validators=[DataRequired()])
    section_name = StringField('New Section Name', validators=[DataRequired()], render_kw={"placeholder": "e.g., 1C"})
    semester = SelectField('Semester', choices=[('1', '1st Semester'), ('2', '2nd Semester')], validators=[DataRequired()])
    submit = SubmitField('Create Section')

class DeleteSectionForm(FlaskForm):
    submit = SubmitField('Delete')

class SectionGridScheduleForm(FlaskForm):

    faculty = SelectField('Faculty', validators=[DataRequired()])
    course = SelectField('Course', validators=[DataRequired()])
    room = SelectField('Room', choices=[], validators=[DataRequired()])
    time_start = StringField('Start Time', validators=[DataRequired()], render_kw={"placeholder": "e.g., 07:30 AM"})
    time_end = StringField('End Time', validators=[DataRequired()], render_kw={"placeholder": "e.g., 09:00 AM"})
    day = HiddenField('Day', validators=[DataRequired()])
    submit = SubmitField('Add Schedule')


class EditScheduleForm(FlaskForm):
    """A dedicated form for the Edit Schedule modal."""
    schedule_id = HiddenField(validators=[DataRequired()])

    faculty = SelectField('Faculty', validators=[DataRequired()])
    course = SelectField('Course', validators=[DataRequired()])

    room = SelectField('Room', choices=[], validators=[DataRequired()])
    time_start = StringField('Start Time', validators=[DataRequired()])
    time_end = StringField('End Time', validators=[DataRequired()])
    submit = SubmitField('Update Schedule')

class CourseForm(FlaskForm):
    year = IntegerField('Year', validators=[DataRequired()])
    semester = SelectField('Semester', choices=[('1', '1st Semester'), ('2', '2nd Semester')], validators=[DataRequired()])
    course_code = StringField('Course Code', validators=[DataRequired(), Length(max=20)])
    course_description = StringField('Course Description', validators=[DataRequired(), Length(max=200)])
    units = IntegerField('Units', validators=[DataRequired()])
    submit = SubmitField('Add Course')

class RoomForm(FlaskForm):
    room_name = StringField('Room Name', validators=[DataRequired(), Length(max=50)])
    submit = SubmitField('Add Room')

class UpdateRateForm(FlaskForm):
    hourly_rate = FloatField('Hourly Rate (PHP)', validators=[DataRequired()])
    submit = SubmitField('Update Rate')

class UploadProofForm(FlaskForm):
    proof = FileField('Upload Proof (Image)', validators=[
        FileAllowed(['jpg', 'png', 'jpeg'], 'Images only!')
    ])
    submit = SubmitField('Upload Proof')