"""Regressions found while rehearsing the VPS update on a copy of the original database."""

from conftest import _uniq, login_as, make_student
from test_phase8_sections import faculty, as_faculty


def test_admin_can_create_admission_user_without_email(client, dbx):
    # The Add New User form hides Email for Admission accounts, but users.email is
    # NOT NULL UNIQUE, so the INSERT failed with "Column 'email' cannot be null".
    login_as(client, 'admin')
    username = _uniq('registrar')
    resp = client.post('/admin/users/add', data={
        'system': 'admission',
        'full_name': 'Test Registrar',
        'username': username,
        'password': 'Secret123!',
        'role': 'registrar',
    })
    assert resp.status_code == 302
    row = dbx.one("SELECT role, email, `system` FROM users WHERE username=%s", (username,))
    assert row is not None, 'the user was not created'
    assert row['role'] == 'registrar' and row['system'] == 'admission'
    assert row['email'] == f'{username}@pgpc.local'


def test_admin_edit_user_keeps_email_when_left_blank(client, dbx):
    # Saving the edit modal with an empty Email used to write NULL and fail.
    login_as(client, 'admin')
    username = _uniq('secretary')
    uid = dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`) "
                "VALUES (%s, %s, 'x', 'secretary', 'active', 'Test Secretary', 'admission')",
                (username, f'{username}@example.com'))
    resp = client.post('/admin/users/edit', data={
        'user_id': uid,
        'full_name': 'Renamed Secretary',
        'username': username,
        'role': 'secretary',
        'email': '',
    })
    assert resp.status_code == 302
    row = dbx.one("SELECT full_name, email FROM users WHERE id=%s", (uid,))
    assert row['full_name'] == 'Renamed Secretary'
    assert row['email'] == f'{username}@example.com'


def test_section_grid_for_program_without_program_name(app, client, dbx):
    # Programs added on the admission side have a title but no program_name; the scheduling
    # grid used to crash on None.upper() for them.
    code = _uniq('NP').upper()[:10]
    pid = code.lower()
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,NULL)",
          (pid, 'Titled Only ' + code, code))
    dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
          "VALUES (%s,'1st Year','1A','A',40,1,1,'2026-2027')", (pid,))
    as_faculty(client, faculty(dbx, 'dean'))
    resp = client.get(f'/faculty/section-grid/prog{pid}/sec1A/sem1')
    assert resp.status_code == 200
    assert ('TITLED ONLY ' + code) in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# OSA is read-only for enrollment status and grades, but can message students.
# ---------------------------------------------------------------------------

def test_osa_cannot_change_enrollment_status(client, dbx, program, active_term):
    student = make_student(dbx, program, status='Enrolled')
    login_as(client, 'osa')

    resp = client.post(f"/admin/application/{student['applicant_id']}/status",
                       json={'status': 'Dropped'})
    assert resp.status_code == 401

    resp = client.post('/admin/applications/bulk-update-status',
                       json={'ids': [student['applicant_id']], 'action': 'Not Enrolled'})
    assert resp.status_code == 401

    resp = client.post(f"/admin/enrollment/{student['applicant_id']}/action",
                       json={'action': 'drop', 'term_id': active_term['id']})
    assert resp.status_code == 401

    row = dbx.one("SELECT application_status FROM applicants WHERE applicant_id=%s",
                  (student['applicant_id'],))
    assert row['application_status'] == 'Enrolled'


def test_osa_enrolled_page_hides_status_controls(client, dbx, program, active_term):
    make_student(dbx, program, status='Enrolled')
    login_as(client, 'osa')

    resp = client.get('/admin/applications/enrolled')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Revert to Enrolling' not in html
    assert 'Set Dropped' not in html
    assert 'Set Not Enrolled' not in html
    # Messaging stays available.
    assert 'Message Selected' in html


def test_osa_grades_are_view_only(client, dbx, program, active_term):
    student = make_student(dbx, program, status='Enrolled')
    login_as(client, 'osa')

    resp = client.get(f"/admin/grades/{student['student_user_id']}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'View only' in html
    assert 'Save Grades' not in html

    resp = client.post(f"/admin/grades/{student['student_user_id']}/save",
                       json={'grades': [{'subject_id': program['subjects'][0],
                                         'grade': '1.00', 'percentage': '95',
                                         'remarks': 'Passed'}]})
    assert resp.status_code == 401
    assert dbx.one("SELECT id FROM student_grades WHERE student_user_id=%s",
                   (student['student_user_id'],)) is None


def test_enrolled_page_flags_students_with_voucher(client, dbx, program, active_term):
    with_voucher = make_student(dbx, program, status='Enrolled')
    without_voucher = make_student(dbx, program, status='Enrolled')
    dbx.x("INSERT INTO voucher_returns (student_id, school_year, semester, returned_by, amount) "
          "VALUES (%s, %s, %s, 'cashier', 1500)",
          (with_voucher['applicant_id'], active_term['year_name'], active_term['semester']))

    login_as(client, 'osa')
    resp = client.get('/admin/applications/enrolled')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="voucherFilter"' in html
    assert f'data-applicant-id="{with_voucher["applicant_id"]}"' in html
    assert 'data-voucher="passed"' in html
    assert 'data-voucher="not_passed"' in html


def test_osa_messenger_page_lists_enrolled_students(client, dbx, program, active_term):
    student = make_student(dbx, program, status='Enrolled')
    login_as(client, 'osa')

    resp = client.get('/admin/messenger')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'New Message' in html
    assert f'value="{student["applicant_id"]}"' in html

    resp = client.get(f'/admin/messenger/chat/{student["applicant_id"]}')
    assert resp.status_code == 200
    assert 'Dela Cruz' in resp.get_data(as_text=True)


def test_osa_can_bulk_message_enrolled_students(client, dbx, program, active_term):
    first = make_student(dbx, program, status='Enrolled')
    second = make_student(dbx, program, status='Enrolled')
    login_as(client, 'osa', name='OSA Staff')

    resp = client.post('/admin/messenger/bulk',
                       json={'ids': [first['applicant_id'], second['applicant_id']],
                             'message': 'Please submit your documents.'})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    rows = dbx.q("SELECT applicant_id, sender_type, admin_role, message_text "
                 "FROM application_messages WHERE applicant_id IN (%s, %s) ORDER BY applicant_id",
                 (first['applicant_id'], second['applicant_id']))
    assert len(rows) == 2
    assert {r['applicant_id'] for r in rows} == {first['applicant_id'], second['applicant_id']}
    assert all(r['sender_type'] == 'admin' and r['admin_role'] == 'osa'
               and r['message_text'] == 'Please submit your documents.' for r in rows)


def test_osa_bulk_message_skips_non_enrolled_students(client, dbx, program, active_term):
    enrolling = make_student(dbx, program, status='Enrolling')
    login_as(client, 'osa')

    resp = client.post('/admin/messenger/bulk',
                       json={'ids': [enrolling['applicant_id']], 'message': 'Hello'})
    assert resp.status_code == 400
    assert dbx.one("SELECT id FROM application_messages WHERE applicant_id=%s",
                   (enrolling['applicant_id'],)) is None
