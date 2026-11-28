"""Create a small cohort of test students, optionally replacing the old ones.

Run from the project root:

    python scripts/add_test_students.py            # add any missing students
    python scripts/add_test_students.py --reset    # remove the old test cohort first

The cohort contains two NEW students and two EXISTING (returnee/transferee)
students. It reuses the application's own enrollment helpers, so each student
gets a section, a permanent P-series student ID (new students only) and the
matching fee ledger rows -- exactly like a student who submitted the enrollment
form.

Safe to re-run: an e-mail that already exists is skipped.
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

load_dotenv(os.path.join(ROOT, '.env'))

from website import auth  # noqa: E402

PASSWORD = 'Student123!'

# Every e-mail used by this script. --reset removes these students (and any
# rows that depend on them) before the new cohort is inserted.
TEST_EMAILS = [
    # Previous test cohort
    'juan.delacruz@example.com',
    'maria.santos@example.com',
    'pedro.reyes@example.com',
    # Current cohort
    'miguel.bautista@example.com',
    'angela.villanueva@example.com',
    'rafael.castillo@example.com',
    'camille.navarro@example.com',
]

STUDENTS = [
    {
        'first_name': 'Miguel', 'middle_name': 'Santos', 'last_name': 'Bautista',
        'sex': 'Male', 'email': 'miguel.bautista@example.com',
        'mobile': '09171234601', 'birth_date': '2006-02-18',
        'student_type': 'new',
    },
    {
        'first_name': 'Angela', 'middle_name': 'Reyes', 'last_name': 'Villanueva',
        'sex': 'Female', 'email': 'angela.villanueva@example.com',
        'mobile': '09171234602', 'birth_date': '2006-09-04',
        'student_type': 'new',
    },
    {
        'first_name': 'Rafael', 'middle_name': 'Mendoza', 'last_name': 'Castillo',
        'sex': 'Male', 'email': 'rafael.castillo@example.com',
        'mobile': '09171234603', 'birth_date': '2004-05-12',
        'student_type': 'existing', 'old_student_id': 'P202300123',
    },
    {
        'first_name': 'Camille', 'middle_name': 'Lopez', 'last_name': 'Navarro',
        'sex': 'Female', 'email': 'camille.navarro@example.com',
        'mobile': '09171234604', 'birth_date': '2005-11-27',
        'student_type': 'existing', 'old_student_id': 'P202400456',
    },
]

# Minimum NOT NULL applicant fields; the rest keep their defaults.
APPLICANT_SQL = """
    INSERT INTO applicants (
        student_user_id, program_choice, last_name, first_name, middle_name,
        date_of_birth, sex, civil_status, religion, citizenship,
        mobile_number, email_address,
        permanent_address_city_municipality, permanent_address_province,
        permanent_address_postal_code, date_of_application, academic_year,
        average_family_income, guardian_name, guardian_occupation,
        guardian_contact_number, senior_high_school_track_strand,
        senior_high_school_year_from, senior_high_school_year_to,
        agreements, final_submission_date, application_status,
        exam_status, enrollment_year_level, enrollment_semester,
        enrollment_student_type, control_number, old_student_id
    ) VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, 'Single', 'Roman Catholic', 'Filipino',
        %s, %s,
        'Padre Garcia', 'Batangas',
        '4224', CURDATE(), %s,
        'below_10000', 'Parent Guardian', 'Self-employed',
        '09170000000', 'STEM',
        '2022', '2024',
        'yes', CURDATE(), 'Enrolling',
        'Passed', %s, %s,
        %s, %s, %s
    )
