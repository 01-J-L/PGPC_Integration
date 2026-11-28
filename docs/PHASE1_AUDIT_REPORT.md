# PGPC Integrated Enrollment Management System — Phase 1 Audit Report

**Scope:** Phase 1 of the master development prompt — complete system audit and critical bug fixing.
**Codebase:** `21 checking/PGPC_Integration` (git `0538529` + the uploaded uncommitted working-copy changes).
**Branch with the fixes:** `phase1-audit-bugfix` (2 commits on top of `0538529`).
**Date:** 2026-11-27

> Status: Phase 1 is complete. 15 bugs are fixed and covered by tests (40 passed, 1 expected-fail that documents a Phase 2 item).
> Phase 2 has **not** been started. It needs the decisions listed in §9.

---

## 1. How the audit was done

| Activity | Method |
|---|---|
| Static review | Read the models, the app factory and the enrollment, registrar, cashier, grading and section code paths. Ran AST and regex scans for duplicate definitions, `url_for` targets, `render_template` targets, routes with no authorization check, and GET routes that change data. |
| Runtime | Ran the app on MariaDB 10.11 with a **separate throw-away database** (`pgpc_test`) built from the original `full.sql`. Production and the `.env` database were never used, and test runs never send e-mail. |
| Route crawl | Requested every GET page as admin, registrar, cashier, accounting, student, dean, superadmin and anonymous (`tests/test_route_crawl.py`). |
| Browser | Ran headless Chrome against the real pages with CSRF switched on: student login, registrar checklist (fetch JSON), document upload (multipart fetch), cashier payment form and dean login (`tests/test_e2e_browser_csrf.py`). |
| Existing tests | Ran `scripts/smoke_pages.py` on both the original code and the fixed code against the same database. The results matched: 2 failures in both, and both come from fixture data (they need a `bscs` program and `courses` rows). They are not regressions. |
| Reference documents | Parsed all 7 plans of study (.docx) and 12 registration forms (.xlsx) and cross-checked them (§8). |

**Limitation:** `full.sql` contains the schema only, with no student, payment or grade rows. I could not audit real data, so I added `scripts/audit_data.py`. It runs 18 read-only checks (duplicate enrollments, balance drift, duplicate payments, orphan schedules and others). **Please run it on a copy of production and send me the output.** Several Phase 4 decisions depend on it.

---

## 2. Project architecture

```
main.py / wsgi.py ──> website.create_app()
   ├─ extensions: SQLAlchemy, Flask-Login (faculty), Flask-Mail, CSRFProtect (new)
   ├─ before_request: maintenance-mode gate
   ├─ db.create_all() on start-up (+ new: pending-migration warning)
   ├─ Blueprint "views"        /            13 routes  public site, student status page, enrollment form
   ├─ Blueprint "auth"         /           168 routes  admission, registrar, cashier, accounting, student portal
   ├─ Blueprint "faculty_auth" /admin-load   8 routes  faculty/dean login (Flask-Login + FlaskForm)
   └─ Blueprint "faculty_main" /faculty     83 routes  scheduling, sections, grading, dean admin
```

| Fact | Consequence |
|---|---|
| `auth.py` is 10,800 lines and uses raw `mysql-connector` SQL (`get_db_connection()`, 145 call sites). Sessions use `session['user_role']` and `session['student_id']`. | Each admission, registrar or cashier route checks its role by hand. There is no decorator, so a missing check is easy to miss. |
| The faculty side uses the SQLAlchemy ORM, `@login_required` and FlaskForm. | Two data-access styles share one database. Any schema change must work for both. |
| `models.py` is the single schema definition, and `full.sql` is generated from it. | `create_all()` never adds columns to existing tables. Flask-Migrate is listed in `requirements.txt` but was never initialized. |
| Status values are free-text strings (`'Enrolling'`, `'Eligible for Enrollment'`, …) repeated across files. | There is no central list. Phase 5 should introduce constants. |
| Student identity is split across two tables: `student_users` (login) and `applicants` (one row per application, holding the program, year, section and documents). | Finance and the faculty `grades` table key on `applicants.applicant_id`. The portal `student_grades` and `student_enrollment_records` key on `student_users.id`. |

