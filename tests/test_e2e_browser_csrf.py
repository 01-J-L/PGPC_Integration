"""End-to-end browser test with CSRF protection ON (Phase 1, P1-01).

Drives the real pages in headless Chrome so the _csrf.html shim is exercised
exactly as a registrar/cashier/student/dean would use it: plain form posts,
fetch() JSON calls and multipart fetch() uploads.

Skipped automatically when Playwright or Chrome is not installed.
    pip install playwright   (uses the system Chrome at CHROME_PATH)
"""

import os
import shutil
import threading

import pytest

from conftest import make_student

playwright = pytest.importorskip('playwright.sync_api')
CHROME = os.environ.get('CHROME_PATH') or shutil.which('google-chrome') or shutil.which('chromium')
if not CHROME:
    pytest.skip('Chrome not available', allow_module_level=True)

PASSWORD = 'E2e-Passw0rd!'


@pytest.fixture(scope='module')
def server(app):
    from werkzeug.serving import make_server
    app.config['WTF_CSRF_ENABLED'] = True
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield 'http://127.0.0.1:%d' % srv.server_port
    srv.shutdown()
    app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture(scope='module')
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        yield b
        b.close()


def _staff(dbx, username, role, system='admission'):
    from werkzeug.security import generate_password_hash
    dbx.x("DELETE FROM users WHERE username=%s", (username,))
    dbx.x("INSERT INTO users (username, email, password_hash, role, status, full_name, `system`, can_edit) "
          "VALUES (%s, %s, %s, %s, 'active', %s, %s, 1)",
          (username, username + '@example.com', generate_password_hash(PASSWORD), role, username.title(), system))


def _new_page(browser):
    page = browser.new_page()
    page.on('dialog', lambda d: d.accept())
    return page


def test_student_login_form(server, browser, dbx, program):
    s = make_student(dbx, program)
    page = _new_page(browser)
    # The login page has a client-side captcha drawn from Math.random(); pin it
    # so the code is always "AAAAA".
    page.add_init_script("Math.random = function () { return 0; };")
    page.goto(server + '/student-login')
    page.fill('#student-login-form input[name="email"]', s['email'])
    page.fill('#student-login-form input[name="password"]', 'Student123!')
    page.fill('#captchaInput', 'AAAAA')
    with page.expect_navigation():
        page.click('#student-login-form [type="submit"]')
    assert '/student-login' not in page.url, page.content()[:500]
    page.close()


def test_registrar_checklist_fetch_and_upload(server, browser, dbx, program, active_term):
    s = make_student(dbx, program)
    _staff(dbx, 'e2e_registrar', 'registrar')
    page = _new_page(browser)
    page.goto(server + '/admin')
    page.fill('input[name="username"]', 'e2e_registrar')
    page.fill('input[name="password"]', PASSWORD)
    with page.expect_navigation():
        page.click('form[action$="/admin_login"] [type="submit"]')

    page.goto(server + '/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], active_term['id']))
    # Phase 5: plain form POST (hidden token added by the CSRF shim): approve the documents line
    with page.expect_navigation():
        page.click('#chk-documents button:has-text("Approve")')
    item = dbx.one("SELECT i.status, i.approved_by FROM enrollment_checklist_items i JOIN enrollment_checklist_requirements r "
                   "ON r.id = i.requirement_id WHERE r.code='documents' AND i.student_user_id=%s AND i.academic_term_id=%s",
                   (s['student_user_id'], active_term['id']))
    assert item and item['status'] == 'approved'
    # fetch() JSON POST with the X-CSRFToken header: the old checkbox endpoint (registrar may not clear Accounting)
    status = page.evaluate("""async (args) => { const r = await fetch('/admin/enrollment/' + args[0] + '/toggle-approval',
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({role: 'accounting', status: true, term_id: args[1]})});
        return [r.status, await r.text()]; }""", [s['applicant_id'], active_term['id']])
    assert status[0] == 400 and 'accounting or admin' in status[1], status   # reached the view: not a CSRF 400 page
    assert 'CSRF' not in status[1]

    # multipart fetch() POST: upload a document from the Documents tab
    page.goto(server + '/admin/enrollment-review/%d?term_id=%d' % (s['applicant_id'], active_term['id']))
    page.click('a[data-target="documents"]')
    form = page.locator('form.upload-form[data-doc-type="good_moral"]')
    form.locator('input[type="file"]').set_input_files(
        files=[{'name': 'gm.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-1.4\n%%EOF'}])
    with page.expect_response(lambda r: 'upload-doc' in r.url) as up:
        form.locator('button[type="submit"]').click()
    assert up.value.status == 200, up.value.text()
    assert dbx.one("SELECT enrollment_good_moral_filename f FROM applicants WHERE applicant_id=%s",
                   (s['applicant_id'],))['f'] == 'gm.pdf'
    page.close()


def test_cashier_payment_form(server, browser, dbx, program):
    s = make_student(dbx, program)
    aid = dbx.x("INSERT INTO assessments (student_id, description, total_amount, amount_paid, balance, status) "
                "VALUES (%s, 'Tuition Fee', 7800.00, 0, 7800.00, 'unpaid')", (s['applicant_id'],))
    dbx.x("INSERT INTO system_settings (setting_key, setting_value) VALUES ('issuance_of_receipt','false') "
          "ON DUPLICATE KEY UPDATE setting_value='false'")
    _staff(dbx, 'e2e_cashier', 'cashier')
    page = _new_page(browser)
    page.goto(server + '/admin')
    page.fill('input[name="username"]', 'e2e_cashier')
    page.fill('input[name="password"]', PASSWORD)
    with page.expect_navigation():
        page.click('form[action$="/admin_login"] [type="submit"]')
    page.goto(server + '/cashier/payment/%d' % s['applicant_id'])
    page.select_option('#assessment_id', str(aid))
    page.fill('#amount', '1000')
    with page.expect_navigation():
        page.click('#paymentForm [type="submit"]')
    row = dbx.one("SELECT amount_paid, balance FROM assessments WHERE id=%s", (aid,))
    assert float(row['amount_paid']) == 1000.0 and float(row['balance']) == 6800.0
    page.close()


def test_faculty_login_flaskform(server, browser, dbx):
    _staff(dbx, 'e2e_dean', 'dean', system='faculty')
    page = _new_page(browser)
    page.goto(server + '/admin-load/login')
    page.fill('input[name="email"]', 'e2e_dean')
    page.fill('input[name="password"]', PASSWORD)
    with page.expect_navigation():
        page.click('input[type="submit"], button[type="submit"]')
    assert '/faculty/dashboard' in page.url, page.url
    page.close()


def test_forged_cross_site_post_is_blocked(server, browser, dbx, program):
    """A page on another origin auto-submitting a form must not change data."""
    s = make_student(dbx, program)
    _staff(dbx, 'e2e_registrar2', 'registrar')
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(server + '/admin')
    page.fill('input[name="username"]', 'e2e_registrar2')
    page.fill('input[name="password"]', PASSWORD)
    with page.expect_navigation():
        page.click('form[action$="/admin_login"] [type="submit"]')
    # Simulate the attacker page: a form posting straight to the app without a token.
    page.set_content('<form id="f" method="POST" action="%s/admin/application/%d/delete"></form>'
                     % (server, s['applicant_id']))
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_load_state()
    assert dbx.one("SELECT applicant_id FROM applicants WHERE applicant_id=%s", (s['applicant_id'],)) is not None
    ctx.close()
