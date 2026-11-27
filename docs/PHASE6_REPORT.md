# Phase 6: PGPC-YY-NNNN enrollment control numbers

Branch `phase6-control-numbers`, based on Phase 5 (`d5de70e`).

## What the number is (decision taken without your answer to Q1–Q4)

| Question | What I built | Why |
|---|---|---|
| Replace the student number? | **No.** PGPC-YY-NNNN is the **enrollment control number** of a student's registration for a term. The P-series student number (`P202600363`) stays unchanged. | The P-number is the login, the "Student No." on every REGFORM, receipt and certification, and the key of the history. Replacing it would rewrite official records. The REGFORM already had a *Control No.* field reserved for this (Phase 3). |
| When is it issued? | At **registrar check-in** (Phase 5), inside the same transaction. | The number then means "officially enrolled". A form that is never checked in uses no number, and a failed check-in uses none either. |
| Once per …? | **Once per student per term.** Undoing and redoing a check-in keeps the same number. | Avoids gaps and double numbers for one registration. |
| YY | First year of the term's school year: 2026-2027 → **26**. | Matches the academic-year wording on the REGFORM. |
| NNNN | **One counter per YY, shared by all programs**, at least 4 digits. It starts at 0001 unless an admin sets another start (e.g. 1434 to continue a paper register). After 9999 it continues to 10000; it never wraps. | One register per school year is the simplest to audit. |

Each of these is a small change if PGPC decides otherwise.

The existing *Last Used Sequence Number* setting (the one with the placeholder "e.g., 1433") belongs to the **P-series student numbers**. It is not touched.

## What users see

- **Registrar**:
  - The check-in message reads *"… officially enrolled. Control No. PGPC-26-0001."*
  - The checklist panel and the check-in queue show the number.
  - The REGFORM page and the printed **Certificate of Registration** show *Control No. PGPC-26-0001*. Before check-in the page says *Not yet assigned*.
- **Student**: the REGFORM in *My Registration* shows the number.
- **New page *Control Numbers*** (sidebar; registrar and admin):
  - the counter per school year, with the count issued and the next number;
  - the register of issued numbers: who issued each one and when, whether the check-in is still in force or was undone, links to the review and REGFORM, and a filter by term or search by number, name or student no.
  - **Admins only** can:
    - **set the next number** for a school year, but never back to or below a number already issued;
    - **issue numbers to students checked in before this phase**, in check-in order (a button appears only when there are any).

## How duplicates are prevented

1. The year's counter row is locked with `SELECT … FOR UPDATE` while a number is taken. Concurrent check-ins wait their turn.
2. The database has three unique keys: on the **number**, on **(counter, position)** and on **(student, term)**.
3. Nothing is ever deleted or renumbered. Undo keeps the number.

**A real bug found and fixed while testing.** The first version created a missing counter with `INSERT IGNORE` inside the check-in transaction. With four registrars issuing at once, MariaDB **deadlocked**: each held a shared lock and waited for the other. A missing counter row is now created in its own short, committed statement, and only the existing row is locked. The concurrency test now issues 16 numbers from 4 threads and checks they are distinct and consecutive. It passes repeatedly, both for an existing counter and for the first number of a new school year.

## Database changes: migration `0007_control_numbers`

New tables only; nothing existing is altered. `registration_forms.control_number` already existed (Phase 3) and is filled in now.

| Table | Purpose |
|---|---|
| `control_number_sequences` | one counter per prefix + YY (`PGPC`/`26`), last number used |
| `control_numbers` | every number issued: student, term, REGFORM, who/when, source (`check_in` / `backfill`) |

Audit rows go to `enrollment_audit_log` (`control_number`, `control_sequence`).

**Deploying:**
```
mysqldump --single-transaction --routines <db> > backup_before_0007.sql
python scripts/migrate.py apply --backup-done
```
**Rollback:** `python scripts/migrate.py rollback 0007_control_numbers --backup-done`. It refuses once any number or counter exists, because issued numbers are official records.

Tested on the local copy of `pgpc_db`:
- backup → apply → rollback → re-apply all succeeded;
- with data, the guard refused the rollback;
- a fresh install from `full.sql` (in a scratch database) recorded 7 migrations, with 0 pending.

## Files

New:
- `website/checkin/control.py` (issue, counters, set next, backfill, register)
- `website/templates/checkin/control_numbers.html`
- `tests/test_phase6_control_numbers.py` (12 tests)
- `docs/PHASE6_REPORT.md`

Changed:
- `website/models.py`: `ControlNumberSequence`, `ControlNumber`, `CONTROL_TABLES`
- `website/schema_migrations.py`: 0007
- `full.sql`
- `website/checkin/service.py`:
  - check-in issues the number, and it is stored in the snapshot, the audit row and the result;
  - the checklist exposes the number.
- `website/checkin/routes.py`: control-number pages and actions; the check-in message shows the number
- `website/auth.py`: the JSON check-in message shows the number
- `templates/checkin/_checklist.html`, `queue.html`, `admin_base.html` (sidebar)
- `tests/test_route_crawl.py`: seeds an issued number

## Tests

`pytest`: **144 passed, 1 xfailed** (the xfail is the pre-existing one). Phase 6 tests cover:
- **Format**: YY from the school year; bad school years refused; 4-digit padding; no wrap after 9999.
- **Issued at check-in**: the next number; the counter moves by one; the number is on the REGFORM row, the snapshot, the audit log, the review page, the REGFORM page, the print and the student's own page.
- **Numbering**: numbers are consecutive across students; each school year has its own counter; asking again for the same student and term returns the same number.
- **No waste**: no number before check-in. A check-in that fails after the number was taken rolls the number back, and the next check-in gets that same number.
- **Undo/redo** keeps the number and does not move the counter.
- **Database keys** refuse a duplicate number, a duplicate counter position and a second number for the same student and term.
- **Concurrency**: 4 threads × 4 students, for an existing counter and for a new year.
- **Admin rules**: only an admin sets the next number; it cannot go back to an issued number; bad input is refused; the next check-in uses the new value; the change is audited.
- **Backfill**: admin only, in check-in order, marked `backfill`, and running it again does nothing.
- **Page access**: the registrar can view but not edit, the admin can edit, and the cashier is refused.

Browser run (Chrome):
1. Check-in shows **Control No. PGPC-26-0001** on the checklist panel.
2. The printed Certificate of Registration shows it next to *Student No. P202500002*.
3. The admin sets the next number to 1434, and the page shows *"next PGPC-26-1434"*.

The phone layout has no horizontal overflow. The smoke script still has only its one pre-existing failure.

## Known issues / not done
- Numbers are not printed on receipts or the Accounting statement. Tell me if they should be.
- Students checked in before Phase 6 need the admin's one-click backfill. Students enrolled before Phase 5 have no check-in, so they get no number.
- The "Enrolled" e-mail template is still missing from the uploaded code (unchanged since Phase 5).

## Questions for Phase 7 (grade visibility)
1. Which grades does a student see: the faculty class record (`grades`) or the registrar's `student_grades`? When does a faculty grade become official ("posted")?
2. Who controls visibility: per term, per program, or per class? Should a hold (for example an unpaid balance) hide grades from a student?
3. Should students see midterm grades, or only final grades?
