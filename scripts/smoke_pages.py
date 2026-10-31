"""Render smoke test: logs in as each role and GETs the key pages.

Uses throwaway accounts in the current database, then removes them.

    py scripts/smoke_pages.py
"""

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from main import app
from website import db
from website.models import User, Subject, Section, FacultyAssignment

app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True
PASSWORD = "Phase3-Test-Passw0rd!"

ACCOUNTS = [
    ("sm_dean", "dean", "faculty", "Smoke Dean", "CCBA"),
    ("sm_teacher", "faculty", "faculty", "Smoke Teacher", "CCBA"),
    ("sm_super", "superadmin", "admission", "Smoke Superadmin", None),
]

PAGES = {
    "sm_teacher": [
        "/faculty/dashboard",
        "/faculty/grades/dashboard",
        "/faculty/grades/manage_classes",
        "/faculty/grades/grade_management",
        "/faculty/my-schedule",
        "/faculty/notifications",
        "/faculty/my-analytics",
    ],
    "sm_dean": [
        "/faculty/dashboard",
        "/faculty/approvals",
        "/faculty/manage-programs",
        "/faculty/users",
        "/faculty/analytics",
        "/faculty/system-settings",
        "/faculty/room-schedule",
        # The dean is also the grading administrator for their department.
        "/faculty/grading-admin/dashboard",
        "/faculty/grading-admin/student_records",
        "/faculty/grading-admin/faculty_records",
        "/faculty/grading-admin/approvals",
        "/faculty/grading-admin/programs",
    ],
    "sm_super": [
        "/faculty/grading-admin/maintenance",
        "/faculty/grading-admin/dashboard",
    ],
}

# Phase 2: grading-admin screens that were folded into the dean UI must 302 to
# their canonical replacement instead of rendering a second copy.
REDIRECTS = {
    "sm_dean": [
        ("/faculty/grading-admin/faculty_records", "/faculty/users"),
        ("/faculty/grading-admin/add_faculty", "/faculty/users"),
        ("/faculty/grading-admin/approvals", "/faculty/approvals"),
        ("/faculty/grading-admin/programs", "/faculty/manage-programs"),
        ("/faculty/grading-admin/programs/add", "/faculty/manage-programs"),
        # The academic term has one screen: /faculty/system-settings.
        ("/faculty/grading-admin/settings", "/faculty/system-settings"),
    ],
}

failures = []


def check(label, ok, detail=""):
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def seed():
    with app.app_context():
        for username, *_ in ACCOUNTS:
            existing = User.query.filter_by(username=username).first()
            if existing:
                db.session.delete(existing)
        db.session.commit()
        for username, role, system, full_name, dept in ACCOUNTS:
            u = User(username=username, email=f"{username}@test.local", full_name=full_name,
                     role=role, status="active", system=system, department=dept)
            u.set_password(PASSWORD)
            db.session.add(u)
        db.session.commit()


