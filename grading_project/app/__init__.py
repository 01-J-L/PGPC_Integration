# app/__init__.py

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_migrate import Migrate
from config import Config
import time
from flask_mail import Mail  # <-- THIS WAS THE CORRECTED LINE

db = SQLAlchemy()
bcrypt = Bcrypt()
mail = Mail()
login_manager = LoginManager()
login_manager.login_view = 'main.admin_login'
login_manager.login_message_category = 'info'
migrate = Migrate()


@login_manager.unauthorized_handler
def handle_unauthorized():
    from flask import redirect, request, url_for
    if request.endpoint and request.endpoint.startswith('main.faculty'):
        return redirect(url_for('main.faculty_login', next=request.url))
    return redirect(url_for('main.admin_login', next=request.url))

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    bcrypt.init_app(app)
    mail.init_app(app) # Initialize mail with the app
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # This is the function that fixes the caching problem
    @app.context_processor
    def inject_cache_buster():
        return dict(cache_buster=int(time.time()))

    from app.routes import main as main_blueprint
    app.register_blueprint(main_blueprint)

    # --- GLOBAL MAINTENANCE MODE GATE ---
    # When maintenance_mode = '1' in system_settings, block everything for
    # everyone except the superadmin. The login page stays reachable so the
    # superadmin can log in and turn maintenance off again.
    @app.before_request
    def check_maintenance_mode():
        from flask import request, session, render_template
        from flask_login import current_user
        from sqlalchemy import text
        if request.endpoint == 'static':
            return
        if current_user.is_authenticated and current_user.role == 'superadmin':
            return
        # Keep login/logout + maintenance pages reachable at all times
        if request.endpoint in ('main.admin_login', 'main.logout', 'main.admin_maintenance'):
            return
        try:
            row = db.session.execute(
                text("SELECT setting_value FROM system_settings WHERE setting_key = 'maintenance_mode'")
            ).fetchone()
            if row and row[0] == '1':
                return render_template('maintenance.html'), 503
        except Exception as e:
            print(f"⚠️ Maintenance check error: {e}")
        return

    return app