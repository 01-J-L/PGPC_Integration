# PGPC Integration: deployment and operations runbook (phases 1–9)

This runbook is for whoever deploys and runs the system: IT staff or the developer on duty. It covers:
- moving a server from the uploaded code to the phase 9 code;
- rolling back;
- the settings each office must check on the first day.

## 1. What changes in the database

Nine migrations run in this order. Every migration only adds something; no existing data is deleted.

| Migration | What it does | Notes |
|---|---|---|
| `0001_enrollment_shs_document_columns` | Adds 6 nullable columns to `applicants` for the enrollment copies of Form 138 and the diploma. | |
| `0002_document_columns_mediumblob` | Widens the 14 document/photo columns of `applicants` from BLOB (64 KB) to MEDIUMBLOB (16 MB). | **Rebuilds `applicants`.** Run it in a quiet period. On 157 MB of documents it took 2.1 s in the rehearsal. |
| `0003_curriculum_management` | Curriculum versions, subjects, prerequisites, student assignment, imports, audit. | |
| `0004_registration_forms` | Registration forms (REGFORM), their subject lines, change log. | |
| `0005_enrollment_finance` | Term assessments, payment verification, reversals, double-submit guard, finance audit. | `assessments` and `payments` are not altered. |
| `0006_enrollment_checkin` | Approval checklist (seeded with 5 lines), approvals, check-ins, enrollment audit log. | |
| `0007_control_numbers` | PGPC-YY-NNNN counters and the register of issued numbers. | |
| `0008_grade_visibility` | Grade releases and holds. Every term that already has grades is recorded as **released**. | Students keep seeing what they see today. |
| `0009_section_archives` | Section archive state. | |

The application never migrates by itself. At start-up it only prints `[WARN] … pending migration(s)`.

## 2. Before the deployment day

1. **Rehearse on a copy** of the production database. You need free disk space about the size of the database. The source is only read.
   ```
   python scripts/rehearse_migrations.py --source <production_db> --rollback
   ```
   The result must be `RESULT: PASSED`.
   - The script copies the database into `<production_db>_rehearsal`.
   - It applies every migration with a timing for each.
   - It checks that no existing table lost a row or changed its ids.
   - It runs the data audit.
   - With `--rollback`, it rolls everything back and applies it again.
   - It then drops the copy (keep it with `--keep`).
2. Check the **fee rules** (the batch assessment screen: *Assessments* in the Accounting menu, *Batch Assessment* in the Cashier menu) for every program, year level and semester that will enroll. Term assessments are built only from these rules. A form with no matching rule gets a ₱0 assessment and a warning.
3. Prepare the **curriculum** for each program (Admin → Curriculum). Import the plan of study (.docx) or the registration forms (.xlsx), check them, activate the version and make it the default. A registration form cannot be created without an active curriculum.
4. Decide the first-day settings in section 5 with the offices.

## 3. Deployment (maintenance window)

1. Announce the window and stop the application (all workers).
2. **Back up** the database and keep the file off the server:
   ```
   mysqldump --single-transaction --routines --events <db> > pgpc_before_phase9_YYYYMMDD.sql
   ```
   Check that the file is not empty, and that the last line reads `-- Dump completed`.
3. Deploy the code. Keep the existing `.env`; it holds the live secrets and is not part of the package.
4. Install the dependencies: `pip install -r requirements.txt`. **`python-docx` was missing from the uploaded `requirements.txt`**, so Word curriculum import would fail on a fresh server.
5. Migrate:
   ```
   python scripts/migrate.py status
   python scripts/migrate.py apply --backup-done
   python scripts/migrate.py status            # every line must say [applied]
   ```
   If a migration stops halfway (power cut, lost connection), run `apply` again: each step checks whether it is already done.
6. Check:
   ```
   python scripts/audit_data.py      # read-only; no "high" findings expected
   python scripts/smoke_pages.py     # logs in as throwaway accounts, renders every page, removes the accounts
   ```
   `smoke_pages.py` writes briefly (its accounts, and a throwaway program when the database has none). Run it inside the window.
7. Start the application. The start-up log must not contain `pending migration`.
8. Log in once with each role: registrar, cashier, accounting, admin, dean, a student. Open each person's home page.

