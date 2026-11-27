# Phase 3 — Registration Form (REGFORM) for Regular and Irregular Students

**Branch:** `phase3-regform` (on top of `phase2-curriculum`)
**Status:** implemented and tested. **97 passed, 1 expected-fail** across the whole suite (26 new Phase 3 tests). The project's own `smoke_pages.py` shows 1 fixture-only failure, which was already failing before Phase 1.

## Decisions taken while Q6, Q7 and Q8 are open

I didn't change any existing behaviour for these. Each decision sits in one place in `website/registration/service.py` and is easy to change later.

| Open question | What Phase 3 does now |
|---|---|
| **Q6** Which grade record is official? | Academic history = **`student_grades`** (the registrar record, which the portal and the registrar grade pages also read). A subject is *passed* when remarks = Passed (or a grade ≤ 3.0 with blank remarks). A later failing record never undoes a pass. Faculty class-record grades that were never posted are **not** counted. |
| **Q7** Maximum units? | Limit = **the larger of** the term's `academic_terms.max_units` (existing setting, 26) **and** the student's own curriculum load for that term. A regular load is never refused: BSCS 2026 1st yr 1st sem is 28 units. Irregular loads can't exceed the limit, and there is no overload override. |
| **Q8** Re-take an already-passed subject? | **Never allowed.** There is no override. |

Prerequisites: a subject prerequisite that hasn't been passed **blocks** the subject (no bypass). Year standing and completed units are checked automatically. A requirement the system cannot verify needs an explicit registrar confirmation with a written basis, which is logged. That covers free-text notes such as "Approved by the dean", and a subject taken in a term whose grade isn't recorded yet.

## How it works for the registrar

Entry points (no other navigation added):
- **Enrollment review** page → a new **Registration Form** panel: *Prepare registration form* / *Open registration form*.
- Sidebar → Enrollment → **Registration Forms**: all forms of a term (filter by program and status), plus the enrolling/enrolled students who don't have a form yet.

**Creating a form** (one per student per academic term, enforced by a unique key, including two registrars clicking at once):
- The program comes from the application. The year level comes from the application, and the semester from the term.
- The curriculum is the student's assigned curriculum, otherwise the program default (Phase 2).
- **Classification is suggested automatically.** A student is *Irregular* when they have back subjects from earlier terms or failed, incomplete or dropped subjects; otherwise *Regular*. The registrar can switch it, and must give a reason.

**Regular:** the curriculum subjects of the year/semester are filled in, with code, description, units and the home section. The registrar only assigns sections: one home section for all, or per subject. Regular forms can't be customised. Switching back from Irregular to Regular rebuilds the curriculum list, and the before/after is logged.

**Irregular:** the system suggests this term's subjects the student is eligible for. The *Available subjects* table lists the **whole curriculum** with eligibility per subject (*Eligible*, *Needs confirmation*, *Not allowed* plus the reason), and filters for *can be added*, *back subjects* and *all*. The registrar can:
- add subjects from other year levels and semesters (flagged as back subjects or "from 2nd Semester / 3rd Year");
- remove subjects;
- retake failed or dropped subjects;
- pick a section per subject. Only sections that offer the subject this term are listed, falling back to the program's sections for that year level when nothing is scheduled yet.

