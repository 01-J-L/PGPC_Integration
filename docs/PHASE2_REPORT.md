# Phase 2 — Dynamic Curriculum Management

**Branch:** `phase2-curriculum` (on top of `phase1-audit-bugfix`)
**Status:** implemented and tested. **Tests: 71 passed, 1 expected-fail** (the expected-fail documents that the *legacy* `subjects` table still cannot hold a shared code; see "Decision taken" below).

## Decision taken without your answer to Q3

You asked me to continue without answering the Phase 1 questions. I chose the option that changes nothing that already exists:

- Curricula live in **new, version-scoped tables**. GE105 can appear in every program and every version.
- The legacy `subjects` table is **not modified**. Its global UNIQUE code and the grading screens that use it work exactly as before.
- Each curriculum subject is **linked** to the legacy subject with the same code in the same program, when one exists. Linking happens on activation, or with the "Link existing subjects" button. Grade history can then be matched in Phase 3. Nothing creates or renames legacy subjects.

If you later decide GE105 should be one shared subject (Q3), the curriculum tables don't change. Only the legacy table and the link step would.

## What the registrar can do (Sidebar → Management → **Curriculum**)

| Requirement (master prompt §6) | Where |
|---|---|
| Organize by program, version, academic year, year level, semester (1st, 2nd, Summer) | Curriculum list (filter by program). A version page shows subjects grouped Year → Semester with unit totals. |
| Create, edit, activate and deactivate versions | **New curriculum** (draft) → **Edit details** / **Activate** / **Archive** / **Set as default for new students** / **Clone as new version** / **Delete draft** |
| Add, edit and remove subjects; set code, description, units, year, semester | The version page: an inline **Edit** per row and an **Add subject** form |
| Configure prerequisites | "Add prerequisite" per subject. Types: *subject passed* (only earlier-term subjects are offered), *year standing*, *units completed*, *other (note)* |
| Import the PLAN OF STUDY / REG. FORM files, review before saving, detect duplicates | **Import reference file** → review screen (described below) |
| View curriculum by program | Curriculum list and version page |
| Assign students to their cohort's curriculum | **Assign students** (active versions). Filter by year level and status; the reason is recorded and history is kept. |

### Import review screen
- Reads **.docx plans of study** (all terms, prerequisites) and **.xlsx registration forms** (one term). It was tested on all 19 uploaded files.
- Every row is editable: term, code, description, units, include or exclude.
- Rows are flagged **New**, **Already in curriculum** or **Conflict**. A conflict means the same code with a different description, units, term or spelling. Conflicts are **never applied automatically**: the registrar picks *Keep existing* or *Use file values*.
- **Errors block the import** until they are fixed or the row is excluded: missing or invalid units, a duplicate code in the file, a prerequisite that is not scheduled earlier. Warnings are kept as notes: prerequisites the system cannot check, or subjects missing from the file.
- When a reg form is checked against a curriculum, the subjects that are **in the curriculum but missing from the file** for that term are listed (example: BPA TAX 301 vs AE 125).
- Uploading the same file twice is flagged (SHA-256). The upload is staged in `curriculum_import_batches` and nothing is written until **Import**. **Discard** leaves no trace beyond the batch record.
- A "Show only rows that need attention" filter for long plans.

### Validation rules (enforced in `website/curriculum/service.py`, plus DB constraints)
- A code appears **once per version**. "GE 105" and "GE105" count as the same code. Enforced in the service and by `UNIQUE(curriculum_version_id, code_key)`.
- Year level 1…the version's year count. Semester 1, 2 or 3 (Summer). Units 0–30 with at most one decimal (`CHECK` constraints as well).
- A subject prerequisite must be in the **same version** and in an **earlier term**, which also rules out cycles. Moving a subject that would break a chain is refused, and so is removing a subject that others require.
- **Locking:** once any student is assigned to a version, or the version is archived, its subjects, units and prerequisites cannot change. You clone it instead. This is how a new curriculum never overwrites the one earlier students follow.
- On an **active** version, a subject whose linked legacy subject has grade records cannot be removed.
- **Activation** is refused when the version is empty. With gaps (a term with no subjects, 0-unit subjects, prerequisites kept as text) it needs a ticked confirmation **and a reason**, both recorded.
- One "default for new students" version per program.
- Access is limited to admission `admin` and `registrar` (server-side), and every POST is CSRF-protected.
- **Audit log:** every create, update, delete, activate, archive, clone, import and assignment, with who, role, when and before/after (shown on the version page).

## Results with your reference documents

