# Phase 8: Section deletion protection

Branch `phase8-section-protection`, based on Phase 7 (`a74c7af`).

## What was wrong

| Path | Before |
|---|---|
| Registrar, *Manage Sections* → Delete | Checked only for assigned students. A section with schedules was deleted and left its **class schedule orphaned**. With class records, registration forms or registration lines, the database refused, and the user saw a raw database error. |
| Dean, *Section Schedule hub* → Delete | Checked schedules and students, then **deleted the section's faculty class records (`faculty_assignments`)** to get past the foreign key. |
| Dean, *Manage Programs* → Delete | Checked only courses. The ORM cascade then tried to delete every section, which crashed if a section was in use. |
| Superadmin, *force delete program* | **Deleted grades (`grades`, `student_grades`)** and every section. |
| Registrar, *Generate Sections* / *Edit* | Could switch any section back on. |

The database already had `ON DELETE RESTRICT` on the four foreign keys to `sections` (applicants, faculty assignments, registration forms, registration lines). Class schedules (`teaching_loads`) refer to a section only by its label ("BSCS 1A") and school year, so no foreign key protects them.

## Decisions made without your answer

| Question | What I built |
|---|---|
| Block or archive a section in use? | **Both.** Delete is refused, with the exact reasons. **Archive** is offered instead: the section disappears from new enrollment and section pickers, and every record pointing to it stays. |
| Who may archive or restore? | The same people who could delete: the **registrar and admin** on *Manage Sections*, and the **dean, admin and superadmin** on the hub. Archiving needs a reason. |
| Is an unused section still deletable? | **Yes.** A section nothing refers to can still be deleted, as before. |
| Superadmin force delete | It now **refuses whenever academic or enrollment history exists** (the same checks as the normal program delete). It still removes a program that has only configuration. This is the one tool that lost power; history is never deleted. |

## How it works

`website/sections/protection.py` is used by every path:

- **`usage(section)`** counts students assigned, faculty class records, registration forms, registration-form subject lines and class schedules. Schedules are matched by label and school year, including admin-style names like "bscs 1A". A schedule from another school year doesn't block.
- **Delete** happens only when the usage is empty. Otherwise it is refused with *"Section "BSCS 1A" cannot be deleted: it has 1 student(s) assigned, 1 registration form(s), 10 registration-form subject line(s), 5 class schedule(s). Archive it instead; that keeps the history."* A deletion is audited with a snapshot of the section.
- **Archive** (reason required) sets `is_active = 0` and records who, when, why and the previous state. Everything that already refused inactive sections now refuses archived ones:
  - automatic section assignment;
  - check-in with a chosen section (Phase 5);
  - assigning a section on a registration form (Phase 3).
  Edit can't re-activate an archived section ("Restore it before making it active"), and *Generate Sections* skips archived ones.
- **Restore** puts back the previous `is_active`.
- **Program deletes**:
  - The dean's program delete refuses while any section is in use.
  - The registrar's program delete now also counts registration forms and lines; before, these were a database error.
  - All program deletes remove archive rows of the (unused) sections they delete.
- Every delete, archive and restore is written to `enrollment_audit_log` with the actor, role, time and details.

## Database changes: migration `0009_section_archives`

One new table; `sections` is not altered.

| Table | Purpose |
|---|---|
| `section_archives` | one row per section: archived or not, previous `is_active`, reason, who/when archived, who/when restored |

**Deploying:**
```
mysqldump --single-transaction --routines <db> > backup_before_0009.sql
python scripts/migrate.py apply --backup-done
```
**Rollback:** `python scripts/migrate.py rollback 0009_section_archives --backup-done`. It refuses once any section has been archived.

Tested on the local copy of `pgpc_db`:
- backup → apply → rollback → re-apply all succeeded;
- with data, the guard refused the rollback;
- a fresh install from `full.sql` recorded 9 migrations, with 0 pending.

## Files

New:
- `website/sections/__init__.py`, `protection.py`
- `tests/test_phase8_sections.py` (13 tests)
- `docs/PHASE8_REPORT.md`

Changed:
- `website/models.py`: `SectionArchive`, `SECTION_TABLES`
- `website/schema_migrations.py`: 0009
- `full.sql`
- `website/auth.py`:
  - *Manage Sections*: delete, archive and restore go through the service;
  - Edit and Generate respect archives;
  - the list shows the archive state;
  - the program blockers count registration forms and lines;
  - the program delete clears archive rows.
- `website/templates/admin_manage_sections.html`: *Archived* badge with the reason as a tooltip, Archive/Restore buttons, clearer delete confirmation
- `website/faculty/main/routes.py`:
  - the hub's delete, archive and restore go through the service;
  - the silent deletion of class records is removed;
  - the dean's program delete checks sections;
  - the superadmin force delete refuses when history exists.
- `website/templates/faculty/manage_section_schedule_hub.html`: *Archived* badge, Archive/Restore and Delete buttons with accessible labels
- `tests/test_route_crawl.py`: seeds an archived section

## Tests

`pytest`: **168 passed, 1 xfailed** (the xfail is the pre-existing one). Phase 8 tests cover:
- **Usage**: every kind of reference is detected; a schedule of another school year doesn't block; admin-style names match their schedule.
- **Registrar**: deletes only unused sections (audited); a refused delete keeps the section and its class record.
- **Archive and restore**: a reason is required; students keep their section; no double archive; Edit can't re-activate; Generate updates the section but leaves it archived; restore puts back the previous state; audited.
- **An archived section takes no new students**: auto-assignment skips it, check-in refuses it, and the registration form refuses it.
- **Roles**: cashier and teacher refused.
- **Dean**: delete no longer destroys class records; archive and restore work from the hub; the hub shows *Archived* and Restore.
- **Program deletes**: the dean's delete is blocked by a section in use and allowed (with its archive row) once unused; the registrar's delete counts registration forms; the superadmin force delete keeps grades but still deletes a configuration-only program.

Browser run (Chrome):
1. Delete on BSCS 1A (1 student, 1 registration form, 10 lines, 5 schedules) is refused with that list.
2. Archive on 1B (reason "Merged into 1A for this term"): the section shows *Archived*, `is_active = 0`.
3. The dean's hub shows *BSCS 1B · Archived · Restore*.

No horizontal overflow on the phone layout. The smoke script still has only its one pre-existing failure.

## Known issues / not done
- Student downloads targeted at a section use a free-text label (`student_downloads.target_section`). They don't block deletion and are not renamed.
- In my offline test browser the hub's Archive/Delete icon buttons are blank, because the faculty icon font is not served locally. The original Delete icon is blank the same way, so this is not caused by this change.
- The superadmin can no longer wipe a program's grades from the UI. If an erroneous program really must be removed, it should be done with a database backup and an explicit script, not a button.

## Questions for Phase 9 (integration and QA)
1. Which flows should the end-to-end acceptance run cover first? My proposal: new student → admission → registration form → assessment → payment and verification → checklist → check-in with control number → grades released.
2. Is there a staging server or a copy of the production database (anonymised if needed) to rehearse migrations 0001–0009 on real data volumes?
3. Should I also prepare an operator runbook (deployment order, backups, rollback per migration, first-day settings such as the payment policy, checklist and control-number start)?
