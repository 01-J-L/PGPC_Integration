# Phase 9: Integration and QA

Branch `phase9-integration-qa`, based on Phase 8 (`8a1ae7a`). This is the last phase of the plan. The operator guide is in **`docs/RUNBOOK.md`**.

## Decisions made without your answer

1. **Acceptance flow:** the full path, from a new student account to the student seeing released grades, run through the real HTTP routes (below).
2. **No staging server or production dump was provided.** I wrote a rehearsal script for you to run on a copy of the real database. Here I rehearsed on the **original uploaded schema filled with realistic volume**.
3. **Runbook:** written (`docs/RUNBOOK.md`).

## Bugs and gaps found in this phase (all fixed, each with a test that fails without the fix)

| # | Finding | Impact | Fix |
|---|---|---|---|
| Q9-1 | **The enrollment form could be submitted at any status.** The page (GET) checked for *Passed / Enrolling / Eligible*, but the submission (POST) did not. A *Pending, Approved, Rejected, Failed, Enrolled or Dropped* applicant could post the form directly. | The student jumped to *Enrolling*, a new student got a permanent P-number, and fees were added. An already checked-in student could be pushed back to *Enrolling*. | The POST applies the same status check, and refuses students already checked in for the active term. 6 parametrised regression tests plus the acceptance flow: **all 7 fail without the fix**. |
| Q9-2 | **Rollback of 0001 had no guard.** Rolling back would drop the enrollment Form 138 / diploma columns, uploaded files included. | Silent loss of documents during a rollback. | A rollback guard that refuses while any of those files exists. |
| Q9-3 | **Rollback of 0002 relied on MariaDB strict mode.** Without strict mode, narrowing MEDIUMBLOB back to BLOB **truncates** files over 64 KB silently. | Corrupted documents after a rollback. | A rollback guard that refuses while any document is larger than 64 KB. |
| Q9-4 | The two control-number POST routes (*set next*, *backfill*) had no route-level login check. The service still refused, because only an admin may use them, so no data could change. | An anonymous visitor reached the service instead of the login page. | Route guard added. Found by the new permission sweep. |
| Q9-5 | **`python-docx` was missing from `requirements.txt`.** | On a fresh server, importing a Word plan of study crashed with an ImportError. | Added (`python-docx==1.2.0`), plus a clear message if it is still missing. |
| Q9-6 | The smoke script failed on any database without a program called `bscs`, and on databases without a course that matches a subject. | The one smoke failure reported since Phase 1. It made the smoke check useless as a gate. | It now uses the database's own data, or creates throwaway rows and removes them. **It passes completely** on the test DB and on the `pgpc_db` copy, with nothing left behind. |
| — | Unused imports in the new modules (lint). | None. | Removed; `pyflakes` is clean on every new module. |

## Acceptance run (`tests/test_phase9_acceptance.py`)

One test walks the whole flow through the real routes, as each office would. Every step is asserted:

1. A new student account logs in and **submits the application** with the three required documents → *Pending*. The enrollment form is refused at this status (Q9-1).
2. The registrar sets **Approved**, then **Passed**. Setting *Enrolled* from the status menu is refused (Phase 5).
3. The student opens and **submits the enrollment form** → *Enrolling*, permanent P-number assigned. The legacy fee step charges the matching fee rules.
4. The registrar **creates and finalizes the registration form** (block subjects of the active curriculum).
5. The **assessment** is ₱4,200.75, built from the fee rules. The two charges from step 3 are adopted, not charged twice.
6. Accounting sets the policy to **full payment, verified payments only**. The cashier records two payments through the real form (submission token, O.R. numbers). Check-in is refused while the payments are unverified. A second accountant **verifies** both.
7. The registrar approves **documents reviewed** and **checks the student in** → *Enrolled*, **PGPC-26-NNNN**. The P-number is unchanged. The snapshot shows a remaining balance of 0 and the control number. Still exactly two charges.
8. The student's **REGFORM** shows the control number and the student number.
9. The registrar **enters a grade** (1.75). The student sees *"not yet released"*. The registrar **releases** the term, and the student sees the grade.
10. The **audit trail** contains the approvals, the control number and the check-in, plus two recorded and two verified payments.

## Permission sweep (`tests/test_phase9_permissions.py`)

