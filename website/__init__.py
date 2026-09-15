from flask import Flask
from flask_mail import Mail
from flask_sqlalchemy import SQLAlchemy 
from flask_login import LoginManager, current_user   
from dotenv import load_dotenv
import os
from flask import request, session, render_template

# Load environment variables
load_dotenv()

# Initialize Extensions
mail = Mail()
db = SQLAlchemy()           
login_manager = LoginManager() 

def create_app():
    app = Flask(__name__)

    # --- 1. Main App Configuration ---
    # SECRET_KEY MUST be set in the .env file (FLASK_SECRET_KEY). No hardcoded fallback.
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
    if not app.config['SECRET_KEY']:
        raise RuntimeError("FLASK_SECRET_KEY is not set. Add it to the .env file.")
    app.debug = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')

    # --- 2. File Upload Configuration ---
    upload_folder = os.path.join(app.root_path, 'static', 'images', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    app.config['UPLOAD_FOLDER'] = upload_folder
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

    # --- 3. Email Configuration ---
    app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
    app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
    app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() in ('true', '1', 't')
    app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
    app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
    
    # Sender Config
    default_sender_email = os.getenv('MAIL_DEFAULT_SENDER_EMAIL')
    default_sender_name = os.getenv('MAIL_DEFAULT_SENDER_NAME')
    if default_sender_name and default_sender_email:
        app.config['MAIL_DEFAULT_SENDER'] = (default_sender_name, default_sender_email)
    elif default_sender_email:
        app.config['MAIL_DEFAULT_SENDER'] = default_sender_email
    else:
        app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

    app.config['MAIL_SENDER_NAME'] = os.getenv('MAIL_SENDER_NAME', 'Padre Garcia Polytechnic College')
    app.config['ADMIN_EMAIL'] = os.getenv('ADMIN_EMAIL')

    # --- 4. SERVER CONFIG ---
    app.config['SERVER_NAME'] = os.getenv('FLASK_SERVER_NAME')
    app.config['PREFERRED_URL_SCHEME'] = os.getenv('FLASK_PREFERRED_URL_SCHEME', 'http')

    # =========================================================
    # FACULTY SYSTEM INTEGRATION (DATABASE)
    # =========================================================

    db_user = os.environ.get('DB_USER', 'root')
    db_password = os.environ.get('DB_PASSWORD', '')
    db_host = os.environ.get('DB_HOST', 'localhost')
    db_port = os.environ.get('DB_PORT', '3306')
    db_name = os.environ.get('DB_NAME', 'expired')
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        f'mysql+mysqlconnector://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Initialize Extensions with App
    mail.init_app(app)
    db.init_app(app)
    login_manager.init_app(app)
    
    # Login Manager Setup for Faculty
    login_manager.login_view = 'faculty_auth.login'
    login_manager.login_message_category = 'info'

    # --- 5. Register Blueprints ---
    
    # A. ADMISSION SYSTEM BLUEPRINTS
    from .views import views
    from .auth import auth

    app.register_blueprint(views, url_prefix='/')
    app.register_blueprint(auth, url_prefix='/')

    # B. FACULTY SYSTEM BLUEPRINTS (Imported from the pasted 'faculty' folder)
    # Note: We wrap this in try/except so the app doesn't crash if files are missing
    try:
        # Import the 'auth' variable from the faculty routes
        from .faculty.auth.routes import auth as faculty_auth
        from .faculty.main.routes import main as faculty_main
        
        # Register it with the prefix /admin-load
        app.register_blueprint(faculty_auth, url_prefix='/admin-load')
        app.register_blueprint(faculty_main, url_prefix='/faculty')
        
        print("[OK] Faculty System Integrated Successfully")
    except ImportError as e:
        print(f"[WARN] Faculty System Import Error: {e}")

    # C. GRADING SYSTEM (merged into faculty system - routes in faculty/main/routes.py)

    # --- 5.5 GLOBAL MAINTENANCE MODE GATE ---
    # When maintenance_mode = '1' in system_settings, block everything for
    # everyone except the superadmin. The superadmin login page stays
    # reachable so they can log in and turn maintenance off again.
    @app.before_request
    def check_maintenance_mode():
        if request.endpoint == 'static':
            return
        if session.get('user_role') == 'superadmin' or session.get('is_superadmin'):
            return
        # Keep the login pages + login/logout endpoints reachable at all times
        if request.endpoint in ('auth.admin', 'auth.admin_login', 'auth.superadmin_login', 'auth.logout', 'auth.admin_maintenance'):
            return
        try:
            from .models import SystemSetting
            setting = SystemSetting.query.get('maintenance_mode')
            if setting and setting.value == '1':
                return render_template('maintenance.html'), 503
        except Exception as e:
            print(f"⚠️ Maintenance check error: {e}")
        return

    @app.context_processor
    def inject_faculty_notifications():
        count = 0
        if current_user.is_authenticated:
            try:
                # Check if the current user is actually a Faculty User (has 'expertise_type')
                # This prevents errors if an Admission Admin logs in and visits a faculty page
                if hasattr(current_user, 'expertise_type'):
                    from website.faculty.models import Notification
                    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
            except Exception:
                pass # Fail silently if table doesn't exist or other error
        
        return dict(unread_notif_count=count)   

    # --- 6. AUTO-CREATE DATABASE TABLES ON STARTUP ---
    # Imports all model definitions and creates any missing tables so a fresh
    # deployment only needs to set up the database itself (db.create_all()).
    try:
        from . import models as _models  # noqa: F401
        with app.app_context():
            db.create_all()
        print("[OK] Database tables verified (create_all ran successfully).")
    except Exception as e:
        print(f"[WARN] Database auto-create failed: {e}")

    return app