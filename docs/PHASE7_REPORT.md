# Phase 7: Grade visibility

Branch `phase7-grade-visibility`, based on Phase 6 (`a2a0ca2`).

## The situation before

- Students see grades in three places: **My Grades**, the **printable class card**, and the current-term grades on the **dashboard**. The dashboard computes those grades but its template never displays them.
- All three read the registrar's record `student_grades`, and a grade appeared to the student **the moment it was saved or imported**. There was no way to finish checking a term first, or to hold one student's grades.
- The faculty class record (`grades`: prelim, midterm, final) is a separate table with no term column. **No student page reads it**, and nothing copies it into `student_grades`.

## Decisions made without your answer (Q6 and the Phase 6 questions)

| Question | What I built |
|---|---|
| Which grades do students see? | Still **only the registrar's `student_grades`**, exactly as before. The faculty class record stays staff-only. "Posting" faculty grades into the registrar record is a separate feature: it needs a term on `grades` and a rule for converting grades. Tell me if you want it. |
| Who controls visibility, and at what level? | The **registrar or an admin**, **per term**, with optional **per-program exceptions** ("release 2025-2026 2nd Semester, but not yet for BSCRIM"). |
| Holds? | **Per-student holds** by the registrar or an admin, for one term or all terms, with an internal reason. The student only sees *"on hold, please see the Registrar's Office"*. Lifting needs a reason, and the hold row is kept. |
| Unpaid balance? | **Optional, off by default.** An admin can switch on "withhold grades while the term's assessment (Phase 4) still has a balance". |
| Midterm vs final? | Not applicable: `student_grades` has one grade per subject and term. |

## On deployment nothing changes for students

Migration 0008 records **every term that already has grades as released**. Students therefore keep seeing exactly what they see today. From then on, **a new term's grades stay hidden until the registrar releases them**. This is the one intended behaviour change.

## What users see

- **Students**:
  - A term that is not released, or is withheld, shows *"Grades for this term are not yet released."*
  - A hold shows *"… on hold. Please see the Registrar's Office."*
  - The unpaid rule shows *"… withheld until your account balance is settled with Accounting."*
  - Released terms look as before. The class card and the dashboard data follow the same rules.
- **Registrar / admin: new page *Grade Release*** (sidebar):
  - every term with its grade count, release state (who and when), and program exceptions;
  - **Release** or **Withhold** (reason required) for all programs or one program;
  - student holds: place one by student no., control no. or e-mail, and lift one (reason required);
  - the unpaid-balance switch, which only an admin can change.
- Staff screens (review page, grade entry, staff print of the class card) always show every grade.

## Rules

A term is visible to a student when:
1. it is **released**. A program-specific row decides for that program; otherwise the all-programs row decides; with no row, it is not released.
2. there is **no active hold** on that term or on all terms for that student.
3. if the admin switched it on: the student has **no remaining balance** on that term's Phase 4 assessment. Terms with no assessment are never withheld by this rule.

Every release, withholding, hold, lift and switch change is written to `enrollment_audit_log` with the actor, role, time and details.

## Database changes: migration `0008_grade_visibility`

New tables only; `student_grades` is not altered.

| Table | Purpose |
|---|---|
| `grade_releases` | one row per term and program key (`''` = all programs): released or withheld, by whom, when, reason |
| `grade_holds` | per-student holds (one term or all); lifting fills `lifted_*` and keeps the row |

The migration seeds one released row per term found in `student_grades`. The switch is stored in `system_settings.grade_hold_unpaid_balance`.

**Deploying:**
```
mysqldump --single-transaction --routines <db> > backup_before_0008.sql
python scripts/migrate.py apply --backup-done
```
**Rollback:** `python scripts/migrate.py rollback 0008_grade_visibility --backup-done`. It refuses once a hold or a registrar release/withholding exists; the migration's own seed rows don't block it.

Tested on the local copy of `pgpc_db` (which holds no grades):
- backup → apply → rollback → re-apply all succeeded;
- with data, the guard refused the rollback;
- a fresh install from `full.sql` recorded 8 migrations, with 0 pending.

The seed itself is tested separately: it marks existing terms released, and running it twice is harmless.

## Files

New:
- `website/grades/__init__.py`, `visibility.py` (rules and actions), `routes.py` (`/admin/grade-release`)
- `website/templates/grades/release.html`
- `tests/test_phase7_grade_visibility.py` (11 tests)
- `docs/PHASE7_REPORT.md`

Changed:
- `website/models.py`: `GradeRelease`, `GradeHold`, `GRADE_TABLES`
- `website/schema_migrations.py`: 0008 and its seed
- `full.sql`
- `website/__init__.py`: registers the blueprint
- `website/auth.py`:
  - `/my-grades` uses the visibility service;
  - the student's class card is filtered (the staff print is unchanged).
- `website/views.py`: the dashboard's current-term grades are filtered
- `website/templates/student_grades.html`: per-term notice for hidden terms. It also fixes a crash: the page compared an empty grade with `> 3.0`, so a row without a numeric grade (e.g. *Incomplete*) broke the page.
- `admin_base.html`: sidebar link
- `tests/test_route_crawl.py`: seeds a release row and a hold

## Tests

`pytest`: **155 passed, 1 xfailed** (the xfail is the pre-existing one). Phase 7 tests cover:
- **Unreleased terms**: hidden on My Grades, the class card and the dashboard data, while staff still see everything.
- **Release and withhold**: withholding requires a reason, the internal reason is never shown, both actions are audited, and the dashboard data follows.
- **Terms are independent**.
- **Program exceptions**: both directions; an unknown program is refused.
- **Holds**: one term or all terms; reason, role and term validation; lifting keeps history; a hold can't be lifted twice.
- **Hold page and lookup**: holds placed and lifted through the page; student lookup by e-mail, applicant id and student no.
- **Unpaid rule**: off by default and admin-only. On: the term is withheld while a balance remains, shown again once paid, and never applied to a term without an assessment.
- **Page permissions**: registrar and admin allowed; cashier, accounting, guidance and student refused.
- **Migration seed**: keeps existing terms visible and is idempotent.
- **Empty grade**: a row with no numeric grade renders.

Browser run (Chrome, a real student login):
1. 4 grades exist and the term is not released: the student sees only the notice.
2. The registrar clicks **Release**: the student sees the 4 grades.
3. The registrar places a hold ("Library book not returned"): the student sees *on hold* and not the reason.

No horizontal overflow at 390 px. The smoke script still has only its one pre-existing failure.

## Known issues / not done
- **Bridge from the faculty class record to the registrar record** (see above): not built, pending your rule.
- **Security observation (pre-existing):** the student login captcha is checked only in the browser; the server never verifies it. It slows down people, not scripts. A server-side check, or login rate limiting, is a small follow-up.
- The dashboard computes current-term grades but its template never shows them (unchanged; the data is now filtered anyway).

## Questions for Phase 8 (section deletion protection)
1. When a section still has students, schedules, faculty loads, grades or registration-form lines, should **Delete** be blocked outright, or should the section be **archived** (hidden from new enrollment but kept for history)?
2. Who may archive or restore a section: the registrar, the dean or the admin?
3. Should an empty, never-used section still be truly deletable?