def cleanup():
    with app.app_context():
        for username, *_ in ACCOUNTS:
            u = User.query.filter_by(username=username).first()
            if u:
                db.session.delete(u)
        # Anything the functional checks created, in case they aborted early.
        for u in User.query.filter(User.username.like("sm\\_%")).all():
            FacultyAssignment.query.filter_by(faculty_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()


def phase2_checks():
    """Assertions for the collapsed admin UI."""
    print("== phase 2: one screen per function ==")
    with app.app_context():
        teacher_id = User.query.filter_by(username="sm_teacher").first().id
        program_id = "bscs"

    # The faculty list is the single place that can reach the load-assignment
    # screen (previously only the deleted grading faculty list linked there).
    with app.test_client() as client:
        client.post("/admin-load/login", data={"email": "sm_dean", "password": PASSWORD},
                    follow_redirects=True)
        resp = client.get("/faculty/users")
        check("/faculty/users links to Assign Loads",
              resp.status_code == 200 and b"Assign Loads" in resp.data, f"got {resp.status_code}")

        resp = client.get(f"/faculty/grading-admin/faculty/{teacher_id}/assign")
        check("assign-loads screen renders", resp.status_code == 200, f"got {resp.status_code}")

        resp = client.get(f"/faculty/grading-admin/programs/{program_id}")
        check("program subjects screen renders", resp.status_code == 200, f"got {resp.status_code}")

        resp = client.get("/faculty/manage-programs")
        check("manage-programs links to Subjects",
              resp.status_code == 200 and b"Subjects" in resp.data, f"got {resp.status_code}")

        # Dean is not the superadmin, so the destructive force-delete is hidden.
        check("force delete hidden from a dean",
              b"Force Delete" not in resp.data, "button was rendered")

        # The sidebar must not list the screens that were folded away.
        for stale in (b"Grading Faculty", b"Grading Programs"):
            check(f"sidebar has no '{stale.decode()}' entry", stale not in resp.data, "entry still present")

    with app.test_client() as client:
        client.post("/admin-load/login", data={"email": "sm_super", "password": PASSWORD},
                    follow_redirects=True)
        resp = client.get("/faculty/manage-programs")
        check("force delete available to superadmin",
              resp.status_code == 200 and b"Force Delete" in resp.data, f"got {resp.status_code}")
        check("grading-admin approvals entry gone from sidebar",
              b"grading-admin/approvals" not in resp.data, "stale link still present")

    # The merged approval action must clear both gates (status + edit rights).
    with app.app_context():
        pending = User(username="sm_pending", email="sm_pending@test.local",
                       full_name="Smoke Pending", role="faculty", status="pending",
                       system="faculty", can_edit=False)
        pending.set_password(PASSWORD)
        db.session.add(pending)
        db.session.commit()
        pending_id = pending.id

    with app.test_client() as client:
        client.post("/admin-load/login", data={"email": "sm_dean", "password": PASSWORD},
                    follow_redirects=True)
        resp = client.get("/faculty/approvals")
        check("pending account appears in the queue",
              resp.status_code == 200 and b"Smoke Pending" in resp.data, f"got {resp.status_code}")
        client.post(f"/faculty/approve/{pending_id}", follow_redirects=True)

    with app.app_context():
        approved = User.query.get(pending_id)
        check("approve clears status and grants edit rights",
              approved is not None and approved.status == "active" and approved.can_edit is True,
              f"status={getattr(approved, 'status', None)} can_edit={getattr(approved, 'can_edit', None)}")
        db.session.delete(approved)
        db.session.commit()

    # delete_user must remove the faculty's assignments first (no ON DELETE
    # CASCADE on faculty_assignments.faculty_id / grades.faculty_id).
    with app.app_context():
        victim = User(username="sm_victim", email="sm_victim@test.local",
                      full_name="Smoke Victim", role="faculty", status="active",
                      system="faculty")
        victim.set_password(PASSWORD)
        db.session.add(victim)
        db.session.commit()
        subject = Subject.query.first()
        section = Section.query.first()
        if subject and section:
            db.session.add(FacultyAssignment(
                faculty_id=victim.id, subject_id=subject.id, section_id=section.id))
            db.session.commit()
        victim_id = victim.id
        has_assignment = FacultyAssignment.query.filter_by(faculty_id=victim_id).count() > 0

    with app.test_client() as client:
        client.post("/admin-load/login", data={"email": "sm_dean", "password": PASSWORD},
                    follow_redirects=True)
        resp = client.post(f"/faculty/user/delete/{victim_id}", follow_redirects=True)
        check("delete_user succeeds for a faculty with assignments",
              resp.status_code == 200, f"got {resp.status_code}")

    with app.app_context():
        gone = User.query.get(victim_id) is None
        leftover = FacultyAssignment.query.filter_by(faculty_id=victim_id).count()
        check("user and their assignments were removed",
              gone and leftover == 0, f"user_gone={gone} leftover_assignments={leftover}"
              + ("" if has_assignment else " (no fixture assignment)"))

    print()


def main():
    seed()
    try:
        for username, pages in PAGES.items():
            print(f"== {username} ==")
            with app.test_client() as client:
                r = client.post("/admin-load/login", data={"email": username, "password": PASSWORD},
                                follow_redirects=True)
                check(f"{username} logged in", r.status_code == 200, f"got {r.status_code}")
                for page in pages:
                    try:
                        resp = client.get(page, follow_redirects=False)
                        ok = resp.status_code in (200, 302)
                        check(f"GET {page}", ok, f"got {resp.status_code}")
                    except Exception as exc:
                        check(f"GET {page}", False, f"{type(exc).__name__}: {exc}")
                        traceback.print_exc(limit=3)

                for source, target in REDIRECTS.get(username, []):
                    resp = client.get(source, follow_redirects=False)
                    location = resp.headers.get("Location", "")
                    check(f"GET {source} -> {target}",
                          resp.status_code == 302 and target in location,
                          f"got {resp.status_code} -> {location}")
            print()

        phase2_checks()
    finally:
        cleanup()

    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   ", f)
        return 1
    print("All pages rendered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
