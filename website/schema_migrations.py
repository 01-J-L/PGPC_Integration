"""Versioned, idempotent schema migrations for an EXISTING PGPC database.

Why this exists
---------------
`db.create_all()` (run at start-up) only creates *missing tables*; it never
adds a column to a table that already exists. Flask-Migrate is listed in
requirements.txt but was never initialised, and the production database was
built from full.sql, so an Alembic baseline would first have to be stamped.
Until that is done, schema changes are shipped here: each migration is small,
additive, safe to re-run, and recorded in the `schema_migrations` table.

Use
---
    python scripts/migrate.py status          # what is applied / pending
    python scripts/migrate.py apply           # take a backup first (see docs)

The application never applies migrations by itself; at start-up it only logs
a warning when migrations are pending.
"""

import datetime

# A migration has either `steps`, a list of (check_sql, check_params, apply_sql)
# where `check_sql` returns a row when the step is ALREADY done, or `build`, a
# function that inspects the live schema and returns only the SQL still needed.
# Either way re-running a migration is a no-op.
_COLUMN_EXISTS = ("SELECT 1 FROM information_schema.COLUMNS "
                  "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s")


def _add_column(table, column, ddl):
    return (_COLUMN_EXISTS, (table, column), f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")


_BLOB_AT_LEAST_MEDIUM = ("SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                         "AND TABLE_NAME = %s AND COLUMN_NAME = %s AND DATA_TYPE IN ('mediumblob', 'longblob')")

# Every uploaded-file column on `applicants` (uploads are capped at 5 MB in
# process_uploaded_file(); MEDIUMBLOB holds up to 16 MB).
DOCUMENT_BLOB_COLUMNS = [
    'photo', 'shs_diploma_file', 'shs_card_file', 'birth_certificate_file',
    'enrollment_good_moral_file', 'enrollment_psa_birth_file', 'enrollment_photos_2x2_file',
    'enrollment_entrance_fee_proof_file', 'enrollment_voters_id_file', 'enrollment_cbs_file',
    'enrollment_brgy_cert_file', 'pwd_id_file',
    'enrollment_shs_card_file', 'enrollment_shs_diploma_file',
]


def _widen_document_blobs(cur):
    """One ALTER for every still-narrow column, so the (large) applicants table
    is rebuilt once instead of once per column."""
    narrow = []
    for col in DOCUMENT_BLOB_COLUMNS:
        cur.execute(_COLUMN_EXISTS, ('applicants', col))
        exists = cur.fetchone()
        cur.execute(_BLOB_AT_LEAST_MEDIUM, ('applicants', col))
        if exists and not cur.fetchone():
            narrow.append(col)
    if not narrow:
        return []
    return ["ALTER TABLE `applicants` " + ", ".join(f"MODIFY `{c}` MEDIUMBLOB NULL" for c in narrow)]


_TABLE_EXISTS = ("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s")


def model_table_ddl(table_names):
    """CREATE TABLE statements generated from website/models.py, so the model
    stays the single source of truth for new tables."""
    from sqlalchemy.schema import CreateTable, CreateIndex
    from sqlalchemy.dialects import mysql
    from website import db
    from website import models  # noqa: F401  (registers every table)
    out = []
    for name in table_names:
        table = db.metadata.tables[name]
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).strip()
        ddl = ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)
        out.append((name, ddl, [str(CreateIndex(ix).compile(dialect=mysql.dialect())).strip() for ix in table.indexes]))
    return out


def _create_model_tables(table_names):
    def build(cur):
        # New tables reference programs.program_id / student_users.id; a FOREIGN
        # KEY on a VARCHAR needs the same charset+collation, so copy it from the
        # live programs table instead of trusting the server default.
        cur.execute("SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'programs' AND COLUMN_NAME = 'program_id'")
        row = cur.fetchone()
        options = (f" ENGINE=InnoDB DEFAULT CHARSET={row[0]} COLLATE={row[1]}" if row and row[0]
                   else " ENGINE=InnoDB")
        statements = []
        for name, ddl, index_ddl in model_table_ddl(table_names):
            cur.execute(_TABLE_EXISTS, (name,))
            if cur.fetchone():
                continue
            # Replace the model's table options with the live database's.
            statements.append(ddl[:ddl.rstrip().rfind(')') + 1] + options)
            statements.extend(index_ddl)
        return statements
    return build