---

## 3. Database relationship analysis

```
student_users 1─* applicants *─1 sections *─1 programs 1─* subjects (self-ref prerequisites x2)
      │              │  │                          1─* courses          (faculty scheduling catalog)
      │              │  └─* assessments 1─* payments                    (live finance ledger)
      │              │  └─* grades (faculty class record) *─1 subjects, users
      │              └─ status, year level, semester, documents (BLOBs)
      ├─* student_grades (registrar/portal grades) *─1 subjects
      └─* student_enrollment_records *─1 academic_terms   (one per term, UNIQUE)
teaching_loads.section = "<PROGRAM_CODE> <SECTION>"  (string, no FK)   users 1─* teaching_loads
faculty_assignments *─1 users, subjects, sections
```

**Structural findings (to resolve in later phases; not changed in Phase 1):**

| # | Finding | Impact | Phase |
|---|---|---|---|
| R1 | **Two grade stores that are never synchronized.** Faculty enter grades in `grades` (the class record). The student portal (`/my-grades`) and the registrar grade pages read `student_grades`. No code copies `grades` into `student_grades`. | Grades faculty enter never reach the student portal. | 7 (needs decision Q6) |
| R2 | **`subjects.subject_code` is globally UNIQUE.** GE105 cannot exist in both BSCS and BSMA, and two curriculum versions cannot share a code. The existing curriculum importer rolls back the whole file when a shared GE code appears. | The four programs cannot all be loaded. Recorded as an expected-fail test. | 2 |
| R3 | **Three subject catalogs:** `subjects` (grading/registrar), `courses` (faculty scheduling) and `program_courses` (website). They are linked only by matching code strings. | Phase 2 must unify them behind curriculum versions without breaking scheduling. | 2 |
| R4 | `teaching_loads.section` is a label string with no FK to `sections`. `sections` has no `academic_term_id` (only `school_year` and `semester` strings). | No database-level deletion protection for schedules. Labels drift when a section is renamed. | 8 |
| R5 | ORM `Program.sections` uses `cascade="all, delete-orphan"`. The faculty "delete program" path depends on it. | ORM deletes cascade into sections. This is unsafe for history. | 8 |
| R6 | `assessments.balance` and `amount_paid` are stored totals that every payment and undo updates by hand. `payments` has no reversal concept, and *undo* deletes the row. | Balances can drift from the payment rows (audit check D03/D04). Financial history is destroyed. | 4 |
| R7 | Approval flags appear twice: `applicants.is_approved_by_*` (legacy) and `student_enrollment_records.is_approved_by_*` (per term). Only the latter is used now. | Confusing. Phase 5 replaces both with a checklist table. | 5 |
| R8 | `student_assessments` and `student_payments` exist as tables and models but nothing reads or writes them. | Dead tables. Keep them (non-destructive) and do not build on them. | 4 |
| R9 | Money columns use `float()` in Python (`update_payment`, list pages) even though the DB columns are DECIMAL. | Rounding risk. Phase 4 moves this to `Decimal`. | 4 |

---

## 4. Current enrollment workflow (as implemented)

```
New applicant: Pending → In Review → Approved (permit no.) → Scheduled (exam) → Passed | Failed | Not Taken
                                   └→ Rejected
Passed ── student opens /enrollment-form, submits documents + inventory ──> Enrolling
          • section auto-assigned (first section with space)
          • temporary A-number replaced by permanent P<YYYY><5 digits> (e.g. P202600363)
          • batch fees matching program/year/semester/residency appended to assessments
Enrolling ── registrar: enrollment review page ──> Approve → Enrolled   (or Reject → Enrolling, Drop → Dropped)
          • checklist UI now shows only "Accounting" (President/Dean removed in the uploaded working copy)
Registrar "opens" a new term: every Enrolled → Eligible for Enrollment (original_enrollment_status='Enrolled')
Eligible ── student submits re-enrollment (auto year/semester progression, shifting only in 1st sem) ──> Enrolling
```

