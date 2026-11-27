"""Phase 6: PGPC-YY-NNNN enrollment control numbers."""

import re
import threading

import pytest

from conftest import login_as, make_student
from test_phase3_regform import world, student_in, create, flashes, _term          # noqa: F401  (fixtures)
from test_phase4_finance import fees, run, A, reset_policy                       # noqa: F401  (fixtures)
from test_phase5_checkin import ready_student, check_in_json

ADMIN = ('Ady Admin', 'admin')
NUM = re.compile(r'^PGPC-26-(\d{4,})$')


def ctl():
    from website.checkin import control
    return control


def last26(dbx):
    row = dbx.one("SELECT last_number FROM control_number_sequences WHERE prefix='PGPC' AND year_code='26'")
    return row['last_number'] if row else 0


def number_of(dbx, s):
    row = dbx.one("SELECT number FROM control_numbers WHERE student_user_id=%s", (s['student_user_id'],))
    return row['number'] if row else None


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------

def test_format_and_year_code():
    c = ctl()
    assert c.year_code('2026-2027') == '26' and c.year_code(' 2099 - 2100 ') == '99'
    assert c.format_number('26', 7) == 'PGPC-26-0007' and c.format_number('26', 1434) == 'PGPC-26-1434'
    assert c.format_number('26', 10000) == 'PGPC-26-10000'          # never wraps to 0000
    for bad in ('2026', '2026-2028', 'SY 2026', '', None):
        with pytest.raises(c.ControlNumberError):
            c.year_code(bad)


# ---------------------------------------------------------------------------
# issuing at check-in
# ---------------------------------------------------------------------------

