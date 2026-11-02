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


def scheduling_checks():
    """The section grid must stay a usable hourly grid and reject clashes.

    Runs against a throwaway section ("<year>Z") and removes every row it
    creates, so the live schedule is untouched.
    """
    print("== section scheduling ==")
    from datetime import time as dtime

    with app.app_context():
        from website.faculty.models import Program, Course, TeachingLoad, Room, SystemSetting
        program = Program.query.first()
        teacher = User.query.filter_by(username="sm_teacher").first()
        if not program or not teacher:
            print("   (no program/teacher fixture, skipping)")
            return
        program_id = program.program_id
        course = Course.query.filter_by(program_id=program_id, semester=1).first()
        room = Room.query.order_by(Room.room_name).first()
        sy = SystemSetting.query.filter_by(key="school_year").first()
        school_year = sy.value.strip() if sy and sy.value else None
        year = course.year if course else 1
        course_id = course.id if course else None
        teacher_id = teacher.id
        rname = room.room_name if room else None
        syv = school_year
        section_name = f"{year}Z"
        full_section = f"{program.program_code} {section_name}"

    url = f"/faculty/section-grid/prog{program_id}/sec{section_name}/sem1"

    def purge():
        with app.app_context():
            from website.faculty.models import TeachingLoad
            TeachingLoad.query.filter_by(section=full_section).delete()
            db.session.commit()

    purge()
    try:
        with app.test_client() as client:
            client.post("/admin-load/login", data={"email": "sm_dean", "password": PASSWORD},
                        follow_redirects=True)
            resp = client.get(url)
            body = resp.data
            check("section grid renders", resp.status_code == 200, f"got {resp.status_code}")
            rows = body.count(b"whitespace-nowrap text-xs font-medium")
            # A single long class must not collapse the day into one row.
            check("empty grid keeps hourly rows", rows >= 10, f"{rows} rows")
            check("'Add Time Row' control is gone", b"Add Time Row" not in body)
            check("empty cells open the add modal", b"openAddModal(" in body)
            check("availability URL resolves", b"/faculty/api/check-availability" in body)
            check("edit modal still lets you change the day", b'id="edit_day"' in body)
            check("add modal takes the day from the clicked cell",
                  b'name="day" id="add_day"' in body
                  and b'id="add_day" class="w-full' not in body)
            check("start/end times are dropdowns",
                  b'id="add_start" name="time_start"' in body
                  and b'id="edit_start" name="time_start"' in body)
            check("no native time spinners remain", b'type="time"' not in body)
            check("quick length buttons are present",
                  b"setDuration(false, 90)" in body and b"setDuration(true, 90)" in body)
            check("time dropdowns cover the school day",
                  body.count(b'<option value="07:00">') >= 4,
                  f"{body.count(b'<option value="07:00">')} options")
            # hidden_tag() would render schedule_id a second time, and browsers
            # submit the first (empty) value, so the edit was rejected.
            id_inputs = body.count(b'name="schedule_id"')
            check("edit form renders exactly one schedule_id input",
                  id_inputs == 1, f"{id_inputs} inputs")

            if not (course and room and school_year):
                print("   (no course/room/school year fixture, skipping the conflict checks)")
                return

            # A class keeps its own frame: 07:00-10:00 is one row and one card,
            # not three hourly rows. A class straight after it keeps its own row
            # too (back-to-back classes must not be merged).
            with app.app_context():
                from website.faculty.models import TeachingLoad
                for start_h, end_h in ((7, 10), (10, 13)):
                    db.session.add(TeachingLoad(
                        course_code=course.course_code, course_description=course.course_description,
                        section=full_section, day="Wednesday",
                        time_start=dtime(start_h, 0), time_end=dtime(end_h, 0),
                        room=room.room_name, school_year=school_year,
                        is_published=False, user_id=teacher.id))
                db.session.commit()
            body = client.get(url).data
            check("a 7:00-10:00 class is a single fixed-frame row",
                  b"7:00 - 10:00 AM" in body)
            check("a back-to-back class keeps its own row",
                  b"10:00 AM - 1:00 PM" in body)
            check("both back-to-back cards are drawn",
                  body.count(b"data-schedule-id=") == 2,
                  f"{body.count(b'data-schedule-id=')} cards")
            purge()

            # Overlapping classes on different days still need both cards.
            with app.app_context():
                from website.faculty.models import TeachingLoad
                for day, start_h, end_h in (("Monday", 8, 10), ("Tuesday", 9, 11)):
                    db.session.add(TeachingLoad(
                        course_code=course.course_code, course_description=course.course_description,
                        section=full_section, day=day,
                        time_start=dtime(start_h, 0), time_end=dtime(end_h, 0),
                        room=room.room_name, school_year=school_year,
                        is_published=False, user_id=teacher.id))
                db.session.commit()
            body = client.get(url).data
            check("overlapping classes on different days both render",
                  body.count(b"data-schedule-id=") == 2,
                  f"{body.count(b'data-schedule-id=')} cards")
            check("the overlap adds the needed row cuts",
                  b"8:00 - 9:00 AM" in body and b"9:00 - 10:00 AM" in body)
            purge()

            # A class that runs to the bottom of the day must not leave the grid
            # as a single row; there is always one free row underneath it.
            with app.app_context():
                from website.faculty.models import TeachingLoad
                db.session.add(TeachingLoad(
                    course_code=course.course_code, course_description=course.course_description,
                    section=full_section, day="Thursday",
                    time_start=dtime(7, 0), time_end=dtime(22, 0),
                    room=room.room_name, school_year=school_year,
                    is_published=False, user_id=teacher.id))
                db.session.commit()
            body = client.get(url).data
            check("a full-day class is one row",
                  b"7:00 AM - 10:00 PM" in body)
            check("a free row is kept below it", b"10:00 - 11:00 PM" in body)
            row_cells = body.count(b"whitespace-nowrap text-xs font-medium")
            check("the grid is not collapsed to one row", row_cells >= 2,
                  f"{row_cells} rows")
            purge()

            with app.app_context():
                from website.faculty.models import TeachingLoad
                db.session.add(TeachingLoad(
                    course_code=course.course_code, course_description=course.course_description,
                    section=full_section, day="Saturday",
                    time_start=dtime(8, 0), time_end=dtime(9, 0),
                    room=room.room_name, school_year=school_year,
                    is_published=False, user_id=teacher.id))
                db.session.commit()

            def add(day, start, end):
                return client.post(url, data={
                    "add_schedule_submit": "1", "day": day,
                    "course": str(course.id), "faculty": str(teacher.id),
                    "time_start": start, "time_end": end, "room": room.room_name,
                }, follow_redirects=True).data

            body = add("Saturday", "08:30", "09:30")
            check("busy teacher blocks the add", b"is busy at that time" in body)
            with app.app_context():
                from website.faculty.models import TeachingLoad
                rows_after_clash = TeachingLoad.query.filter_by(section=full_section).count()
            check("the clash created no row", rows_after_clash == 1, f"{rows_after_clash} rows")

            body = add("Saturday", "10:00", "09:00")
            check("end before start is rejected", b"end time must be after the start time" in body)

            body = add("Saturday", "10:00", "11:00")
            check("free slot is accepted", b"Class added as DRAFT" in body)

            with app.app_context():
                from website.faculty.models import TeachingLoad
                created = TeachingLoad.query.filter_by(
                    section=full_section, day="Saturday").count()
                fixture_id = TeachingLoad.query.filter_by(
                    section=full_section, day="Saturday",
                    time_start=dtime(8, 0)).first().id
            check("fixture + accepted class are both stored", created == 2, f"{created} rows")

            # Round-trip an edit: same teacher/room, moved to a free slot.
            # Find a day where neither the room nor the teacher is taken, so the
            # assertion does not depend on the live timetable.
            from sqlalchemy import or_
            with app.app_context():
                from website.faculty.models import TeachingLoad, Course as C
                target = None
                for candidate in ("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"):
                    clash = (TeachingLoad.query
                             .join(C, TeachingLoad.course_code == C.course_code)
                             .filter(C.semester == 1,
                                     TeachingLoad.school_year == syv,
                                     TeachingLoad.id != fixture_id,
                                     TeachingLoad.day == candidate,
                                     TeachingLoad.time_start < dtime(14, 0),
                                     TeachingLoad.time_end > dtime(13, 0),
                                     or_(TeachingLoad.room == rname,
                                         TeachingLoad.user_id == teacher_id))
                             .first())
                    if not clash:
                        target = candidate
                        break
            check("a free day was found for the edit", target is not None)

            body = client.post(url, data={
                "update_schedule_submit": "1", "schedule_id": str(fixture_id),
                "day": target or "Saturday", "course": str(course_id),
                "faculty": str(teacher_id),
                "time_start": "13:00", "time_end": "14:00", "room": rname,
            }, follow_redirects=True).data
            check("editing a class is accepted", b"Schedule updated." in body)
            with app.app_context():
                from website.faculty.models import TeachingLoad
                edited = db.session.get(TeachingLoad, fixture_id)
                moved = (edited is not None and edited.day == (target or "Saturday")
                         and edited.time_start == dtime(13, 0)
                         and edited.time_end == dtime(14, 0))
            check("the edit is persisted", moved)

            # The edited row must now block its own slot.
            body = client.post(url, data={
                "update_schedule_submit": "1", "schedule_id": str(fixture_id),
                "day": target or "Saturday", "course": str(course_id),
                "faculty": str(teacher_id),
                "time_start": "13:30", "time_end": "14:30", "room": rname,
            }, follow_redirects=True).data
            check("a clash on edit is still rejected",
                  b"already has a class at that time" in body or b"is busy at that time" in body)
    finally:
        purge()
        print()


def dark_mode_checks():
    """The grading screens must carry dark: variants, not just light colours."""
    print("== dark mode ==")
    pages = {
        "sm_teacher": [
            "/faculty/grades/dashboard",
            "/faculty/grades/manage_classes",
            "/faculty/grades/grade_management",
        ],
        "sm_dean": [
            "/faculty/grading-admin/dashboard",
            "/faculty/grading-admin/student_records",
        ],
    }
    for username, urls in pages.items():
        with app.test_client() as client:
            client.post("/admin-load/login", data={"email": username, "password": PASSWORD},
                        follow_redirects=True)
            for url in urls:
                body = client.get(url).data
                name = url.rsplit("/", 1)[-1]
                check(f"{name} has dark: variants", b"dark:bg-" in body and b"dark:text-" in body)
                check(f"{name} themes its card borders", b"dark:border-" in body)
    with app.test_client() as client:
        client.post("/admin-load/login", data={"email": "sm_teacher", "password": PASSWORD},
                    follow_redirects=True)
        body = client.get("/faculty/grades/grade_management").data
        check("the class record table has dark overrides",
              b".dark .sticky-col" in body and b".dark .grade-table" in body)
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
        scheduling_checks()
        dark_mode_checks()
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
