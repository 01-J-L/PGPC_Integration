"""Assessment Description as a dropdown, maintained by Accounting."""

import re

import pytest

from conftest import login_as, _uniq
from test_phase3_regform import flashes                                          # noqa: F401
from test_phase4_finance import run, A, reset_policy                             # noqa: F401  (fixtures)

PAGE = '/cashier/batch-assessment'


def options(page):
    sel = re.search(r'<select id="description".*?</select>', page, re.S).group(0)
    return re.findall(r'<option value="([^"]+)"', sel)


def add(client, name):
    client.post('/accounting/enrollment-finance/fee-descriptions', data={'name': name})
    return flashes(client)


@pytest.fixture()
def fee(dbx):
    name = 'Lab Fee ' + _uniq('x')[-6:]
    yield name
    dbx.x("DELETE FROM fee_descriptions WHERE name=%s", (name,))


def test_accounting_adds_and_the_dropdown_shows_it(client, dbx, fee):
    login_as(client, 'accounting', name='Acct One')
    page = client.get(PAGE).get_data(as_text=True)
    assert 'id="fee-descriptions"' in page and fee not in options(page)
    assert 'added to' in add(client, '  ' + fee + '  ')                     # spaces trimmed
    assert fee in options(client.get(PAGE).get_data(as_text=True))
    assert 'already in the list' in add(client, fee.upper())                # case-insensitive duplicate
    assert 'Enter the fee description' in add(client, '   ')
    row = dbx.one("SELECT created_by FROM fee_descriptions WHERE name=%s", (fee,))
    assert row['created_by'] == 'Acct One'
    assert dbx.one("SELECT COUNT(*) n FROM finance_audit_log WHERE action='fee_description_add' AND details LIKE %s", ('%' + fee + '%',))['n'] == 1


def test_cashier_picks_but_cannot_add(client, dbx, fee):
    login_as(client, 'cashier')
    page = client.get(PAGE).get_data(as_text=True)
    assert '<select id="description"' in page and 'id="fee-descriptions"' not in page
    assert 'Only accounting' in add(client, fee)
    assert not dbx.one("SELECT id FROM fee_descriptions WHERE name=%s", (fee,))


def test_hide_and_show(client, dbx, fee):
    login_as(client, 'accounting', name='Acct One')
    add(client, fee)
    fid = dbx.one("SELECT id FROM fee_descriptions WHERE name=%s", (fee,))['id']
    client.post('/accounting/enrollment-finance/fee-descriptions/%d/hide' % fid)
    page = client.get(PAGE).get_data(as_text=True)
    assert fee not in options(page) and fee in page                          # still listed in the management card
    assert 'is back in' in add(client, fee)                                  # adding the same name shows it again
    assert fee in options(client.get(PAGE).get_data(as_text=True))
    login_as(client, 'cashier')
    client.post('/accounting/enrollment-finance/fee-descriptions/%d/hide' % fid)
    assert dbx.one("SELECT is_active FROM fee_descriptions WHERE id=%s", (fid,))['is_active'] == 1


def test_batch_create_only_accepts_listed_descriptions(client, dbx, fee):
    login_as(client, 'accounting', name='Acct One')
    form = {'total_amount': '250', 'semester': '', 'scope': '', 'course': 'No Such Course ' + fee, 'year_level': ''}
    client.post(PAGE, data=dict(form, description='Typed By Hand ' + fee))
    assert 'Choose the fee description from the list' in flashes(client)
    assert not dbx.one("SELECT id FROM batch_assessments_log WHERE description=%s", ('Typed By Hand ' + fee,))
    add(client, fee)
    client.post(PAGE, data=dict(form, description=fee))
    assert dbx.one("SELECT amount FROM batch_assessments_log WHERE description=%s", (fee,))


def test_batch_edit_keeps_a_hidden_current_name_but_refuses_free_text(client, dbx, fee):
    login_as(client, 'accounting', name='Acct One')
    add(client, fee)
    bid = dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) "
                "VALUES (%s, 100, '', %s, '', '')", (fee, 'No Such Course ' + fee))
    fid = dbx.one("SELECT id FROM fee_descriptions WHERE name=%s", (fee,))['id']
    client.post('/accounting/enrollment-finance/fee-descriptions/%d/hide' % fid)
    edit = {'amount': '120', 'semester': '', 'scope': '', 'course': 'No Such Course ' + fee, 'year_level': ''}
    client.post('/cashier/batch-assessment/edit/%d' % bid, data=dict(edit, description=fee))      # unchanged name: fine
    assert dbx.one("SELECT amount FROM batch_assessments_log WHERE id=%s", (bid,))['amount'] == 120
    client.post('/cashier/batch-assessment/edit/%d' % bid, data=dict(edit, description='Something typed'))
    assert 'Choose the fee description from the list' in flashes(client)
    assert dbx.one("SELECT description FROM batch_assessments_log WHERE id=%s", (bid,))['description'] == fee


def test_edit_button_survives_an_apostrophe(client, dbx):
    name = "Student's ID Fee " + _uniq('q')[-5:]
    dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) VALUES (%s, 50, '', 'None', '', '')", (name,))
    login_as(client, 'accounting')
    page = client.get(PAGE).get_data(as_text=True)
    assert 'data-description="Student&#39;s ID Fee' in page and "openEditModal(this)" in page


def test_migration_seed_takes_existing_descriptions(dbx):
    from website.schema_migrations import FEE_DESCRIPTION_SEED_SQL
    name = 'Seeded Fee ' + _uniq('s')[-6:]
    dbx.x("INSERT INTO batch_assessments_log (description, amount, semester, course, year_level, scope) VALUES (%s, 10, '', 'None', '', '')", ('  ' + name,))
    dbx.x(FEE_DESCRIPTION_SEED_SQL)
    dbx.x(FEE_DESCRIPTION_SEED_SQL)
    rows = dbx.q("SELECT name, is_active, created_by FROM fee_descriptions WHERE name=%s", (name,))
    assert rows == [{'name': name, 'is_active': 1, 'created_by': 'migration 0011'}]
    dbx.x("DELETE FROM fee_descriptions WHERE name=%s", (name,))