**Checked on every change, and again at finalize** (backend; the database locks the form and section rows):
- already passed;
- already on the form, or one form per term;
- prerequisites (subject / standing / units / note);
- unit limit;
- the section is active and offers the subject;
- **section capacity** (counted per subject across all forms of the term; row lock prevents two registrars taking the last seat);
- **schedule conflicts** from the faculty timetable (`teaching_loads`, same school year, filtered to the term's semester);
- at finalize, every subject has a section, and a regular form matches the curriculum term.

**Finalize** freezes the form and refreshes the schedule snapshot. It also sets the student's home section if they had none, and that change is logged. **Reopen** needs a reason and logs the subjects as they were.

**Printable Certificate of Registration** (`Print`) follows the college's REG. FORM layout. It includes:
- the college header;
- student no., name, A.Y., course, year & section, level, curriculum;
- a subject table with code, description, units, section and schedule;
- the total;
- the prerequisite note;
- signature areas: Student, Accounting (Assessed by), College Dean (Verified by), College Registrar (Enrolled by).

Drafts carry a **DRAFT** watermark. The control number line is ready for Phase 6. The fee box points to the Statement of Account until Phase 4 adds the assessment.

**Students** can open their own **finalized** form at `/my-registration`. Drafts and other students' forms return 404.

**History:** each term keeps its own form, and lines are **snapshots** (code, description, units, schedule). So a later curriculum version or edit never changes a past REGFORM; this is tested. Every action is in `registration_form_changes` with who, when, and before/after: create, add, remove, section, home section, classification, rebuild, finalize, reopen.

## Database — migration `0004_registration_forms` (new tables only)

| Table | Key points |
|---|---|
| `registration_forms` | student, applicant, term, **curriculum version**, program, year, semester, classification + reason, status draft/finalized/cancelled, home section, unit limit, total, control number (Phase 6), created/finalized by and at. `UNIQUE(student_user_id, academic_term_id)` |
| `registration_form_subjects` | the curriculum subject plus a snapshot of code, description, units, curriculum term, section, schedule text, source (curriculum/added/back_subject) and remarks. `UNIQUE(form, code)` |
| `registration_form_changes` | the change log (no FK, so it outlives anything) |

All foreign keys are `ON DELETE RESTRICT`; nothing cascades. `full.sql` was updated, and old DB + migrations equals fresh `full.sql` (tested). Rollback refuses while forms exist.

## Files
**New:** `website/registration/{__init__,service,routes}.py`, `website/templates/registration/{index,start,form,print}.html`, `tests/test_phase3_regform.py`, `docs/PHASE3_REPORT.md`.
**Modified:** `website/models.py` (3 models), `website/schema_migrations.py` (0004), `website/__init__.py` (blueprints), `website/auth.py` (REGFORM status on the enrollment review), `website/templates/admin_enrollment_review.html` (panel), `website/templates/admin_base.html` (sidebar item), `full.sql`, `tests/test_route_crawl.py`.

## Tests (26 new)
- **Regular:** curriculum load and home section, not customisable, finalize needs sections and sets the home section, schedule snapshot text, print layout and total, regular load above the configured limit is allowed.
- **Irregular:**
  - failed subject → Irregular, and the dependent subject is blocked with the reason; already-passed subject is blocked; back subject is retaken and flagged off-semester;
  - incomplete prerequisite;
  - free-text requirement needs confirmation, and the basis is stored and logged; such subjects aren't auto-suggested;
  - retake of an ungraded subject needs confirmation;
  - unit limit, schedule conflict (with the day named), full section, a section that doesn't offer the subject;
  - reclassify to Regular rebuilds and logs.
- **History:** finalized forms are frozen and reopen needs a reason; one form per term, with separate forms per term; **four concurrent creates → one form**; a new curriculum version doesn't alter an existing form.
- **Access:** students see only their own finalized form; cashier, accounting, guidance and secretary are refused; enrollment-review links work.
- **All-roles crawl** includes the new pages (no 500s, CSRF tokens present).
- **Headless Chrome (CSRF on):** import BSCS 2026 → activate → set default → regular form for a 1st-year (28 units, schedules from the timetable) → finalize → print. Also an irregular 2nd-year who failed CC 102 (back subjects listed, dependent subjects blocked). No horizontal overflow at phone width.

## Integration notes for the next phases
- **Phase 4** reads `registration_forms` (finalized) to generate the assessment. The form's lines and units are the basis for any unit-based fees, once you confirm the fee rules.
- **Phase 5** checklist items "Registration form completed / subjects verified / sections assigned / schedules verified" can come straight from the form's status and its finalize validation.
- **Phase 6** fills `registration_forms.control_number`.
- **Gap to decide (Phase 7/9):** faculty grading class lists come from `applicants.section_id` (home section) plus existing grade rows. An irregular student's subjects in *other* sections won't appear on those teachers' class lists until grading reads the REGFORM. I didn't change the grading module in this phase.
- The faculty scheduling screens use `system_settings.school_year` and `current_semester`, while admission uses `academic_terms`. Keep them pointing at the same term, because schedules are matched by school year.