def test_check_in_issues_the_next_number_everywhere(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    before = last26(dbx)
    r = check_in_json(client, s, world)
    expected = 'PGPC-26-%04d' % (before + 1)
    assert r.get_json()['success'] and ('Control No. %s.' % expected) in r.get_json()['message']
    assert number_of(dbx, s) == expected and last26(dbx) == before + 1
    assert dbx.one("SELECT control_number FROM registration_forms WHERE id=%s", (s['form_id'],))['control_number'] == expected
    cn = dbx.one("SELECT * FROM control_numbers WHERE number=%s", (expected,))
    assert cn['registration_form_id'] == s['form_id'] and cn['issued_by'] == 'Reg One' and cn['source'] == 'check_in'
    assert expected in dbx.one("SELECT checklist_snapshot FROM enrollment_checkins WHERE student_user_id=%s", (s['student_user_id'],))['checklist_snapshot']
    assert dbx.one("SELECT COUNT(*) n FROM enrollment_audit_log WHERE action='control_number' AND applicant_id=%s", (s['applicant_id'],))['n'] == 1
    login_as(client, 'registrar')
    assert expected in client.get('/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], world['t1'])).get_data(as_text=True)
    assert expected in client.get('/admin/registration/forms/%d' % s['form_id']).get_data(as_text=True)
    assert expected in client.get('/admin/registration/forms/%d/print' % s['form_id']).get_data(as_text=True)
    with client.session_transaction() as sess:
        sess.clear()
        sess['student_id'] = s['student_user_id']
    assert expected in client.get('/my-registration/%d' % s['form_id']).get_data(as_text=True)


def test_numbers_run_on_across_students_and_programs(app, client, dbx, world, fees):
    a = ready_student(app, client, dbx, world)
    b = ready_student(app, client, dbx, world)
    assert check_in_json(client, a, world).get_json()['success']
    assert check_in_json(client, b, world).get_json()['success']
    na, nb = int(NUM.match(number_of(dbx, a)).group(1)), int(NUM.match(number_of(dbx, b)).group(1))
    assert nb == na + 1


def test_no_number_before_check_in_and_failed_check_in_uses_none(app, client, dbx, world, fees, monkeypatch):
    s = ready_student(app, client, dbx, world)
    login_as(client, 'registrar')
    assert 'Not yet assigned' in client.get('/admin/registration/forms/%d' % s['form_id']).get_data(as_text=True)
    before = last26(dbx)
    import website.checkin.control as control

    def boom(*a, **k):
        raise RuntimeError('simulated failure after the number was taken')
    monkeypatch.setattr(control, '_audit', boom)
    r = check_in_json(client, s, world)
    assert r.status_code == 500
    assert last26(dbx) == before and number_of(dbx, s) is None                # rolled back with the check-in
    assert dbx.one("SELECT application_status s FROM applicants WHERE applicant_id=%s", (s['applicant_id'],))['s'] == 'Enrolling'
    monkeypatch.undo()
    assert check_in_json(client, s, world).get_json()['success']
    assert number_of(dbx, s) == 'PGPC-26-%04d' % (before + 1)                # the next check-in gets the same, unused number


def test_undo_and_redo_keep_the_same_number(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    assert check_in_json(client, s, world).get_json()['success']
    first, counter = number_of(dbx, s), last26(dbx)
    login_as(client, 'registrar', name='Reg One')
    client.post('/admin/enrollment-checkin/%d/undo' % s['applicant_id'], data={'term_id': world['t1'], 'reason': 'typo'})
    assert number_of(dbx, s) == first                                        # kept while undone
    login_as(client, 'admin')
    page = client.get('/admin/enrollment-checkin/control-numbers?q=%s' % first).get_data(as_text=True)
    assert first in page and 'Check-in undone' in page
    assert check_in_json(client, s, world).get_json()['success']
    assert number_of(dbx, s) == first and last26(dbx) == counter
    assert dbx.one("SELECT COUNT(*) n FROM control_numbers WHERE student_user_id=%s", (s['student_user_id'],))['n'] == 1


def test_each_school_year_has_its_own_counter(app, dbx, world):
    t27 = _term(dbx, '2027-2028', '1st Semester')
    s = student_in(dbx, world)
    c = ctl()

    def go():
        from website import db
        from website.models import Applicant, AcademicTerm
        return c.issue(A(*ADMIN), db.session.get(Applicant, s['applicant_id']), db.session.get(AcademicTerm, t27))[0].number
    before = (dbx.one("SELECT last_number FROM control_number_sequences WHERE year_code='27'") or {'last_number': 0})['last_number']
    assert run(app, go) == 'PGPC-27-%04d' % (before + 1)
    assert run(app, go) == 'PGPC-27-%04d' % (before + 1)                      # same student and term: same number


def test_database_keys_forbid_duplicates(app, client, dbx, world, fees):
    import mysql.connector
    s = ready_student(app, client, dbx, world)
    assert check_in_json(client, s, world).get_json()['success']
    cn = dbx.one("SELECT * FROM control_numbers WHERE student_user_id=%s", (s['student_user_id'],))
    other = student_in(dbx, world)
    cols = "(number, sequence_id, seq, student_user_id, applicant_id, academic_term_id, issued_by)"
    with pytest.raises(mysql.connector.IntegrityError):          # same number
        dbx.x("INSERT INTO control_numbers " + cols + " VALUES (%s,%s,%s,%s,%s,%s,'x')",
              (cn['number'], cn['sequence_id'], 999998, other['student_user_id'], other['applicant_id'], world['t1']))
    with pytest.raises(mysql.connector.IntegrityError):          # same counter position
        dbx.x("INSERT INTO control_numbers " + cols + " VALUES ('PGPC-26-X',%s,%s,%s,%s,%s,'x')",
              (cn['sequence_id'], cn['seq'], other['student_user_id'], other['applicant_id'], world['t1']))
    with pytest.raises(mysql.connector.IntegrityError):          # second number for the same student and term
        dbx.x("INSERT INTO control_numbers " + cols + " VALUES ('PGPC-26-Y',%s,999997,%s,%s,%s,'x')",
              (cn['sequence_id'], s['student_user_id'], s['applicant_id'], world['t1']))


@pytest.mark.parametrize('new_year', [False, True], ids=['existing-counter', 'first-number-of-a-new-year'])
def test_concurrent_issuing_never_duplicates(app, dbx, world, new_year):
    """Several registrars at once: every number distinct and consecutive (row lock on the counter)."""
    term_id = world['t1']
    if new_year:
        dbx.x("DELETE FROM control_number_sequences WHERE year_code='31' AND NOT EXISTS "
              "(SELECT 1 FROM control_numbers c WHERE c.sequence_id = control_number_sequences.id)")
        term_id = _term(dbx, '2031-2032', '1st Semester')
    students = [student_in(dbx, world) for _ in range(16)]
    c = ctl()
    out, errors = [], []

    def worker(chunk):
        try:
            for st in chunk:
                with app.app_context():
                    from website import db
                    from website.models import Applicant, AcademicTerm
                    try:
                        cn, _ = c.issue(A('Reg %d' % st['applicant_id'], 'registrar'),
                                        db.session.get(Applicant, st['applicant_id']), db.session.get(AcademicTerm, term_id))
                        db.session.commit()
                        out.append(cn.seq)
                    finally:
                        db.session.remove()
        except Exception as e:     # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=worker, args=(students[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(out) == 16 and len(set(out)) == 16
    assert sorted(out) == list(range(min(out), min(out) + 16))
    if new_year:
        assert sorted(out) == list(range(1, 17))


# ---------------------------------------------------------------------------
# administration
# ---------------------------------------------------------------------------

def test_set_next_number_rules(app, client, dbx, world, fees):
    s = ready_student(app, client, dbx, world)
    assert check_in_json(client, s, world).get_json()['success']
    issued = int(NUM.match(number_of(dbx, s)).group(1))
    login_as(client, 'registrar')
    client.post('/admin/enrollment-checkin/control-numbers/next', data={'school_year': '2026-2027', 'next_number': '5000'})
    assert 'Only an administrator' in flashes(client)
    login_as(client, 'admin', name='Ady Admin')
    client.post('/admin/enrollment-checkin/control-numbers/next', data={'school_year': '2026-2027', 'next_number': str(issued)})
    assert 'already been issued' in flashes(client)
    client.post('/admin/enrollment-checkin/control-numbers/next', data={'school_year': '2026-2027', 'next_number': 'abc'})
    assert 'whole number' in flashes(client)
    target = max(issued + 1, last26(dbx) + 1) + 500
    client.post('/admin/enrollment-checkin/control-numbers/next', data={'school_year': '2026-2027', 'next_number': str(target)})
    assert 'PGPC-26-%04d' % target in flashes(client)
    t = ready_student(app, client, dbx, world)
    assert check_in_json(client, t, world).get_json()['success']
    assert number_of(dbx, t) == 'PGPC-26-%04d' % target
    log = dbx.one("SELECT details FROM enrollment_audit_log WHERE action='control_sequence' ORDER BY id DESC LIMIT 1")
    assert '"last_number_after": %d' % (target - 1) in log['details']


def test_backfill_for_students_checked_in_before_phase6(app, client, dbx, world, fees):
    s1, s2 = student_in(dbx, world), student_in(dbx, world)
    for st, when in ((s2, '2026-06-01 08:00:00'), (s1, '2026-06-01 09:00:00')):
        dbx.x("INSERT INTO enrollment_checkins (student_user_id, applicant_id, academic_term_id, active, checklist_snapshot, checked_in_by, checked_in_role, checked_in_at) "
              "VALUES (%s,%s,%s,1,'[]','old','registrar',%s)", (st['student_user_id'], st['applicant_id'], world['t1'], when))
    login_as(client, 'admin')
    page = client.get('/admin/enrollment-checkin/control-numbers').get_data(as_text=True)
    assert 'have no control number' in page
    login_as(client, 'registrar')
    client.post('/admin/enrollment-checkin/control-numbers/backfill')
    assert 'Only an administrator' in flashes(client)
    assert number_of(dbx, s1) is None
    login_as(client, 'admin', name='Ady Admin')
    client.post('/admin/enrollment-checkin/control-numbers/backfill')
    assert 'Issued' in flashes(client)
    n1, n2 = number_of(dbx, s1), number_of(dbx, s2)
    assert n1 and n2 and int(NUM.match(n2).group(1)) < int(NUM.match(n1).group(1))      # check-in order
    assert dbx.one("SELECT source FROM control_numbers WHERE number=%s", (n1,))['source'] == 'backfill'
    client.post('/admin/enrollment-checkin/control-numbers/backfill')
    assert 'already has a control number' in flashes(client)


def test_control_numbers_page_access(client, dbx):
    login_as(client, 'registrar')
    page = client.get('/admin/enrollment-checkin/control-numbers').get_data(as_text=True)
    assert 'Enrollment Control Numbers' in page and 'Set the next number' not in page
    login_as(client, 'admin')
    assert 'Set the next number' in client.get('/admin/enrollment-checkin/control-numbers').get_data(as_text=True)
    login_as(client, 'cashier')
    assert client.get('/admin/enrollment-checkin/control-numbers').status_code == 302
