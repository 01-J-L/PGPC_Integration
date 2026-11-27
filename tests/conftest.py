"""Shared fixtures for the PGPC test suite.

The suite runs against a THROWAWAY MySQL/MariaDB database. It refuses to start
unless DB_NAME ends with ``_test`` so it can never touch the production or
development database.

    set DB_NAME=pgpc_test            (Windows)   /   export DB_NAME=pgpc_test
    python -m pytest tests -q

The database is dropped and rebuilt from full.sql at the start of every run.
"""

import os
import re
import sys
import datetime
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Never send real e-mail from tests; point SMTP at a closed local port.
os.environ.setdefault('MAIL_SERVER', '127.0.0.1')
os.environ.setdefault('MAIL_PORT', '2525')
os.environ['MAIL_USE_TLS'] = 'False'
os.environ['MAIL_USE_SSL'] = 'False'
os.environ.setdefault('FLASK_SECRET_KEY', 'pgpc-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AdminPass123')

DB_NAME = os.environ.get('DB_NAME', '')
if not DB_NAME.endswith('_test'):
    raise RuntimeError(
        "Refusing to run tests: DB_NAME must end with '_test' (got %r). "
        "The test suite drops and rebuilds the database." % DB_NAME)

import mysql.connector  # noqa: E402


def _raw_conn(database=None):
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', '3306')),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=database,
    )


BASELINE_SQL = os.path.join(ROOT, 'tests', 'fixtures', 'baseline_full_pre_phase1.sql')


def load_sql_file(path, database):
    """Drop `database` and rebuild it from a full.sql-style script."""
    sql = open(path, encoding='utf-8').read()
    sql = sql.replace('`pgpc_db`', '`%s`' % database)
    conn = _raw_conn()
    cur = conn.cursor()
    cur.execute('DROP DATABASE IF EXISTS `%s`' % database)
    # Execute statement by statement (full.sql contains no procedures).
    statements = [s for s in re.split(r';\s*\n', re.sub(r'(?m)^\s*--.*$', '', sql)) if s.strip()]
    for stmt in statements:
        cur.execute(stmt)
    conn.commit()
    cur.close()
    conn.close()


def _rebuild_database():
    # Start from the schema as it was BEFORE Phase 1 (what production has),
    # then apply the migrations exactly as a deployment would.
    load_sql_file(BASELINE_SQL, DB_NAME)
    from website.schema_migrations import apply_all
    conn = _raw_conn(DB_NAME)
    apply_all(conn, log=lambda *_: None)
    conn.close()


_rebuild_database()

from website import create_app, db  # noqa: E402

_app = create_app()
_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, MAIL_SUPPRESS_SEND=True)


@pytest.fixture(scope='session')
def app():
    return _app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def csrf_app(app):
    """The same app with CSRF protection switched ON for the duration of a test."""
    app.config['WTF_CSRF_ENABLED'] = True
    yield app
    app.config['WTF_CSRF_ENABLED'] = False


class DB:
    """Tiny helper around a raw connection for arranging/asserting test data."""

    def __init__(self):
        self.conn = _raw_conn(DB_NAME)
        self.conn.autocommit = True

    def q(self, sql, params=()):
        cur = self.conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.with_rows else []
        cur.close()
        return rows

    def one(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        last = cur.lastrowid
        cur.close()
        return last

    def close(self):
        self.conn.close()


@pytest.fixture()
def dbx():
    d = DB()
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

_counter = {'n': 0}


def _uniq(prefix):
    _counter['n'] += 1
    return '%s%d' % (prefix, _counter['n'])


@pytest.fixture()
def program(dbx):
    """A BSCS-like program with a 1st-year/2nd-sem subject set and a section."""
    pid = _uniq('tp')
    code = pid.upper()
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name, total_units, max_units) "
          "VALUES (%s, %s, %s, %s, 0, 26)", (pid, 'Test Program ' + code, code, 'Test Program ' + code))
    subj = []
    for i, (c, t, u) in enumerate([('CC10', 'Intro', 3), ('CC11', 'Programming', 3)]):
        sid = dbx.x("INSERT INTO subjects (subject_code, subject_title, program_id, units, semester, year_level) "
                    "VALUES (%s, %s, %s, %s, 1, 1)", (c + pid, t, pid, u))
        subj.append(sid)
    sec = dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
                "VALUES (%s, '1st Year', '1A', 'A', 40, 1, 1, '2026-2027')", (pid,))
    return {'id': pid, 'title': 'Test Program ' + code, 'subjects': subj, 'section_id': sec}


@pytest.fixture()
def active_term(dbx):
    row = dbx.one("SELECT * FROM academic_terms WHERE year_name='2026-2027' AND semester='1st Semester'")
    if not row:
        dbx.x("INSERT INTO academic_terms (year_name, semester, is_active, max_units) VALUES ('2026-2027','1st Semester',1,26)")
        row = dbx.one("SELECT * FROM academic_terms WHERE year_name='2026-2027' AND semester='1st Semester'")
    dbx.x("UPDATE academic_terms SET is_active = (id = %s)", (row['id'],))
    return dbx.one("SELECT * FROM academic_terms WHERE id=%s", (row['id'],))


def make_student(dbx, program, status='Enrolling', student_type='new', control='A00001'):
    email = _uniq('student') + '@example.com'
    from werkzeug.security import generate_password_hash
    suid = dbx.x("INSERT INTO student_users (email, password, is_verified, student_type) VALUES (%s, %s, 1, %s)",
                 (email, generate_password_hash('Student123!'), student_type))
    control = control + str(suid) if control else None
    aid = dbx.x("""
        INSERT INTO applicants (student_user_id, program_choice, last_name, first_name, date_of_birth, sex,
            civil_status, religion, citizenship, mobile_number, email_address,
            permanent_address_city_municipality, permanent_address_province, permanent_address_postal_code,
            date_of_application, academic_year, average_family_income, guardian_name, guardian_occupation,
            guardian_contact_number, senior_high_school_track_strand, senior_high_school_year_from,
            senior_high_school_year_to, agreements, final_submission_date, application_status,
            enrollment_year_level, enrollment_semester, enrollment_student_type, control_number, old_student_id)
        VALUES (%s,%s,'Dela Cruz','Juan','2006-01-01','Male','Single','None','Filipino','09170000000',%s,
            'Padre Garcia','Batangas','4224',CURDATE(),'2026-2027','below_10000','Maria','None','09170000001',
            'STEM','2022','2024','yes',CURDATE(),%s,'1st Year','1st Semester',%s,%s,%s)
    """, (suid, program['title'], email, status, 'New' if student_type == 'new' else 'Existing', control, control))
    return {'student_user_id': suid, 'applicant_id': aid, 'email': email}


@pytest.fixture()
def student(dbx, program):
    return make_student(dbx, program)


def login_as(client, role, name='Test User', user_id=1):
    with client.session_transaction() as s:
        s['user_role'] = role
        s['user_name'] = name
        s['user_id'] = user_id
        s['can_edit_student'] = True


def login_student(client, student_user_id):
    with client.session_transaction() as s:
        s['student_id'] = student_user_id