**Gaps relevant to Phases 3–6:**
- **Approve has no backend gate.** The checklist is advisory, and a registrar can approve with nothing ticked. Two other paths also set `Enrolled` and bypass the review page entirely: the status dropdown (`admin_update_application_status`) and bulk update (`admin_bulk_update_status`). Phase 5 must route all three through one check-in service.
- **No per-term subject list (no REGFORM record).** The review page shows the whole curriculum. Nothing stores which subjects a student takes in a term (Phase 3).
- **Fees come from flat batch assessments** (`batch_assessments_log`) matched by program, year, semester and residency. They are not unit-based (Phase 4).
- **The student number is the P-series.** The requested `PGPC-YY-NNNN` format conflicts with it (Q1).
- **No grade-visibility control.** `/my-grades` always shows `student_grades` (Phase 7).
- **The registrar "Delete section" button** only checks for assigned students, not schedules. The faculty version deletes `faculty_assignments` rows silently (Phase 8).

---

## 5. Bugs fixed in Phase 1

Each bug has a regression test in `tests/test_phase1_regressions.py`. The ID in the test name matches the ID here. All tests failed on the uploaded code (verified) and pass now.

| ID | Severity | Bug | Root cause | Files | Tables | Fix |
|---|---|---|---|---|---|---|
| P1-01 | **Critical** | No CSRF protection on any admission, registrar, cashier or accounting form or fetch call. A third-party page could make a logged-in cashier post a payment, or an admin delete an application or program. | `CSRFProtect` was never initialized. Only faculty FlaskForms had tokens. | `website/__init__.py`, new `templates/_csrf.html`, 8 base layouts | – | App-wide `CSRFProtect`. A head include adds the token to every POST form (including dynamically added forms and `form.submit()`) and to every same-origin fetch/XHR (header). A JSON-aware error handler. Cookie flags `HttpOnly` and `SameSite=Lax`, plus `SESSION_COOKIE_SECURE` from `.env`. Tokens last for the whole session. |
| P1-02 | **Critical** | Admission "Delete program" (admin, registrar, secretary) **deleted every `student_grades` and `grades` row** of the program's subjects. | Hand-written cascade in `admin_delete_program`. | `auth.py` | student_grades, grades | Refuses when grades, students, faculty assignments or scheduled classes exist, and lists the reasons. Empty programs can still be deleted. The hero image is removed only after a successful delete (it used to be deleted even when the delete was refused). |
| P1-03 | High | Registrar upload and view of Form 138 and SHS Diploma (enrollment copies) returned a server error. | Code used `enrollment_shs_card_*` and `enrollment_shs_diploma_*` columns that did not exist. | `schema_migrations.py`, `full.sql`, `models.py` (comment) | applicants | Migration **0001** adds the 6 nullable columns. |
| P1-04 | High | Chart of accounts: adding or deleting an account committed, then crashed (`BuildError`). | `url_for('chart_of_accounts')` was missing the blueprint prefix. | `auth.py` | chart_of_accounts | `auth.chart_of_accounts`. |
| P1-05 | High | **Uploaded working-copy change:** the review page ticked "Accounting" on screen for students who had paid ₱0 (the threshold defaulted to 0). The value was never saved, so the screen disagreed with the database. | `total_paid >= 0` is always true. Display-only override. | `auth.py`, `admin_enrollment_review.html` | – | The checkbox shows only the stored approval. The computed total paid and required minimum appear as a hint, plus "requirement met — tick to record" when a threshold is configured. Decimal maths. |
| P1-06 | High | Approve, reject and drop left **no per-term enrollment record** unless a checklist box had been ticked first. | Plain `UPDATE student_enrollment_records`. | `auth.py` | student_enrollment_records | Upsert with year level and semester. The action and term id are validated (unknown actions used to raise `NameError`). |
| P1-07 | High | A failure while adding fees or generating the student ID was swallowed, and the rest of the enrollment was committed. Result: a student Enrolled or Enrolling with no fees or no permanent ID. | `except: return 0` in `_auto_assign_fees`, `_ensure_enrolled_balance` and `_assign_permanent_student_id`. | `auth.py` | applicants, assessments | The helpers re-raise, and the callers roll back the whole transaction. An explicit rollback was added in `admin_update_application_status`. |
| P1-08 | High | The first-deployment admin bootstrap failed (`Column 'email' cannot be null`). Staff login ignored the legacy `password_hash_alt` that the users merge preserved. | `INSERT … email NULL`. `check_password_hash` only on `password_hash`. | `auth.py` | users | Placeholder `admin@pgpc.local`. A null-safe `_staff_password_ok()` matches `User.check_password()`. |
| P1-09 | Medium | `/enrollment-form/<id>` was registered twice. The `auth` copy was unreachable and allowed a different status set. | Duplicate view. | `auth.py`, `pending_application.html` | – | Dead copy removed. Links point to `views.enrollment_form_page`. |
| P1-11 | Low | Dead duplicate definitions: `save_file_to_uploads` ×3, `send_otp_email` ×2, `admin_print_grade_card` ×2, and `auth = Blueprint(...)` twice. | Copy and paste. | `auth.py` | – | Earlier copies removed. The definition Python actually used is kept, so behavior is unchanged. |
| P1-12 | **High** | Every document and photo upload over **64 KB** failed (`Data too long`). Uploads allow 5 MB. | Columns were `BLOB` in `full.sql` and the models. | `schema_migrations.py`, `full.sql`, `models.py` | applicants | Migration **0002** widens the 14 file columns to `MEDIUMBLOB` in **one** `ALTER`. Data verified byte-identical. Rollback refuses if it would truncate. |
| P1-13 | High | **Printing a receipt crashed after every payment** until someone had saved a receipt layout (the default redirect after a payment). | Templates read `layout.dragDate.top_pos` from an empty table. | `auth.py` | receipt_layout_settings | `_receipt_layout()` merges saved positions with defaults. An unknown payment id redirects. |
| P1-14 | Medium | Dean "Add Subject" (grading programs) gave HTTP 500. A duplicate code would also have caused a 500. | The template expects `form`, and the view passed none. | `faculty/main/routes.py`, `forms.py` | subjects | `GradingSubjectForm` (with CSRF) and a duplicate-code check. |
| P1-15 | Medium | The grade view and printout crashed for students without a section (irregular or unsectioned). | `student.section.program_id` on `None`. | `faculty/main/routes.py`, `print_student_grades.html` | – | Falls back to the student's `program_choice`. |