- All **39 POST routes** of the new modules (curriculum, registration, finance, check-in/checklist/control numbers, grade release) are called as an **anonymous visitor**, a **student** and an unrelated staff role (**OSA**). Every call must be refused: a redirect to login, or 400/401/403/404. None may cause a server error.
- `CHECKSUM TABLE` over 23 tables (`applicants`, `payments`, `student_grades`, every new table, …) must be identical afterwards.
- All **14 staff GET pages** must refuse anonymous visitors and students.

## Migration rehearsal (`scripts/rehearse_migrations.py`)

The script copies a database into `<db>_rehearsal`, foreign keys included. It then:
- applies every pending migration, with a timing for each;
- checks that every pre-existing table kept its rows and ids, and that nothing is pending;
- runs the data audit;
- with `--rollback`, rolls back in reverse order (respecting the guards) and applies everything again;
- drops the copy. **The source is only read.**

Here it ran on the **original uploaded schema** filled with synthetic volume:
- 2,000 students, each with 70 KB of documents;
- 30,000 grades, 6,000 charges, 12,000 payments;
- 48 sections, 240 schedules, 48 class records;
- 52,607 rows and 157 MB in total.

```
applied 0001_enrollment_shs_document_columns    0.06s
applied 0002_document_columns_mediumblob        2.10s   (rebuild of applicants, 157 MB)
applied 0003_curriculum_management              0.30s
applied 0004 … 0009                            ≤0.10s each
Data audit: exit 0, 0 high-severity findings
Rolled back 0009 … 0001, re-applied 0001 … 0009
RESULT: PASSED                                   (whole run 12 s)
```

After the run, `grade_releases` holds the one existing grade term as **released** (`migration 0008`), so the 30,000 existing grades stay visible to students.

**Before production, run it on a copy of the real database** (runbook §2).

## Other checks

- **Fresh install** from `full.sql` (into a scratch database): 9 migrations recorded, 0 pending.
- **Browser walkthroughs** of phases 5–8 re-run against the final code (Chrome, desktop and 390 px phone). All still pass:
  - check-in → P-number and PGPC-26-0001 on the printed certificate;
  - grades hidden → released → on hold;
  - section delete refused → archived → shown on the dean's hub.
  No horizontal overflow.
- **Dependencies:** every third-party import is in `requirements.txt`.

## Totals for the nine phases

`pytest`: **181 passed, 1 xfailed** (the xfail is the pre-existing one documented in Phase 1).

| Phase | Tests added |
|---|---|
| 1 Audit and bug fixes | regression tests per bug (P1-01 … P1-12) |
| 2 Curriculum | curriculum versions, import, activation |
| 3 Registration form | regular/irregular forms, prerequisites, sections |
| 4 Assessment and payments | 20 |
| 5 Checklist and check-in | 15 |
| 6 Control numbers | 12 |
| 7 Grade visibility | 11 |
| 8 Section protection | 13 |
| 9 Integration and QA | 13 (acceptance 8, permission sweep 5) |

Plus the route crawl (every page for every role, with real data from all phases), the CSRF browser tests and the smoke script.

## Files (this phase)

New:
- `tests/test_phase9_acceptance.py`
- `tests/test_phase9_permissions.py`
- `scripts/rehearse_migrations.py`
- `docs/RUNBOOK.md`
- `docs/PHASE9_REPORT.md`

Changed:
- `website/auth.py`: status gate on `submit-enrollment`
- `website/schema_migrations.py`: rollback guards for 0001 and 0002
- `website/checkin/routes.py`: route guards; unused import
- `requirements.txt`: `python-docx`
- `website/curriculum/importer.py`: clear message without python-docx; unused imports
- `website/finance/routes.py`, `website/finance/service.py`: unused imports
- `scripts/smoke_pages.py`: independent of existing data; cleans up after itself

## Open items (not done; your decision)

1. **Faculty class record → registrar grades.** There is no bridge between the two grade tables (Phase 7). It needs a term on faculty grades and a conversion rule.
2. **Student login captcha** is checked only in the browser (pre-existing). A server-side check or login rate limiting is a small follow-up.
3. **Missing e-mail templates** (pre-existing): `enrollment_approved`, `application_not_enrolled`, `exam_scheduled`, `exam_passed` and `exam_failed`. The system sends a plain-text fallback.
4. **Registrar override** of an unmet checklist line (e.g. a promissory note): not built (Phase 5 default: blocked).
5. **Control number on receipts**: not printed (Phase 6).
6. **Irregular students in other blocks** are missing from faculty class lists (Phase 3 note).
7. **Rehearse on the real data** with `scripts/rehearse_migrations.py` before the production deployment.
