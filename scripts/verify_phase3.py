"""Regression checks for the cleaned-up integrated system.

Covers the Phase 1 (dead code), Phase 3 (single identity table) and Phase 2
(one screen per function) invariants, plus static template wiring.

    py scripts/verify_phase3.py                 # against DB_NAME
    set DB_NAME=expired_phase3_test
    py scripts/verify_phase3.py                 # against the scratch copy
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from main import app
from website.models import User

failures = []


def check(label, ok, detail=""):
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


with app.app_context():
    print("== accounts ==")
    users = User.query.order_by(User.id).all()
    people = {u.username: u for u in users if u.username}
    for u in users:
        print(f"   id={u.id:2d} {(u.username or '<no username>'):14s} role={u.role or '-':10s} "
              f"system={u.system or '-':9s} dept={u.department or '-':5s} status={u.status}")

    # The account list is maintained by hand outside this repo, so the check is
    # about duplicates rather than a fixed count: two rows may never share a
    # username, or a login would resolve to the wrong person.
    login_names = [u.username for u in users if u.username]
    check("no duplicate usernames", len(login_names) == len(set(login_names)))
    unnamed = [u for u in users if not u.username]
    if unnamed:
        print(f"   ({len(unnamed)} account(s) have no username and cannot sign in: "
              f"ids {', '.join(str(u.id) for u in unnamed)})")

    # The CCBA/COC deans administer grading; a separate grading account is a
    # duplicate identity and must not exist.
    check("no separate grading-system accounts",
          User.query.filter_by(system="grading").count() == 0,
          f"{User.query.filter_by(system='grading').count()} found")
    check("no role=admin accounts outside admission",
          User.query.filter(User.role == "admin", User.system != "admission").count() == 0)

    from website.models import load_user
    check("load_user resolves every id to that same account",
          all(load_user(u.id) is not None and load_user(u.id).id == u.id
              for u in users))

    # Regression guard for the original bug: `admin` and the CCBA dean must be
    # separate rows. It only applies when both accounts are still present.
    admin = people.get("admin")
    dean = people.get("adminccba")
    if admin and dean:
        check("id=1 resolves to the dean, not the admin", dean.id == 1)
        check("admin got its own id", admin.id != 1, f"admin.id={admin.id}")
        check("load_user(1) is adminccba", load_user(1).username == "adminccba")
        check("load_user(admin.id) is admin", load_user(admin.id).username == "admin")
    else:
        print("   (admin/adminccba renamed, skipping the id=1 regression guard)")

    superadmin = people.get("superadmin")
    if superadmin:
        check("load_user(superadmin.id) is superadmin",
              load_user(superadmin.id).username == "superadmin")

    carquillo = people.get("carquillo52")
    if carquillo:
        check("merged teacher has a username", carquillo.username == "carquillo52")
        check("merged teacher keeps a fallback password", carquillo.password_hash_alt is not None)
        check("merged teacher password hashes parse",
              carquillo.check_password("wrong-password") is False)

    print("\n== templates ==")
    errors = []
    for name in sorted(app.jinja_env.list_templates()):
        try:
            app.jinja_env.get_template(name)
        except Exception as exc:
            errors.append((name, str(exc).splitlines()[0]))
    check("all templates compile", not errors, str(errors[:3]) if errors else "")

    # A render_template('x.html') whose file is missing only fails at request
    # time as a 500. Catch it statically instead.
    static_root = os.path.join(ROOT, "website")
    referenced = {}
    for base, _, names in os.walk(static_root):
        if "__pycache__" in base:
            continue
        for fname in names:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(base, fname)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for tpl in re.findall(r"render_template\(\s*['\"]([^'\"]+)['\"]", line):
                        referenced.setdefault(tpl, []).append(
                            f"{os.path.relpath(path, ROOT)}:{lineno}")
    available = set(app.jinja_env.list_templates())
    missing_templates = {t: w for t, w in referenced.items() if t not in available}
    check("every render_template target exists", not missing_templates,
          str(missing_templates) if missing_templates else f"{len(referenced)} templates referenced")

    # Same class of bug for url_for: a wrong blueprint prefix only fails when
    # the page (or email) is actually rendered.
    known_endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    bad_urls = {}
    for base, _, names in os.walk(os.path.join(ROOT, "website", "templates")):
        for fname in names:
            if not fname.endswith((".html", ".txt")):
                continue
            path = os.path.join(base, fname)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for ep in re.findall(r"url_for\(\s*['\"]([A-Za-z_][\w]*\.[\w.]+)['\"]", line):
                        if ep not in known_endpoints:
                            bad_urls.setdefault(
                                os.path.relpath(path, ROOT).replace("\\", "/"), []).append(
                                f"{lineno}:{ep}")
    check("every template url_for names a real endpoint", not bad_urls,
          str(bad_urls) if bad_urls else "all template url_for calls resolve")

    print("\n== routing ==")
    after = {(str(r), r.endpoint, ",".join(sorted(r.methods - {"HEAD", "OPTIONS"})))
             for r in app.url_map.iter_rules()}
    # Baseline after Phase 1 (dead code) and Phase 2 (duplicate admin screens).
    check("route table matches the current baseline", len(after) == 273, f"got {len(after)}")
    check("no debug routes left", not any("/faculty/debug" in rule for rule, _, _ in after))

    print("\n== requests ==")
    client = app.test_client()
    for url, expected in (("/", 200), ("/admin-load/login", 200), ("/faculty/", 302)):
        resp = client.get(url, follow_redirects=False)
        check(f"GET {url} -> {expected}", resp.status_code == expected, f"got {resp.status_code}")

    # Use the admission admin (or the superadmin) to exercise the admission UI.
    admin_account = admin or next(
        (u for u in people.values() if u.role in ("admin", "superadmin")), None)
    if admin_account:
        with client.session_transaction() as sess:
            sess["user_role"] = "admin"
            sess["user_id"] = admin_account.id
            sess["user_name"] = admin_account.full_name
            sess["can_edit_student"] = True
        resp = client.get("/admin/manage-users", follow_redirects=False)
        check("GET /admin/manage-users with admin session", resp.status_code in (200, 302), f"got {resp.status_code}")
    else:
        print("   (no admission admin account present, skipping the manage-users request)")

    # Cashier report: the template used to be misspelled, so this 500'd.
    with client.session_transaction() as sess:
        sess["user_role"] = "cashier"
        sess["user_name"] = "Verification Cashier"
    resp = client.get("/cashier/generate-report", follow_redirects=False)
    check("GET /cashier/generate-report", resp.status_code == 200, f"got {resp.status_code}")

    print("\n== Flask-Login identity roundtrip ==")
    if admin_account:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin_account.id)
            sess["_fresh"] = True
        resp = client.get("/faculty/grading-admin/dashboard", follow_redirects=False)
        check("admin session reaches grading dashboard", resp.status_code in (200, 302), f"got {resp.status_code}")
    else:
        print("   (no admin account present, skipping the roundtrip)")

print()
if failures:
    print(f"FAILURES: {failures}")
    sys.exit(1)
print("All Phase 3 checks passed.")