(P1-10 is the "every `url_for` target exists" guard test. It found P1-04.)

---

## 6. Open issues (not fixed in Phase 1, assigned to a phase)

| ID | Sev. | Issue | Where | Phase |
|---|---|---|---|---|
| O-01 | High | Enrollment approval has no backend checklist gate, and three separate code paths can set `Enrolled`. | `admin_enrollment_action`, `admin_update_application_status`, `admin_bulk_update_status` | 5 |
| O-02 | High | "Undo last payment" **hard-deletes** the payment row: no reversal record, no reason, no time limit, no row lock. | `undo_last_payment` | 4 |
| O-03 | High | Duplicate payment submissions (double-click or resubmit) are not prevented. `FOR UPDATE` only stops overpayment. | `update_payment` | 4 |
| O-04 | High | Faculty grades (`grades`) never reach `student_grades` (portal and registrar). See R1. | grading | 7 |
| O-05 | High | `subjects.subject_code` is globally unique, which blocks shared GE codes and curriculum versions (expected-fail test). | schema | 2 |
| O-06 | Medium | Registrar "Delete section" checks students but not schedules or teaching loads. The faculty delete silently removes `faculty_assignments`. | `admin_manage_sections`, `delete_section` | 8 |
| O-07 | Medium | Money handled as `float` in Python. Financial status is derived from stored `balance` sums. | `update_payment`, `_get_all_applications_and_stats` | 4 |
| O-08 | Medium | No audit log for registrar, accounting or grade actions (who, what, before/after). | – | 4/5/7 |
| O-09 | Medium | The student-login captcha is client-side only, so a bot can post straight to `/student-login` and skip it. There is no login rate limiting. | `student_login.html` | 9 (security) |
| O-10 | Medium | `/logout` and a few faculty actions (`accept_substitution_request`, `reset_time_slots`) change state via GET. | various | 9 |
| O-11 | Low | Section capacity uses `COUNT(*)` with no lock, so simultaneous enrollments can overfill a section. | `_assign_section_automatically` | 3 |
| O-12 | Low | The effective `save_file_to_uploads` accepts `.mp4` and `.mov` for every caller (status files, etc.). | `auth.py` | 9 |
| O-13 | Low | The faculty-side "delete program" (no courses) cascades into sections through the ORM. It raises a 500 if students hold those sections. | `delete_program` | 8 |
| O-14 | **High (ops)** | The `.env` inside the uploaded zip contains live secrets: `MAIL_PASSWORD` (Gmail), `DB_PASSWORD`, `FLASK_SECRET_KEY` and `ADMIN_INITIAL_PASSWORD`. It is git-ignored, but it was shipped in the zip. **Rotate all four.** A new `FLASK_SECRET_KEY` logs everyone out once. | `.env` | now |

