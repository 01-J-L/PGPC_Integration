# Phase 5: Approval checklist and registrar check-in

Branch `phase5-checkin`, based on Phase 4 (`1d1cf69`).

## The problem this fixes

The Phase 1 audit found that **anyone at the registrar's desk could make a student "Enrolled" with nothing checked**. There were three ways:
1. The review page's *Approve* button (its checklist was only a front-end hint).
2. The status dropdown on the application lists.
3. Bulk status update.

There was also no record of who enrolled a student, or of what had been checked at that moment.

## What changed for users

| Who | What they get |
|---|---|
| Registrar | The review page shows an **Approval Checklist** for the term. Each line shows whether it is met, who approved it and when. The registrar approves **"Enrollment documents reviewed"** and can revoke it (reason required). **Check in & Enroll** stays disabled, with the reason as a tooltip, until every required line is met. After check-in the panel shows *Checked in by …, date*, and **Undo check-in** (reason required). |
| Registrar | New page **Enrollment Check-in** (sidebar): every student enrolling in the active term with checklist progress (2 / 3), what is still needed, and the balance. Filters: Missing items / Ready / Checked in. A **Check in** button appears on rows that are ready. |
| Accounting | Under the default "Accounting clearance" policy, **Enrollment Assessments** now has **Give clearance** / **Withdraw clearance** per student. The registrar can no longer tick Accounting. |
| Admin | New page **Enrollment Checklist** (sidebar): rename, reorder, make optional, switch off, choose who approves, and limit to new or existing students. Admins can also **add lines**, e.g. "Guidance interview, approved by Guidance, new students only". |
| Everyone | The status dropdown and bulk update still offer "Enrolled", but they now run the same check-in. Bulk check-in enrolls the students who are ready and lists the others with the reason. |

## Default checklist (seeded by the migration)

| Line | How it is met | Approved by | Required |
|---|---|---|---|
| Registration form finalized | automatically, when this term's REGFORM is finalized (Phase 3) | — | yes |
| Enrollment documents reviewed | the registrar ticks it after checking the Documents tab | registrar, admin | yes |
| Accounting / enrollment payment policy | automatically from the Phase 4 policy. Under *Accounting clearance* (default), Accounting gives the clearance. | accounting, admin | yes |
| Dean approval | — | admin | **inactive** |
| President approval | — | admin | **inactive** |

The uploaded system showed only the Accounting tick box, and the Dean and President flags had been removed from the screen. These defaults match that: the registration form and the document review come from the existing process, and Dean/President can be switched on if still needed (my Q4). **No document list was invented.** The student forms mark no document as required, so document review is a single registrar line.

## Rules

**Check-in** (`checkin.service.check_in`, one transaction):
- Only the **registrar or an admin**, only for the **active term**.
- The student must be *Enrolling* (or still *Enrolled* from last term) and not already checked in.
- Every required line that applies to the student must be met; otherwise the error lists what is missing.

Check-in then runs the steps of the old Approve button:
1. **Section**: the section chosen on the review page, else the registration form's block section, else the current section, else auto-assignment (the old rule).
2. **Permanent student ID** for new students (unchanged P-series rule; PGPC-YY numbers are Phase 6).
3. **Fees**: the Phase 4 assessment is topped up (idempotent, never charged twice). The old automatic fee step runs only if an admin turns off the registration-form line.
4. Status → *Enrolled*, and the per-term record → *Enrolled*.
5. An `enrollment_checkins` row stores who, when, the role and a **JSON snapshot of the checklist** (with the assessment total and remaining balance at that moment).
6. The same "Enrolled" e-mail as before is sent. A mail failure never undoes a check-in.

If any step fails, nothing is saved (the P1-07 test still passes through the new path).

**After check-in**, until the check-in is undone, the following are refused:
- approving or revoking checklist lines;
- reopening the registration form;
- "Reject / send back".

Dropping a student still works.

**Undo check-in**: registrar or admin, reason required. The check-in row is **kept** (marked undone, with who, when and why). The student returns to *Enrolling*. The student ID, section and charges are kept. Checking in again creates a new row; the database allows only one active check-in per student and term.

**Checklist lines**:
- Only the roles configured on a line can approve or revoke it. This is enforced on the server, including on the old `toggle-approval` endpoint, which now goes through the same service.
- Revoking needs a reason and keeps who approved it.
- The Accounting line keeps the existing `student_enrollment_records.is_approved_by_accounting` flag in step, so the old screens and the Phase 4 policy still read it.

**Audit**: every approval, revocation, check-in, undo and checklist setting change is written to `enrollment_audit_log` with the actor, role, time and details.

## Database changes: migration `0006_enrollment_checkin`

New tables only. **No existing table is altered**, and nothing is deleted.

| Table | Purpose |
|---|---|
| `enrollment_checklist_requirements` | the configurable checklist lines (seeded with the 5 rows above) |
| `enrollment_checklist_items` | approval or revocation of a line, per student and term |
| `enrollment_checkins` | check-ins with snapshot; undo keeps the row (`active` → NULL; a unique key allows one active row) |
| `enrollment_audit_log` | audit trail |

**Deploying:**
```
mysqldump --single-transaction --routines <db> > backup_before_0006.sql
python scripts/migrate.py status
python scripts/migrate.py apply --backup-done
```
**Rollback:** `python scripts/migrate.py rollback 0006_enrollment_checkin --backup-done`. It refuses while check-ins or checklist approvals exist; then restore the previous code.

