"""End-to-end login test for Phase 3 (run against a scratch database).

Creates three throwaway accounts, exercises both login portals, then removes
them again.

    set DB_NAME=expired_phase3_test
    py scripts/test_logins.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from main import app
from website import db
from website.models import User

app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True

PASSWORD = "Phase3-Test-Passw0rd!"
TEMP = [
    ("p3_admission", "registrar", "admission", "P3 Registrar"),
    ("p3_dean", "dean", "faculty", "P3 Dean"),
    ("p3_teacher", "faculty", "faculty", "P3 Teacher"),
]

failures = []


def check(label, ok, detail=""):
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def seed():
    with app.app_context():
        for username, role, system, full_name in TEMP:
            existing = User.query.filter_by(username=username).first()
            if existing:
                db.session.delete(existing)
        db.session.commit()
        for username, role, system, full_name in TEMP:
            u = User(
                username=username,
                email=f"{username}@test.local",
                full_name=full_name,
                role=role,
                status="active",
                system=system,
                department="CCBA" if system == "faculty" else None,
            )
            u.set_password(PASSWORD)
            db.session.add(u)
        db.session.commit()


def cleanup():
    with app.app_context():
        for username, *_ in TEMP:
            u = User.query.filter_by(username=username).first()
            if u:
                db.session.delete(u)
        db.session.commit()


def post_login(client, url, identifier, password):
    return client.post(url, data={"username": identifier, "password": password,
                                  "email": identifier}, follow_redirects=False)


def main():
    seed()
    try:
        print("== admission portal (/admin_login) ==")
        with app.test_client() as client:
            r = post_login(client, "/admin_login", "p3_admission", PASSWORD)
            check("admission account logs in", r.status_code == 302 and "/admin/dashboard" in r.headers.get("Location", ""),
                  f"{r.status_code} -> {r.headers.get('Location')}")

        with app.test_client() as client:
            r = post_login(client, "/admin_login", "p3_teacher", PASSWORD)
            location = r.headers.get("Location", "")
            check("teacher is refused by the admission portal", r.status_code == 302 and "/admin/dashboard" not in location,
                  f"{r.status_code} -> {location}")

        with app.test_client() as client:
            r = post_login(client, "/admin_login", "p3_dean", PASSWORD)
            location = r.headers.get("Location", "")
            check("dean is refused by the admission portal", r.status_code == 302 and "/admin/dashboard" not in location,
                  f"{r.status_code} -> {location}")

        print("\n== faculty portal (/admin-load/login) ==")
        with app.test_client() as client:
            r = post_login(client, "/admin-load/login", "p3_teacher", PASSWORD)
            check("teacher logs in", r.status_code == 302 and "/faculty/dashboard" in r.headers.get("Location", ""),
                  f"{r.status_code} -> {r.headers.get('Location')}")

        with app.test_client() as client:
            r = post_login(client, "/admin-load/login", "p3_dean", PASSWORD)
            check("dean logs in", r.status_code == 302 and "/faculty/dashboard" in r.headers.get("Location", ""),
                  f"{r.status_code} -> {r.headers.get('Location')}")

        # There is no separate grading-admin account: the dean administers the
        # grading screens for their department through the same login.
        with app.test_client() as client:
            post_login(client, "/admin-load/login", "p3_dean", PASSWORD)
            r = client.get("/faculty/grading-admin/dashboard", follow_redirects=False)
            check("dean reaches the grading dashboard", r.status_code == 200, f"got {r.status_code}")

        with app.test_client() as client:
            r = post_login(client, "/admin-load/login", "p3_admission", PASSWORD)
            location = r.headers.get("Location", "")
            check("admission account is refused by the faculty portal",
                  r.status_code == 302 and "/faculty/dashboard" not in location,
                  f"{r.status_code} -> {location}")

        print("\n== wrong password ==")
        with app.test_client() as client:
            r = post_login(client, "/admin_login", "p3_admission", "nope")
            check("bad password is rejected", r.status_code == 302 and "/admin/dashboard" not in r.headers.get("Location", ""),
                  f"{r.status_code} -> {r.headers.get('Location')}")
    finally:
        cleanup()

    print()
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("All login checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