---

## 7. New and changed files

**Modified:** `website/__init__.py`, `website/auth.py`, `website/models.py`, `website/faculty/main/routes.py`, `website/faculty/main/forms.py`, `full.sql`, and templates `base.html`, `admin_base.html`, `accounting_base.html`, `cashier_base.html`, `faculty/base.html`, `faculty/auth_base.html`, `maintenance.html`, `receipt_layout_tool.html`, `admin_enrollment_review.html`, `pending_application.html`, `grading/admin/print_student_grades.html`.

**New:** `website/templates/_csrf.html`, `website/schema_migrations.py`, `scripts/migrate.py`, `scripts/audit_data.py`, `tests/conftest.py`, `tests/test_phase1_regressions.py`, `tests/test_route_crawl.py`, `tests/test_e2e_browser_csrf.py`, `tests/fixtures/baseline_full_pre_phase1.sql`, `pytest.ini`, `requirements-dev.txt`, `docs/PHASE1_AUDIT_REPORT.md`.

**Test results (MariaDB 10.11, Python 3.13):** `40 passed, 1 xfailed`. They cover the 15 bug regressions, migration idempotency, *old schema + migrations == fresh full.sql* (column by column), the all-roles crawl (no 500s, every POST form carries a CSRF token) and 5 headless-Chrome flows with CSRF on.

---

## 8. Reference documents (input for Phase 2)

**Inventory:** 7 plans of study (BSCS ×2, BSCRIM ×3, BPA, BSMA; 54–61 subjects each, Years 1–4) and 12 registration forms (2nd semester only; Years 1–3 for each program; Resident and Non-Resident fee sheets). `Certication of Grades -BPA.docx` contains real student grades. It is **not** a curriculum source and was not used.

**Curriculum versions are cohort-based.** Each reg form matches exactly one plan:

| Program | Version (plan file) | Cohort it matches |
|---|---|---|
| BSCS | `PLAN-OF-STUDY-BSCS - 2026 FINAL` | 1st year (AY 2026-2027, 9/9 subjects) |
| BSCS | `PLAN-OF-STUDY-BSCS FINAL` | 2nd and 3rd year (8/8, 7/7) |
| BSCRIM | `NEW FINAL` / `2ND YEAR FINAL` / `3RD YEAR FINAL` | 1st / 2nd / 3rd year cohorts |
| BPA, BSMA | one plan each | all cohorts |

