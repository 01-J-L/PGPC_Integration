# app/utils.py
import random
from flask_mail import Message
from app import mail # We will create this 'mail' object next

def generate_otp():
    """Generate a 6-digit OTP."""
    return str(random.randint(100000, 999999))

def send_otp_email(recipient_email, otp):
    """Send an email with the OTP."""
    msg = Message(
        'Your Verification Code',
        recipients=[recipient_email]
    )
    msg.body = f'Your OTP for the College Grading System is: {otp}'
    mail.send(msg)