## 4. Rolling back

**Preferred:** stop the application, restore the backup from step 3.2, and deploy the previous code. This is the only complete rollback once people have entered new data.

**Per migration**, newest first, only while nothing has been recorded in the new tables:
```
python scripts/migrate.py rollback 0009_section_archives --backup-done
... down to 0001
```
Each rollback refuses (`Refusing: these tables contain data …`) when it would delete records:
- 0001: uploaded enrollment documents exist;
- 0002: a document larger than 64 KB exists (narrowing would truncate it);
- 0003–0009: curricula, forms, assessments, check-ins, numbers, holds or archives exist.

Do not use `--force` unless you have exported those tables and know why.

> `full.sql` starts with `CREATE DATABASE pgpc_db; USE pgpc_db`. It is for **new installations only**. Never run it on a server where `pgpc_db` holds live data.

## 5. First-day settings

| Who | Where | Setting | Default |
|---|---|---|---|
| Accounting | Accounting → Enrollment Assessments → *Enrollment payment policy* | What the registrar needs from Accounting before check-in: Accounting clearance / full payment / any payment / minimum down payment. Also whether only *verified* payments count. | Accounting clearance, verified payments only |
| Admin | Admin → Enrollment Checklist | The checklist lines, who approves each one, required or optional, new or existing students. | Registration form finalized, documents reviewed, Accounting (required). Dean and President exist but are off. |
| Admin | Admin → Control Numbers → *Set the next number* | The first PGPC-YY-NNNN number of the school year (e.g. 1434 to continue a paper register). | 0001 |
| Admin | Admin → Control Numbers → *Issue their numbers* | Numbers for students checked in before this version (appears only when there are any). | — |
| Registrar | Admin → Grade Release | Release each term's grades when they are checked. New terms start **not released**. | Existing terms released |
| Admin | Admin → Grade Release → *Unpaid balance* | Withhold grades while the term's assessment has a balance. | Off |
| Registrar / Admin | Manage Sections / Section hub | Archive sections that are no longer used; only unused sections can be deleted. | — |

## 6. Who can do what

| Function | Registrar | Admin | Cashier | Accounting | Dean | Student |
|---|---|---|---|---|---|---|
| Curriculum versions, import, activate | ✓ | ✓ | | | | |
| Registration form: prepare, finalize, reopen | ✓ | ✓ | | | | view own (finalized) |
| Record payment; reverse own same-day unverified payment | | | ✓ | | | |
| Verify payments (not their own); reverse any (with reason); re-apply fees | | | | ✓ | | |
| Payment policy | | ✓ | | ✓ | | |
| Approve "documents reviewed" | ✓ | ✓ | | | | |
| Give / withdraw accounting clearance | | ✓ | | ✓ | | |
| Check in / undo check-in (with reason) | ✓ | ✓ | | | | |
| Checklist settings, control-number counter, backfill, unpaid-balance rule | | ✓ | | | | |
| Grade release / withhold, student holds | ✓ | ✓ | | | | sees released grades |
| Archive / restore / delete sections | ✓ | ✓ | | | ✓ (hub) | |

The checks are on the server, and they also cover direct POSTs to every route. The phase 9 sweep (`tests/test_phase9_permissions.py`) calls all 39 POST routes and 14 pages of the new modules as an anonymous visitor, a student and an unrelated staff role.

## 7. Where to look when something is questioned

| Question | Table |
|---|---|
| Who approved, checked in, undid a check-in, issued a control number, released/withheld grades, placed/lifted a hold, archived/deleted a section? | `enrollment_audit_log` |
| Who generated an assessment, recorded/verified/reversed a payment, or changed the payment policy? | `finance_audit_log` |
| What did the checklist look like at check-in? | `enrollment_checkins.checklist_snapshot` (JSON) |
| Changes to a registration form | `registration_form_changes` |
| Curriculum changes | `curriculum_audit_log` |

## 8. Routine checks

- **Weekly**: `python scripts/audit_data.py` (read-only). Investigate any *high* finding.
- **Before each enrollment period**:
  - fee rules for the new term;
  - an active default curriculum per program;
  - the payment policy;
  - the checklist;
  - the control-number counter of the new school year.
- **After grade encoding**: release the term on *Grade Release*.