**Conflicts for the registrar to resolve before import (no automatic choice will be made):**
1. **BPA 2nd yr 2nd sem:** the reg form lists **AE 125**, and the plan lists **TAX 301** in the same slot.
2. **BSCS 2026 plan:** **DC 103** *Database Administration and Development* has **no units**.
3. **BSCS plans:** THESIS 1 requires **DC 101** (2026 plan) and **DC 1013** (older plan). Neither subject exists in those plans.
4. **BSCRIM plans:** OJTCRIM 2 requires **OJTCRIM 1**, and COMPRE 2 requires **COMPRE 1**. Neither is listed. The 3rd-year plan's CLJ6 requires **CLJ5**, which is not in that plan.
5. **Non-subject prerequisites** need their own rule type: "4th Year Standing", "Regular 4th Year", "Completed 110 Academic units", "All 1st & 2nd year subjects".
6. **Description spelling differences** between the reg forms and the plans: *Arts* vs *Art Appreciation* (GE106), *INTODUCTION* (CLJ1), *DISAMING* (DT2), *BALISTICS* (FRC6), *Crisis* vs *Crises* (CRIM6), *Civic* vs *Civil* Welfare (BSMA NSTP 2). Which spelling is official?
7. **Code formatting:** plans write `GE 105`, and reg forms write `GE105`. Proposal: store the official form and match without spaces.

**Fees on the reg forms** (e.g. BSCS 1st yr Resident: Tuition 7,800 + 14 other fees = **₱11,700**; separate Non-Resident sheet) look like the existing *batch assessment* scopes (`garciano` / `non_garciano`). Phase 4 will read fees from the existing accounting setup and **will not** copy amounts from the spreadsheets unless you confirm them (Q5).

---

## 9. Decisions needed before Phase 2+ (business rules not documented)

| # | Question | Why it matters |
|---|---|---|
| Q1 | **Is `PGPC-YY-NNNN` a new *registration/enrollment control number*, or does it replace the existing student number (`P2026xxxxx`)?** The certification uses the P-number as *Student No.* and a separate `PGPC-REG…` document number. | Phase 6 design. Replacing student numbers would affect logins, receipts and history. |
| Q2 | Should every program share one PGPC-YY sequence, or should each have its own? Starting number for AY 2026-2027 (1433?). | Phase 6 |
| Q3 | Should one subject code (e.g. GE105) be **one shared subject** across programs, or a separate subject per program and curriculum version? | Phase 2 schema (O-05) |
| Q4 | Approval checklist: are President and Dean sign-offs removed for good (the uploaded UI removed them), or still required for some cases? | Phase 5 |
| Q5 | Enrollment payment policy: full payment, minimum down payment (how much?) or accounting clearance? Is the resident/non-resident split by municipality ("Padre Garcia" in the address) correct? | Phase 4/5 |
| Q6 | Which grade record is official: the faculty class record (`grades`), or the registrar's (`student_grades`)? When do faculty grades get "posted"? | Phase 7 (O-04) |
| Q7 | Maximum units per term: the existing `academic_terms.max_units`/`programs.max_units` (26), or a different overload rule? Are curriculum terms above that limit (BSCS 1st sem = 28 units) allowed? | Phase 3 |
| Q8 | May an authorized person re-enroll a student in an already-passed subject (an override), and who? | Phase 3 |

---

## 10. Migration plan

| # | Migration | Type | Status |
|---|---|---|---|
| 0001 | Add `enrollment_shs_card_*`, `enrollment_shs_diploma_*` (6 nullable columns) | additive | ready |
| 0002 | `applicants` file columns `BLOB` → `MEDIUMBLOB` | widening, table rebuild | ready |
| Phase 2 (planned) | `curriculum_versions`, `curriculum_subjects` (program/version/year/sem/prereq), and a relaxed subject-code uniqueness per the Q3 answer. Back-fill from existing `subjects`. | additive + index change | design |
| Phase 3 | `student_term_subjects` (registration form lines per term, with section and schedule) + a change log | additive | design |
| Phase 4 | Payment reversals and a verification table, idempotency key, Decimal service | additive | design |
| Phase 5 | `enrollment_checklist_items` + `enrollment_audit_log`, check-in fields on `student_enrollment_records` | additive | design |
| Phase 6 | `control_number_sequences` (row-lock, UNIQUE) | additive | design (Q1) |
| Phase 7 | `grade_visibility_settings` + audit | additive | design |
| Phase 8 | FK `RESTRICT` on section relations, `is_archived` on sections | constraint | design |