# Default checklist (Phase 5). These mirror the process of the uploaded system:
# the registration form, the registrar's document review and Accounting. The Dean
# and President flags exist in student_enrollment_records but were removed from the
# review screen, so they are seeded inactive for an administrator to switch on.
CHECKLIST_SEED = [
    # code, label, description, kind, approver_roles, applies_to, required, active, sort
    ('regform', 'Registration form finalized',
     "Met automatically when the registrar finalizes this term's registration form.",
     'regform', 'registrar,admin', 'all', 1, 1, 10),
    ('documents', 'Enrollment documents reviewed',
     'The registrar has checked the uploaded documents on the Documents tab.',
     'manual', 'registrar,admin', 'all', 1, 1, 20),
    ('payment', 'Accounting / enrollment payment policy',
     "Met when the policy set on Enrollment Assessments is met. Under 'Accounting clearance', Accounting gives the clearance.",
     'payment', 'accounting,admin', 'all', 1, 1, 30),
    ('dean', 'Dean approval', 'Inactive by default (removed from the review screen of the uploaded system).',
     'manual', 'admin', 'all', 0, 0, 40),
    ('president', 'President approval', 'Inactive by default (removed from the review screen of the uploaded system).',
     'manual', 'admin', 'all', 0, 0, 50),
]

def _sql_literal(v):
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace('\\', '\\\\').replace("'", "''") + "'"


def checklist_seed_sql():
    """INSERT IGNORE for the default checklist lines (used by migration 0006 and full.sql)."""
    rows = ',\n  '.join('(' + ', '.join(_sql_literal(v) for v in row) + ", 'migration 0006')" for row in CHECKLIST_SEED)
    return ("INSERT IGNORE INTO enrollment_checklist_requirements "
            "(code, label, description, kind, approver_roles, applies_to, is_required, is_active, sort_order, updated_by) "
            "VALUES\n  " + rows)


def _create_and_seed_checkin(cur):
    statements = _create_model_tables(['enrollment_checklist_requirements', 'enrollment_checklist_items',
                                       'enrollment_checkins', 'enrollment_audit_log'])(cur)
    return statements + [checklist_seed_sql()]


# Phase 7: keep today's behaviour on deploy. Students currently see every grade,
# so each term that already has grades starts out released.
GRADE_RELEASE_SEED_SQL = (
    "INSERT IGNORE INTO grade_releases (academic_year, semester, program_key, is_released, released_by, released_at, reason) "
    "SELECT DISTINCT TRIM(academic_year), TRIM(semester), '', 1, 'migration 0008', NOW(), "
    "'Grades already visible before grade release control existed' "
    "FROM student_grades WHERE TRIM(academic_year) <> '' AND TRIM(semester) <> ''")


def _create_and_seed_grade_releases(cur):
    return _create_model_tables(['grade_releases', 'grade_holds'])(cur) + [GRADE_RELEASE_SEED_SQL]


