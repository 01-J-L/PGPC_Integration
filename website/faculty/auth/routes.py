# website/faculty/auth/routes.py

from flask import Blueprint, render_template, redirect, url_for, flash, request
from website import db, mail
from website.faculty.auth.forms import LoginForm, RegistrationForm, PasswordResetRequestForm, PasswordResetForm, VerifyOTPForm
from website.faculty.models import User
from flask_login import current_user, login_user, logout_user
from flask_mail import Message
import os

# Renamed blueprint to avoid conflict with Admission system
auth = Blueprint('faculty_auth', __name__)

def send_otp_email(user):
    otp = user.generate_otp()
    db.session.commit() 
    msg = Message('Your Verification Code - Faculty System',
                  recipients=[user.email])
    msg.body = f'''Your verification code is: {otp}

This code will expire in 10 minutes.
If you did not request this, please ignore this email.
'''
    mail.send(msg)

def send_password_reset_email(user):
    token = user.get_reset_password_token()
    msg = Message('Password Reset Request - Faculty System',
                  recipients=[user.email])
    msg.body = f'''To reset your password, visit the following link:
{url_for('faculty_auth.reset_password', token=token, _external=True)}

This link will expire in 30 minutes.
If you did not make this request then simply ignore this email.
'''
    mail.send(msg)

# --- Routes ---
@auth.route('/', methods=['GET', 'POST'])       # Make /admin-load work directly
@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('faculty_main.dashboard')) # Updated blueprint name
    form = LoginForm()
    if form.validate_on_submit():
        login_identifier = form.email.data
        user = User.query.filter_by(email=login_identifier).first()
        if user is None:
            user = User.query.filter_by(username=login_identifier).first()

        # FALLBACK / INITIAL SETUP (For first-time deployment)
        # Mirrors the admission system: the first dean account is bootstrapped
        # from the ADMIN_INITIAL_PASSWORD environment variable (.env).
        initial_dean_password = os.getenv('ADMIN_INITIAL_PASSWORD')
        if user is None and initial_dean_password and form.password.data == initial_dean_password:
            dean_username = login_identifier.strip()
            dean_spec = {
                'ccbaAdmin': {'full_name': 'Dean Micheal K. Almirez', 'department': 'CCBA'},
                'cocadmin': {'full_name': 'Dean Joy H. Abu', 'department': 'COC'},
            }.get(dean_username)
            if dean_spec and not User.query.filter_by(role='dean').first():
                dean = User(
                    full_name=dean_spec['full_name'],
                    email=f'{dean_username}@pgpc.edu.ph',
                    department=dean_spec['department'],
                    role='dean',
                    status='active',
                )
                dean.username = dean_username
                dean.set_password(initial_dean_password)
                db.session.add(dean)
                db.session.commit()
                flash('Initial Dean account created successfully. You are logged in.', 'success')
                login_user(dean)
                return redirect(url_for('faculty_main.dashboard'))

        if user is None or not user.check_password(form.password.data):
            flash('Invalid credentials. Please check your email/username and password.', 'error')
            return redirect(url_for('faculty_auth.login'))
        
        if user.status == 'unverified':
            flash('Your account has not been verified. Please check your email.', 'error')
            return redirect(url_for('faculty_auth.login'))
        login_user(user, remember=form.remember_me.data)
        return redirect(url_for('faculty_main.dashboard'))
    return render_template('faculty/login.html', title='Sign In', form=form)

@auth.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('faculty_auth.login'))

@auth.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('faculty_main.dashboard'))
    form = RegistrationForm()
    if form.validate_on_submit():
        
        existing_user = User.query.filter_by(email=form.email.data).first()
        if existing_user:
            flash('This email address is already registered.', 'error')
            return redirect(url_for('faculty_auth.register'))

        user = User(
            full_name=form.full_name.data, 
            email=form.email.data,
            contact_number=form.contact_number.data,
            baccalaureate_degree=form.baccalaureate_degree.data,
            baccalaureate_year_grad=form.baccalaureate_year_grad.data,
            highest_edu_attainment=form.highest_edu_attainment.data,
            highest_edu_year_grad=form.highest_edu_year_grad.data,
            industry_experience=form.industry_experience.data,
            is_licensed=(form.is_licensed.data == 'Yes')
        )
        
        # Handle PROFED vs GENED
        user.expertise_type = form.expertise_type.data
        if form.expertise_type.data == 'PROFED':
            if not form.major_program.data:
                flash('You must select a program if expertise is "PROFED".', 'error')
                return render_template('faculty/register.html', title='Register', form=form)
            user.program_id = form.major_program.data.id
        else: 
            user.program_id = None

        user.subjects_taught = "" 
        user.set_password(form.password.data)
        user.status = 'unverified'
        db.session.add(user)
        db.session.commit()
        
        send_otp_email(user)
        
        flash('A verification code has been sent to your email.', 'success')
        return redirect(url_for('faculty_auth.verify_otp', email=user.email))
        
    return render_template('faculty/register.html', title='Register', form=form)

@auth.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    email = request.args.get('email')
    if not email:
        return redirect(url_for('faculty_auth.login'))
        
    user = User.query.filter_by(email=email).first_or_404()
    
    if user.status != 'unverified':
        flash('This account has already been verified.', 'info')
        return redirect(url_for('faculty_auth.login'))

    form = VerifyOTPForm()
    if form.validate_on_submit():
        if user.verify_otp(form.otp.data):
            user.status = 'pending' 
            db.session.commit()
            flash('Congratulations, your email has been verified! Your account is now awaiting dean approval.', 'success')
            return redirect(url_for('faculty_auth.login'))
        else:
            flash('Invalid or expired verification code. Please try again.', 'error')

    return render_template('faculty/verify_otp.html', title='Enter Verification Code', form=form)

@auth.route('/reset_password_request', methods=['GET', 'POST'])
def reset_password_request():
    if current_user.is_authenticated:
        return redirect(url_for('faculty_main.dashboard'))
    form = PasswordResetRequestForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            send_password_reset_email(user)
        flash('Check your email for the instructions to reset your password.', 'success')
        return redirect(url_for('faculty_auth.login'))
    return render_template('faculty/reset_password_request.html', title='Reset Password', form=form)

@auth.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('faculty_main.dashboard'))
    user = User.verify_reset_password_token(token)
    if not user:
        flash('That is an invalid or expired token.', 'error')
        return redirect(url_for('faculty_auth.reset_password_request'))
    form = PasswordResetForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash('Your password has been reset successfully!', 'success')
        return redirect(url_for('faculty_auth.login'))
    return render_template('faculty/reset_password.html', title='Reset Your Password', form=form)

@auth.route('/resend-otp', methods=['GET'])
def resend_otp():
    email = request.args.get('email')
    if not email:
        flash('Email is required to resend code.', 'error')
        return redirect(url_for('faculty_auth.login'))
    
    user = User.query.filter_by(email=email).first()
    
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('faculty_auth.login'))
    
    if user.status != 'unverified':
        flash('This account is already verified.', 'info')
        return redirect(url_for('faculty_auth.login'))

    send_otp_email(user)
    
    flash('A new verification code has been sent to your email.', 'success')
    return redirect(url_for('faculty_auth.verify_otp', email=email))