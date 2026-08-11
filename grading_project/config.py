# config.py

import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Load the shared root .env so this app also works when run standalone
# (grading_project/config.py -> parent dir is the repo root)
_root_env = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(_root_env)

class Config:
    # SECRET_KEY MUST be set in .env (FLASK_SECRET_KEY / GRADING_SECRET_KEY). No hardcoded fallback.
    SECRET_KEY = os.environ.get('GRADING_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY is not set. Add FLASK_SECRET_KEY/GRADING_SECRET_KEY to the .env file.")

    DB_HOST = os.environ.get('DB_HOST', 'localhost')
    DB_PORT = os.environ.get('DB_PORT', '3306')
    DB_NAME = os.environ.get('DB_NAME', 'expired')
    DB_USER = os.environ.get('DB_USER', 'root')
    DB_PASSWORD = os.environ.get('DB_PASSWORD', '')

    SQLALCHEMY_DATABASE_URI = (
        f'mysql+pymysql://{DB_USER}:{quote_plus(DB_PASSWORD)}'
        f'@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    )


    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- EMAIL CONFIGURATION FOR OTP ---
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'False').lower() in ('true', '1', 't')
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER = (os.environ.get('MAIL_SENDER_NAME', 'Padre Garcia Polytechnic College'), MAIL_USERNAME) if MAIL_USERNAME else None