MIGRATIONS = [
    {
        'id': '0001_enrollment_shs_document_columns',
        'description': ("P1-03: enrollment copies of Form 138 (SHS card) and SHS diploma. "
                        "The registrar upload and document viewer already wrote/read these "
                        "columns, which did not exist, so both actions crashed."),
        'steps': [
            _add_column('applicants', 'enrollment_shs_card_file', 'MEDIUMBLOB NULL'),
            _add_column('applicants', 'enrollment_shs_card_filename', 'VARCHAR(255) NULL'),
            _add_column('applicants', 'enrollment_shs_card_mimetype', 'VARCHAR(100) NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_file', 'MEDIUMBLOB NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_filename', 'VARCHAR(255) NULL'),
            _add_column('applicants', 'enrollment_shs_diploma_mimetype', 'VARCHAR(100) NULL'),
        ],
        'rollback': [
            "ALTER TABLE `applicants` DROP COLUMN `enrollment_shs_card_file`, "
            "DROP COLUMN `enrollment_shs_card_filename`, DROP COLUMN `enrollment_shs_card_mimetype`, "
            "DROP COLUMN `enrollment_shs_diploma_file`, DROP COLUMN `enrollment_shs_diploma_filename`, "
            "DROP COLUMN `enrollment_shs_diploma_mimetype`",
        ],
    },
    {
        'id': '0002_document_columns_mediumblob',
        'description': ("P1-12: document/photo columns were BLOB (64 KB max) while uploads allow 5 MB, "
                        "so normal scans failed with 'Data too long'. Widen to MEDIUMBLOB (no data change). "
                        "Rebuilds the applicants table: run in a quiet period."),
        'build': _widen_document_blobs,
        # Narrowing back fails (strict mode) if any file is now larger than 64 KB,
        # which is the safe outcome; restore from the backup instead in that case.
        'rollback': ["ALTER TABLE `applicants` " + ", ".join(
            f"MODIFY `{c}` BLOB NULL" for c in DOCUMENT_BLOB_COLUMNS)],
    },
    {
        'id': '0003_curriculum_management',
        'description': ("Phase 2: versioned curricula (curriculum_versions, curriculum_subjects, "
                        "curriculum_prerequisites), student curriculum assignment (student_curricula), "
                        "staged reference imports (curriculum_import_batches) and curriculum_audit_log. "
                        "New tables only; no existing table is changed."),
        'build': _create_model_tables(['curriculum_versions', 'curriculum_subjects', 'curriculum_prerequisites',
                                       'student_curricula', 'curriculum_import_batches', 'curriculum_audit_log']),
        # Refuse to drop tables that already hold curriculum data (use --force after exporting).
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM curriculum_versions) + (SELECT COUNT(*) FROM student_curricula)",
        'rollback': [
            "DROP TABLE IF EXISTS `curriculum_audit_log`",
            "DROP TABLE IF EXISTS `curriculum_import_batches`",
            "DROP TABLE IF EXISTS `student_curricula`",
            "DROP TABLE IF EXISTS `curriculum_prerequisites`",
            "DROP TABLE IF EXISTS `curriculum_subjects`",
            "DROP TABLE IF EXISTS `curriculum_versions`",
        ],
    },
    {
        'id': '0004_registration_forms',
        'description': ("Phase 3: per-term registration forms (registration_forms), their subject lines with "
                        "section/schedule snapshots (registration_form_subjects) and the registrar change log "
                        "(registration_form_changes). New tables only."),
        'build': _create_model_tables(['registration_forms', 'registration_form_subjects', 'registration_form_changes']),
        'rollback_guard': "SELECT COUNT(*) FROM registration_forms",
        'rollback': [
            "DROP TABLE IF EXISTS `registration_form_changes`",
            "DROP TABLE IF EXISTS `registration_form_subjects`",
            "DROP TABLE IF EXISTS `registration_forms`",
        ],
    },
    {
        'id': '0005_enrollment_finance',
        'description': ("Phase 4: enrollment assessments per registration form and their charge links, payment "
                        "verification, payment reversals (no deletes), payment double-submit guard and the finance "
                        "audit log. New tables only; assessments/payments are not altered."),
        'build': _create_model_tables(['enrollment_assessments', 'enrollment_assessment_items', 'payment_verifications',
                                       'payment_reversals', 'payment_submissions', 'finance_audit_log']),
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM enrollment_assessments) + (SELECT COUNT(*) FROM payment_reversals) "
                          "+ (SELECT COUNT(*) FROM payment_verifications)",
        'rollback': [
            "DROP TABLE IF EXISTS `finance_audit_log`",
            "DROP TABLE IF EXISTS `payment_submissions`",
            "DROP TABLE IF EXISTS `payment_reversals`",
            "DROP TABLE IF EXISTS `payment_verifications`",
            "DROP TABLE IF EXISTS `enrollment_assessment_items`",
            "DROP TABLE IF EXISTS `enrollment_assessments`",
        ],
    },
    {
        'id': '0006_enrollment_checkin',
        'description': ("Phase 5: configurable enrollment approval checklist, per-student checklist approvals, "
                        "registrar check-in records (undo keeps history) and the enrollment audit log. "
                        "New tables only; seeds the default checklist lines."),
        'build': _create_and_seed_checkin,
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM enrollment_checkins) + (SELECT COUNT(*) FROM enrollment_checklist_items)",
        'rollback': [
            "DROP TABLE IF EXISTS `enrollment_audit_log`",
            "DROP TABLE IF EXISTS `enrollment_checkins`",
            "DROP TABLE IF EXISTS `enrollment_checklist_items`",
            "DROP TABLE IF EXISTS `enrollment_checklist_requirements`",
        ],
    },
    {
        'id': '0007_control_numbers',
        'description': ("Phase 6: PGPC-YY-NNNN enrollment control numbers: one locked counter per school-year code "
                        "(control_number_sequences) and the register of issued numbers (control_numbers, unique, "
                        "never reused). New tables only."),
        'build': _create_model_tables(['control_number_sequences', 'control_numbers']),
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM control_numbers) + (SELECT COUNT(*) FROM control_number_sequences)",
        'rollback': [
            "DROP TABLE IF EXISTS `control_numbers`",
            "DROP TABLE IF EXISTS `control_number_sequences`",
        ],
    },
    {
        'id': '0008_grade_visibility',
        'description': ("Phase 7: grade visibility. grade_releases (release per term, optional per-program override) "
                        "and grade_holds (per-student holds, lifting keeps the row). Every term that already has grades "
                        "is recorded as RELEASED so students keep seeing what they see today. New tables only."),
        'build': _create_and_seed_grade_releases,
        'rollback_guard': "SELECT (SELECT COUNT(*) FROM grade_holds) + "
                          "(SELECT COUNT(*) FROM grade_releases WHERE released_by <> 'migration 0008' OR released_by IS NULL)",
        'rollback': [
            "DROP TABLE IF EXISTS `grade_holds`",
            "DROP TABLE IF EXISTS `grade_releases`",
        ],
    },
    {
        'id': '0009_section_archives',
        'description': ("Phase 8: section_archives (archive instead of deleting a section that has students, "
                        "schedules, class records or registration forms). New table only; sections is not altered "
                        "(the existing foreign keys to sections are already ON DELETE RESTRICT)."),
        'build': _create_model_tables(['section_archives']),
        'rollback_guard': "SELECT COUNT(*) FROM section_archives",
        'rollback': ["DROP TABLE IF EXISTS `section_archives`"],
    },
]

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    description VARCHAR(500),
    applied_at DATETIME NOT NULL
) DEFAULT CHARSET=utf8mb4
"""


def _ensure_ledger(cur):
    cur.execute(_LEDGER_DDL)


def applied_ids(conn):
    cur = conn.cursor()
    _ensure_ledger(cur)
    cur.execute("SELECT id FROM schema_migrations")
    ids = {r[0] for r in cur.fetchall()}
    cur.close()
    return ids


def pending(conn):
    done = applied_ids(conn)
    return [m for m in MIGRATIONS if m['id'] not in done]


def apply_all(conn, log=print):
    """Apply every pending migration. DDL auto-commits in MySQL, so each step is
    checked before it runs; a re-run after a failure resumes where it stopped."""
    applied = []
    for m in pending(conn):
        cur = conn.cursor()
        for check_sql, params, apply_sql in m.get('steps', []):
            cur.execute(check_sql, params)
            if cur.fetchone():
                log(f"  [skip] {apply_sql}")
                continue
            log(f"  [run ] {apply_sql}")
            cur.execute(apply_sql)
        if 'build' in m:
            # `build(cur)` inspects the live schema and returns only what is still needed.
            statements = m['build'](cur)
            if not statements:
                log("  [skip] nothing left to change")
            for sql in statements:
                log(f"  [run ] {sql}")
                cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations (id, description, applied_at) VALUES (%s, %s, %s)",
                    (m['id'], m['description'][:500], datetime.datetime.now()))
        conn.commit()
        cur.close()
        applied.append(m['id'])
        log(f"[OK] {m['id']}")
    return applied