"""

# Tables that point directly at applicants or student_users.
APPLICANT_TABLES = [
    ('application_messages', 'applicant_id'),
    ('assessments', 'student_id'),
    ('control_numbers', 'applicant_id'),
    ('down_payments', 'applicant_id'),
    ('enrollment_assessments', 'applicant_id'),
    ('enrollment_checkins', 'applicant_id'),
    ('grades', 'student_id'),
    ('payments', 'student_id'),
    ('registration_forms', 'applicant_id'),
    ('student_discounts_applied', 'student_id'),
    ('voucher_returns', 'student_id'),
]
STUDENT_USER_TABLES = [
    ('control_numbers', 'student_user_id'),
    ('down_payments', 'student_user_id'),
    ('enrollment_assessments', 'student_user_id'),
    ('enrollment_checkins', 'student_user_id'),
    ('enrollment_checklist_items', 'student_user_id'),
    ('grade_holds', 'student_user_id'),
    ('registration_forms', 'student_user_id'),
    ('student_assessments', 'student_user_id'),
    ('student_curricula', 'student_user_id'),
    ('student_enrollment_records', 'student_user_id'),
    ('student_grades', 'student_user_id'),
    ('student_payments', 'student_user_id'),
]


def _placeholders(values):
    return ', '.join(['%s'] * len(values))


def _delete_in(cursor, table, column, ids):
    if ids:
        cursor.execute(
            f"DELETE FROM `{table}` WHERE `{column}` IN ({_placeholders(ids)})",
            tuple(ids))


def _remove_test_students(cursor):
    """Delete the old test cohort and every row that depends on it."""
    emails = tuple(TEST_EMAILS)
    cursor.execute(
        f"SELECT applicant_id, student_user_id FROM applicants "
        f"WHERE email_address IN ({_placeholders(emails)}) "
        f"OR student_user_id IN (SELECT id FROM student_users WHERE email IN ({_placeholders(emails)}))",
        emails + emails)
    rows = cursor.fetchall()
    if not rows:
        return 0

    applicant_ids = sorted({row['applicant_id'] for row in rows})
    student_user_ids = sorted({row['student_user_id'] for row in rows if row['student_user_id']})

    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    try:
        # Children of registration forms, assessments, payments and check-ins
        # must go before their parents.
        rf_ids = []
        if applicant_ids:
            cursor.execute(
                f"SELECT id FROM registration_forms WHERE applicant_id IN ({_placeholders(applicant_ids)})",
                tuple(applicant_ids))
            rf_ids = [r['id'] for r in cursor.fetchall()]
        _delete_in(cursor, 'registration_form_subjects', 'registration_form_id', rf_ids)

        ea_ids = []
        if applicant_ids:
            cursor.execute(
                f"SELECT id FROM enrollment_assessments WHERE applicant_id IN ({_placeholders(applicant_ids)})",
                tuple(applicant_ids))
            ea_ids = [r['id'] for r in cursor.fetchall()]
        _delete_in(cursor, 'enrollment_assessment_items', 'enrollment_assessment_id', ea_ids)

        payment_ids = []
        if applicant_ids:
            cursor.execute(
                f"SELECT id FROM payments WHERE student_id IN ({_placeholders(applicant_ids)})",
                tuple(applicant_ids))
            payment_ids = [r['id'] for r in cursor.fetchall()]
        for table in ('payment_verifications', 'payment_reversals', 'payment_submissions',
                      'down_payments'):
            _delete_in(cursor, table, 'payment_id', payment_ids)

        assessment_ids = []
        if applicant_ids:
            cursor.execute(
                f"SELECT id FROM assessments WHERE student_id IN ({_placeholders(applicant_ids)})",
                tuple(applicant_ids))
            assessment_ids = [r['id'] for r in cursor.fetchall()]
        for table, column in (('enrollment_assessment_items', 'assessment_id'),
                              ('down_payments', 'applied_assessment_id')):
            _delete_in(cursor, table, column, assessment_ids)

        student_assessment_ids = []
        if student_user_ids:
            cursor.execute(
                f"SELECT id FROM student_assessments WHERE student_user_id IN ({_placeholders(student_user_ids)})",
                tuple(student_user_ids))
            student_assessment_ids = [r['id'] for r in cursor.fetchall()]
        _delete_in(cursor, 'student_payments', 'assessment_id', student_assessment_ids)

        for table, column in APPLICANT_TABLES:
            _delete_in(cursor, table, column, applicant_ids)
        for table, column in STUDENT_USER_TABLES:
            _delete_in(cursor, table, column, student_user_ids)

        _delete_in(cursor, 'applicants', 'applicant_id', applicant_ids)
        _delete_in(cursor, 'student_users', 'id', student_user_ids)
    finally:
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    return len(applicant_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset', action='store_true',
                        help='remove the old test cohort before adding the new students')
    args = parser.parse_args()

    conn = auth.get_db_connection()
    if not conn:
        raise SystemExit('Could not connect to the database.')

    cursor = conn.cursor(dictionary=True)
    try:
        term = auth._get_active_term()
        if not term:
            raise SystemExit('No active academic term. Activate one first.')

        cursor.execute("SELECT program_id, title FROM programs ORDER BY title LIMIT 1")
        program = cursor.fetchone()
        if not program:
            raise SystemExit('No program found in the database.')
        program_id = program['program_id']
        program_title = program['title']

        if args.reset:
            removed = _remove_test_students(cursor)
            conn.commit()
            print(f"Removed {removed} old test student(s).")

        year_level = '1st Year'
        created = []

        for i, student in enumerate(STUDENTS, start=1):
            cursor.execute("SELECT id FROM student_users WHERE email = %s", (student['email'],))
            if cursor.fetchone():
                print(f"[skip] {student['email']} already exists")
                continue

            is_existing = student['student_type'] == 'existing'
            old_student_id = student.get('old_student_id')

            hashed = generate_password_hash(PASSWORD, method='pbkdf2:sha256')
            cursor.execute(
                "INSERT INTO student_users (email, password, is_verified, student_type, old_student_id) "
                "VALUES (%s, %s, 1, %s, %s)",
                (student['email'], hashed, student['student_type'], old_student_id))
            student_user_id = cursor.lastrowid

            temp_control = old_student_id or f'TEST{i:04d}'
            cursor.execute(APPLICANT_SQL, (
                student_user_id, program_title,
                student['last_name'], student['first_name'], student['middle_name'],
                student['birth_date'], student['sex'],
                student['mobile'], student['email'],
                term['year_name'],
                year_level, term['semester'],
                'Existing' if is_existing else 'New',
                temp_control, old_student_id,
            ))
            applicant_id = cursor.lastrowid

            auth._assign_section_automatically(cursor, applicant_id, program_id, year_level)

            permanent_id = ''
            if not is_existing:
                permanent_id = auth._assign_permanent_student_id(cursor, applicant_id, 'new')

            cursor.execute("SELECT * FROM applicants WHERE applicant_id = %s", (applicant_id,))
            fees_added = auth._auto_assign_fees(cursor, applicant_id, cursor.fetchone())

            conn.commit()
            created.append((student['first_name'], student['last_name'], student['email'],
                            'Existing' if is_existing else 'New',
                            permanent_id or old_student_id, fees_added))

        if not created:
            print('Nothing to add; all test students already exist.')
            return

        print('\nCreated Enrolling students:')
        for first, last, email, kind, sid, fees in created:
            print(f"  {first} {last}  |  {kind:<8}  |  {email}  |  ID: {sid}  |  fees: {fees}")
        print(f"\nPassword for all students: {PASSWORD}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    main()