| File | Subjects | Notes |
|---|---|---|
| BSCS 2026 plan | 54 | **DC 103 has no units**: the import is blocked until units are entered or the row is excluded. THESIS 1 prereq **DC 101** does not exist (kept as a note). PRAC → *4th year standing*. |
| BSCS plan (older) | 54 | THESIS 1 prereq **DC 1013** does not exist (note). PRAC → *110 units completed*. |
| BSCRIM NEW / 2nd yr / 3rd yr plans | 59 / 59 / 58 | **COMPRE 2 lists itself as its prerequisite** in the NEW plan (note). OJTCRIM 1 and COMPRE 1 are missing. CLJ5 is missing in the 3rd-yr plan. "All 1st & 2nd year subjects" is kept as a note. |
| BPA plan | 55 | PA318's prerequisite cell "STAT 101 PA 310 …" is split into separate subjects correctly. |
| BSMA plan | 61 | Clean. |
| 12 reg forms | 5–10 each | Cross-checked against the matching version. BPA 2nd yr shows **AE 125** new and **TAX 301** missing. BSCRIM 1st yr shows spelling conflicts (INTODUCTION, DISAMING). |

No units or prerequisites were invented. Every gap is either blocked or kept as a visible note for the registrar.

**Suggested set-up** (the registrar does this through the import screen):
- BSCS: "2026" (A.Y. 2026-2027, default for new students) and the older plan for the 2nd- and 3rd-year cohorts.
- BSCRIM: three versions, one per cohort.
- BPA and BSMA: one version each.

## Database changes — migration `0003_curriculum_management` (new tables only)

| Table | Purpose |
|---|---|
| `curriculum_versions` | program (FK RESTRICT), name (unique per program), effective A.Y., status draft/active/archived, default-for-new, year count, activation who/when/note |
| `curriculum_subjects` | version (FK), year, semester, code + normalized `code_key`, description, units DECIMAL(4,1), optional link to legacy `subjects` (FK RESTRICT) |
| `curriculum_prerequisites` | kind subject/standing/units/other, prerequisite subject (FK), min year, min units, text |
| `student_curricula` | the student's curriculum with history (`ended_at`), who and why |
| `curriculum_import_batches` | staged uploads: parsed rows, registrar edits, status, SHA-256 |
| `curriculum_audit_log` | change history (no FK, so the log outlives deletions) |

- All tables are InnoDB, using the live `programs` collation so the foreign keys work. Every FK is `ON DELETE RESTRICT`, so nothing cascades.
- The DDL is generated from `models.py` (the single source), and `full.sql` was updated too. The test suite verifies that *old DB + migrations == fresh full.sql*.
- The program-delete guard from Phase 1 now also refuses programs that have curricula.
- Deploy: `python scripts/migrate.py apply --backup-done`. Note that the app's `create_all()` also creates these new tables at start-up.
- Rollback: `python scripts/migrate.py rollback 0003_curriculum_management --backup-done` **refuses while curricula or assignments exist**. Export first, then add `--force`.

## Files
**New:** `website/curriculum/{__init__,service,importer,routes}.py`, `website/templates/curriculum/{_macros,index,version_detail,version_students,import_upload,import_review}.html`, `tests/test_phase2_curriculum.py`, `tests/fixtures/references/` (the 7 plans and 12 reg forms; the grade certification file was excluded because it holds student grades), `docs/PHASE2_REPORT.md`.
**Modified:** `website/models.py` (6 models), `website/schema_migrations.py` (0003 and a rollback guard), `scripts/migrate.py`, `website/__init__.py` (blueprint), `website/templates/admin_base.html` (sidebar link, plus `min-w-0` on the main area so wide tables scroll inside it on phones), `website/auth.py` (program-delete guard), `full.sql`, `tests/test_route_crawl.py`.

## Testing performed
- **Service rules:** duplicates (including spacing variants, and at the DB level without the service), term/units validation, prerequisite ordering, self, cross-version and cycle rules, moving and deleting with dependants, activation with and without confirmation, locking after assignment, clone independence, archive keeps the student's history, the default-version rule, resolver (assigned vs default), assignment history and the program check, grade-history delete protection, and audit content.
- **Importer:** every one of the 7 plans imported end to end through the screens. Subject counts match the files, and every checked prerequisite points to an earlier term of the same version. The DC 103 block and the exclude path work. Reg-form cross-checks work (BPA AE 125/TAX 301, BSCRIM spelling conflicts needing an explicit choice). Duplicate-upload warning, discard, and bad-file rejection all work.
- **Access:** cashier, accounting, guidance and student are refused on every page and action.
- **All-roles crawl** of every GET page (curriculum pages included): no 500s, and every POST form carries a CSRF token.
- **Headless Chrome with CSRF on:** upload → blocked import → fix → import → activate → assign students → reg-form cross-check. Checked at desktop and phone widths; there is no horizontal overflow on the new pages, and an existing page was compared and is unchanged.
- **Existing `scripts/smoke_pages.py`:** the same 2 data-dependent failures as on the original code, so no regressions.

## Left for later phases
- **Phase 3** uses `service.resolve_student_curriculum()` to build the regular REGFORM. The per-term registration record will store the curriculum version, which freezes it historically.
- Prerequisite checks against grades depend on Q6 (which grade table is official) and on the legacy links.
- The old *Manage Subjects* and xlsx import screens under Programs are still there and unchanged. They feed the grading module's `subjects` table. Once you confirm Q3, I'd point them to the curriculum module.