Tested on the local copy of `pgpc_db`:
- backup → apply (5 lines seeded) → rollback → re-apply all succeeded;
- on the test DB with data, the guard refused the rollback;
- a fresh install from `full.sql` into an empty database: all 6 migrations recorded, 0 pending, 5 checklist lines.

Students already *Enrolled* before this phase have no check-in row. Nothing is back-filled, and the screens say so.

> Note for deploying: `full.sql` starts with `CREATE DATABASE pgpc_db; USE pgpc_db`. Never run it against a server where `pgpc_db` is the live database. It is meant for new installs only, as before.

## Files

New:
- `website/checkin/__init__.py`
- `service.py` (checklist, approve/revoke, check-in, undo, queue, settings)
- `routes.py` (`/admin/enrollment-checkin`)
- `website/templates/checkin/_checklist.html`, `queue.html`, `requirements.html`
- `tests/test_phase5_checkin.py` (15 tests)
- `docs/PHASE5_REPORT.md`

Changed:
- `website/models.py`: 4 models plus `CHECKIN_TABLES`
- `website/schema_migrations.py`: 0006 and the checklist seed
- `full.sql`
- `website/__init__.py`: registers the blueprint
- `website/auth.py`:
  - the review page loads the checklist;
  - *Approve* = check-in;
  - *Reject* is refused after check-in;
  - `toggle-approval`, the status dropdown and bulk update all go through the checklist and check-in service.
- `templates/admin_enrollment_review.html`: checklist panel; server-side button state; old checkbox script removed; the tab bar scrolls on phones (it overflowed to 513 px at 390 px)
- `admin_base.html`: two sidebar links
- `website/registration/service.py`: reopen refused after check-in
- `website/finance/service.py`: under *Accounting clearance*, Accounting's clearance no longer also needs a generated assessment
- `website/finance/routes.py`, `templates/finance/index.html`: Give/Withdraw clearance, "Checked in" marker
- Tests:
  - `tests/conftest.py`: `minimal_checklist` fixture
  - `tests/test_phase1_regressions.py` and `tests/test_e2e_browser_csrf.py`: rewritten where the old behaviour was intentionally removed (the registrar ticking Accounting, Approve without a checklist). They still test the original bugs (CSRF, P1-05 display, P1-06 term record, P1-07 rollback).
  - `tests/test_route_crawl.py`: seeds approvals and check-ins.

## Tests

`pytest`: **132 passed, 1 xfailed** (the xfail is the pre-existing one). Phase 5 tests cover:
- Defaults seeded.
- Checklist state and who may act, per role.
- Approve and revoke: role rules, automatic lines can't be ticked, inactive lines refused, a reason is required, history and audit kept, and the Accounting flag kept in step.
- The payment line follows the policy when the policy is not clearance.
- Routes: a cashier and a student session are refused; Accounting's give/withdraw works.
- Check-in refused until complete, and the button is disabled.
- Happy path: P-ID assigned, block section applied, term record set, no double charge, snapshot, audit, page text.
- An invalid manual section is refused.
- Guards: wrong term, wrong role, double check-in, DB unique key, wrong status.
- Locks after check-in: revoke, REGFORM reopen, reject.
- Undo: reason required, history kept, ID kept, check-in again → two rows, one active.
- Status dropdown and bulk update: the not-ready student stays Enrolling and the ready one is enrolled.
- The old toggle endpoint.
- Admin configuration: add a line, new-students-only, optional lines don't block, validation, audit.
- Queue filters, including students still *Enrolled* from last term who have started a new form.

Browser run (Chrome, desktop 1440 px and phone 390 px):
1. Registrar Rita approves documents.
2. The queue shows 2 / 3, "Accounting… still needed".
3. Accounting (Ben) gives clearance.
4. The queue shows "Ready".
5. Rita clicks **Check in & Enroll** and confirms in the real SweetAlert dialog.
6. The student is *Enrolled*, the check-in row is recorded, and the panel shows "Checked in by Rita Reyes".

There is no horizontal overflow on the phone screens. The smoke script still has only its one pre-existing failure.

## Decisions made without your answer (change on request)
1. **Check-in is blocked** while a required line is unmet. There is no override. If you want a registrar override with a written reason (e.g. a promissory note), say so. It is a small addition.
2. **Dean and President approvals are off** (they were removed from the uploaded UI). Admins can switch them on.
3. Check-in is allowed only for the **active term**, so that `applicants.application_status` keeps meaning "enrolled now".
4. Undo is open to the registrar and admins (reason required); the ID, section and charges stay.
5. The registrar no longer clears Accounting; Accounting or an admin does.

## Known issues / not done
- The "Enrolled" e-mail template `email/enrollment_approved_email.html` is **missing from the uploaded code**, so the e-mail falls back to plain text. This was already the case before this phase.
- Students enrolled before this phase have no check-in record (not back-filled).
- Faculty class lists still miss irregular students sectioned in other blocks (Phase 3 note).

## Questions for Phase 6 (PGPC-YY-NNNN control numbers)
1. Should **PGPC-YY-NNNN replace** the P-series student ID (P202600363), or be a separate control number printed on the REGFORM next to it?
2. Is it assigned **at check-in** (my suggestion) or when the registration form is finalized? Is it once per student for life, or new each term?
3. Does "YY" mean the school year the student entered (2026-2027 → 26), or the calendar year?
4. Does one sequence run across all programs, and what number should 2026-2027 start from (1433 was mentioned)? Should existing students get numbers back-filled?