All migrations are idempotent and recorded in `schema_migrations`. The app never applies them on its own; it only prints `[WARN] … pending` at start-up. A database built from the new `full.sql` is already up to date. **Flask-Migrate:** I recommend stamping an Alembic baseline once production is on migration 0002, and then moving later phases to Alembic. Until then, `scripts/migrate.py` is the tool.

---

## 11. Backup, deployment and rollback

**Before deploying:**
```bash
# 1. Full backup (schema + data + routines); keep it off the server too
mysqldump -u <user> -p --single-transaction --routines --triggers --hex-blob <DB_NAME> > pgpc_backup_$(date +%Y%m%d_%H%M).sql
# 2. Make sure blobs fit through the client/server
mysql -u <user> -p -e "SHOW VARIABLES LIKE 'max_allowed_packet'"   # >= 16M recommended (uploads up to 5 MB)
```

**Deploy:**
```bash
git checkout phase1-audit-bugfix          # or apply the patch
pip install -r requirements.txt           # no new runtime packages
python scripts/migrate.py status
python scripts/migrate.py apply --backup-done     # 0002 rebuilds `applicants`: do it outside office hours
python scripts/audit_data.py > audit_$(date +%Y%m%d).txt   # read-only; send me the output
# restart the app (gunicorn / run_simple)
```
Optional `.env` additions: `SESSION_COOKIE_SECURE=True` once the site runs on HTTPS.

**Smoke-check after the restart:** log in as a registrar, open an enrollment review and tick Accounting. Upload a scanned PDF (>64 KB). As a cashier, record a ₱1 payment and print the receipt, then undo it. Log in as a student. Log in as a dean.

**Rollback:**
- Code: `git checkout 0538529` (or the previous release) and restart. The new columns are harmless to the old code.
- Schema (only if needed): `python scripts/migrate.py rollback 0002_document_columns_mediumblob --backup-done`. This **refuses** if any file is now larger than 64 KB; in that case restore from the backup. Then `rollback 0001_… --backup-done`.
- Full restore: `mysql -u <user> -p <DB_NAME> < pgpc_backup_<stamp>.sql`.

**Running the tests** (never against production; the suite refuses unless `DB_NAME` ends in `_test`):
```bash
pip install -r requirements-dev.txt
DB_NAME=pgpc_test python -m pytest        # Windows: set DB_NAME=pgpc_test && python -m pytest
```

---

## 12. Proposed plan for the next phases (after §9 answers)

1. **Phase 2 – Curriculum:** versioned curricula per program and cohort, a registrar import with preview and duplicate/conflict review (§8 list pre-loaded as "needs review"), prerequisite rules including standing-based ones. Existing `subjects`/`courses` stay readable for scheduling and grading.
2. **Phase 3 – REGFORM:** per-term registration lines. Regular students get curriculum subjects automatically. Irregular students get a subject picker validated against passed/failed/dropped history, prerequisites, unit cap, section capacity (with locking) and schedule conflicts. Printable form generated from the stored lines. Change log.
3. **Phase 4 – Finance:** an assessment generated from the finalized REGFORM using the existing fee setup, a Decimal ledger, payment reversal instead of delete, an idempotency key, accounting verification, and the configurable policy (Q5).
4. **Phase 5 – Checklist and check-in:** 13 items with status, remarks, by and when, a dashboard with filters, and a single transactional check-in service that all three "Enrolled" paths use.
5. **Phases 6–9** as specified, plus the open issues in §6.

I will not start Phase 2 until the §9 answers are in, as the master prompt's execution strategy requires.
